"""The agent loop (milestone 1).

decide -> (code validates and executes) -> observation -> decide again, until a
stop condition: final answer, clarification question, controlled stop (tool
failure, clarification limit) or an exhausted round budget.

Who decides what:
  decision component (mock by default, LLM optional) - proposes the next action
  code (this file + tools.py)                        - validates, executes, enforces limits
Code-side enforcement: the profile and the report input always come from the
state, never from the arguments proposed by the decision component.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import ConversationState, SessionStore, build_context
from .tools import FIELD_LABELS, ToolExecutor

QUESTIONS = {
    "age": "מהו גילך?",
    "is_student": "האם אתה סטודנט?",
    "employed": "האם אתה מועסק כעת?",
    "monthly_income": "מהי הכנסתך החודשית (בשקלים)?",
}
FIELD_ORDER = ["age", "is_student", "employed", "monthly_income"]
BENEFIT_KEYWORDS = {"ארנונה": "arnona_discount", "תחבורה": "student_transport", "מענק": "work_grant"}
ACTION_WORDS = ("תגיש", "הגש", "תשלח", "שלח", "תמלא")
TOOL_FAILURE_TEXT = "לא הצלחתי לגשת למאגר הכללים, נסה שוב מאוחר יותר."
REFUSAL_TEXT = "אני רק בודק זכאות. אני לא מגיש בקשות ולא שולח או שומר מידע."


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
class Trace:
    """A list of events of one run. Can be saved as JSON Lines."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def add(self, event: str, **data: Any) -> None:
        self.events.append({"step": len(self.events) + 1, "event": event, **data})

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Profile extraction used by the mock decision component
# --------------------------------------------------------------------------- #
def _to_number(text: str) -> Optional[float]:
    cleaned = text.replace(",", "").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return int(number) if number == int(number) else number


def extract_profile(text: str, expecting: Optional[str] = None) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    match = re.search(r"(?:גיל|בן|בת)\s*(\d{1,3})", text)
    if match:
        fields["age"] = int(match.group(1))
    if "לא סטודנט" in text:
        fields["is_student"] = False
    elif "סטודנט" in text:
        fields["is_student"] = True
    if re.search(r"לא (?:מועסק|עובד)|מובטל", text):
        fields["employed"] = False
    elif re.search(r"מועסק|עובד", text):
        fields["employed"] = True
    match = re.search(r"הכנסה[^\d]{0,15}(\d[\d,\.]*)", text)
    if match:
        number = _to_number(match.group(1).rstrip(".,"))
        if number is not None:
            fields["monthly_income"] = number
    # a bare answer to the question we just asked
    if expecting and expecting not in fields:
        stripped = text.strip().strip(".")
        if expecting in ("age", "monthly_income"):
            number = _to_number(stripped)
            if number is not None:
                fields[expecting] = int(number) if expecting == "age" else number
        elif stripped in ("כן", "כן."):
            fields[expecting] = True
        elif stripped in ("לא", "לא."):
            fields[expecting] = False
    return fields


# --------------------------------------------------------------------------- #
# Decision components
# --------------------------------------------------------------------------- #
class MockDecider:
    """Deterministic decision component (no language model, no key, no cost).

    It reads the context only for explanation questions ("למה ..."), so the
    full-vs-compact comparison shows what is lost when the context is reduced.
    """

    name = "mock"

    def __init__(self, max_clarifications: int = 3) -> None:
        self.max_clarifications = max_clarifications

    def decide(self, state: ConversationState, context: Dict[str, Any]) -> Dict[str, Any]:
        if state.last_error:
            return self._stop_after_failure(state.last_error)
        if state.pending_user:
            text = state.last_user_message()
            if any(word in text for word in ACTION_WORDS):
                return {"kind": "final", "text": REFUSAL_TEXT}
            explained = self._explain(text, context)
            if explained:
                return {"kind": "final", "text": explained}
            return {"kind": "update_profile", "fields": extract_profile(text, state.pending_field)}
        if not state.profile:
            return self._clarify_or_stop(state, [f for f in FIELD_ORDER])
        if state.rules is None:
            return {"kind": "tool", "name": "check_policy_rules", "args": {"category": "all"}}
        pending = [b for b in state.rules if b["id"] not in state.verified]
        if pending:
            return {"kind": "tool", "name": "verify_user_eligibility",
                    "args": {"benefit_id": pending[0]["id"], "profile": dict(state.profile)}}
        missing = [f for f in state.undetermined_fields() if f not in state.asked_fields]
        if missing and state.clarifications < self.max_clarifications:
            return self._clarify_or_stop(state, missing)
        if state.report is None:
            return {"kind": "tool", "name": "generate_rights_report", "args": {"results": []}}
        text = state.report
        unknown = [r for r in state.verified.values() if r["eligible"] is None]
        if unknown:
            text += f"\nלא ניתן היה לקבוע זכאות ל-{len(unknown)} הטבות נוספות בגלל מידע חסר."
        return {"kind": "final", "text": text}

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _stop_after_failure(error: Dict[str, Any]) -> Dict[str, Any]:
        if error.get("retryable"):
            return {"kind": "final", "text": TOOL_FAILURE_TEXT}
        return {"kind": "final", "text": f"אירעה שגיאה קבועה בכלי {error.get('tool')}: {error.get('message')}"}

    def _clarify_or_stop(self, state: ConversationState, candidates: List[str]) -> Dict[str, Any]:
        options = [f for f in candidates if f not in state.asked_fields]
        if not options or state.clarifications >= self.max_clarifications:
            return {"kind": "final", "text": "לא הצלחתי להשלים את הפרטים הנדרשים. נסה לתאר את מצבך שוב."}
        field = options[0]
        return {"kind": "clarify", "field": field, "text": QUESTIONS[field]}

    @staticmethod
    def _explain(text: str, context: Dict[str, Any]) -> Optional[str]:
        if "למה" not in text:
            return None
        benefit_id = next((bid for word, bid in BENEFIT_KEYWORDS.items() if word in text), None)
        if benefit_id is None:
            return None
        for message in reversed(context["messages"]):
            if message["role"] == "tool" and message.get("tool_name") == "verify_user_eligibility":
                result = json.loads(message["content"])
                if result.get("benefit_id") == benefit_id:
                    return f"הנימוק לפי בדיקת הכלי: {result['reason']}"
        return "אין בידי את הנימוק בהקשר הנוכחי. נדרשת בדיקה מחדש."


class LLMDecider:
    """Optional decision component backed by a real model.

    Used only when an API key is configured (env var ANTHROPIC_API_KEY, in Colab
    via Secrets). It has NOT been run in the environment where this code was
    written (no key), so it is untested. Tests always use MockDecider.
    """

    name = "llm"
    INSTRUCTIONS = (
        "החזר אך ורק אובייקט JSON אחד שמתאר את הפעולה הבאה, באחת הצורות:\n"
        '{"kind":"update_profile","fields":{"age":26}} (שדות מותרים: age, is_student, employed, monthly_income)\n'
        '{"kind":"tool","name":"check_policy_rules","args":{"category":"all"}}\n'
        '{"kind":"tool","name":"verify_user_eligibility","args":{"benefit_id":"..."}}\n'
        '{"kind":"tool","name":"generate_rights_report","args":{}}\n'
        '{"kind":"clarify","field":"monthly_income","text":"שאלה אחת קצרה"}\n'
        '{"kind":"final","text":"תשובה למשתמש"}\n'
        "אל תמציא נתונים על המשתמש. הקוד מאמת ומבצע, ואחראי על הפרופיל ועל הדוח."
    )

    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        import anthropic  # imported only when a key exists

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("ZCHUTI_MODEL", "claude-sonnet-5-5")

    def decide(self, state: ConversationState, context: Dict[str, Any]) -> Dict[str, Any]:
        transcript = "\n".join(f"[{m['role']}] {m['content']}" for m in context["messages"])
        prompt = (
            f"{self.INSTRUCTIONS}\n\nמצב נוכחי:\n"
            f"profile={json.dumps(state.profile, ensure_ascii=False)}\n"
            f"rules_loaded={state.rules is not None}, verified={sorted(state.verified)}, "
            f"report_ready={state.report is not None}, pending_user_message={state.pending_user}, "
            f"clarifications={state.clarifications}\n\nשיחה:\n{transcript}"
        )
        response = self.client.messages.create(
            model=self.model, max_tokens=500, system=context["system"],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        try:
            return json.loads(text[text.index("{"): text.rindex("}") + 1])
        except ValueError:
            return {"kind": "invalid", "raw": text[:200]}


def make_decider(api_key: Optional[str] = None):
    """Mock by default. The real model only when a key is configured."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    return LLMDecider(api_key) if api_key else MockDecider()


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
class Agent:
    def __init__(self, decider, executor: ToolExecutor, store: Optional[SessionStore] = None,
                 max_rounds: int = 10, max_clarifications: int = 3, context_mode: str = "full") -> None:
        self.decider = decider
        self.executor = executor
        self.store = store or SessionStore()
        self.max_rounds = max_rounds
        self.max_clarifications = max_clarifications
        self.context_mode = context_mode

    def handle_message(self, conversation_id: str, user_text: str, trace: Optional[Trace] = None) -> Dict[str, Any]:
        trace = trace if trace is not None else Trace()
        state = self.store.get(conversation_id)
        state.last_error = None
        state.add_message("user", user_text)
        state.status = "active"
        trace.add("user", conversation_id=conversation_id, text=user_text)

        for round_number in range(1, self.max_rounds + 1):
            context = build_context(state, self.context_mode)
            action = self.decider.decide(state, context)
            trace.add("decision", round=round_number, decider=getattr(self.decider, "name", "?"), action=action)
            kind = action.get("kind") if isinstance(action, dict) else None

            if kind == "final":
                return self._finish(state, trace, action["text"], round_number)
            if kind == "clarify":
                if state.clarifications >= self.max_clarifications:
                    return self._stop(state, trace, "clarification_limit", "הגעתי למגבלת שאלות ההבהרה.", round_number)
                field = action.get("field")
                if field not in FIELD_LABELS:
                    self._observe_invalid(state, trace, "clarify_unknown_field")
                    continue
                state.clarifications += 1
                state.asked_fields.append(field)
                state.pending_field = field
                state.status = "clarifying"
                state.add_message("assistant", action["text"])
                trace.add("stop", reason="clarify", text=action["text"])
                return {"status": "clarify", "text": action["text"], "field": field, "rounds": round_number}
            if kind == "update_profile":
                self._update_profile(state, trace, action)
            elif kind == "tool":
                self._run_tool(state, trace, action)
            else:
                self._observe_invalid(state, trace, "unknown_action_kind")
        return self._stop(state, trace, "budget_exhausted", "הגעתי למגבלת הצעדים בלי לסיים.", self.max_rounds)

    # ---- actions -----------------------------------------------------------
    def _update_profile(self, state: ConversationState, trace: Trace, action: Dict[str, Any]) -> None:
        state.pending_user = False
        state.pending_field = None
        try:
            changed = state.update_profile(action.get("fields", {}))
        except (ValueError, AttributeError) as exc:
            trace.add("observation", source="update_profile", ok=False, error=str(exc))
            return
        trace.add("observation", source="update_profile", ok=True, changed=changed, profile=dict(state.profile))

    def _run_tool(self, state: ConversationState, trace: Trace, action: Dict[str, Any]) -> None:
        name = action.get("name")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        overridden = False
        if name == "verify_user_eligibility":  # the profile always comes from the state
            forced = dict(args, profile=dict(state.profile))
            overridden = forced != args
            args = forced
        elif name == "generate_rights_report":  # the report is built from verified results only
            forced = {"results": list(state.verified.values())}
            overridden = forced != args
            args = forced
        request = {"name": name, "args": args}
        trace.add("tool_call", name=name, args=args, args_overridden_by_code=overridden)
        state.add_message("assistant", json.dumps(request, ensure_ascii=False), tool_name=name, is_call=True)
        result = self.executor.execute(request)
        trace.add("tool_result", name=name, result=result)
        state.add_message("tool", json.dumps(result, ensure_ascii=False), tool_name=name)
        if not result.get("ok"):
            state.last_error = {"tool": name, **result["error"], "attempts": result.get("attempts")}
            return
        if name == "check_policy_rules":
            state.rules = result["benefits"]
        elif name == "verify_user_eligibility":
            state.record_verification(result)
        elif name == "generate_rights_report":
            state.report = result["report"]
            state.eligible_ids = result["eligible_ids"]

    # ---- ending a run ------------------------------------------------------
    def _finish(self, state: ConversationState, trace: Trace, text: str, rounds: int) -> Dict[str, Any]:
        state.add_message("assistant", text)
        if state.last_error:
            state.status = "stopped"
            trace.add("stop", reason="tool_failure", text=text)
            return {"status": "stopped", "reason": "tool_failure", "text": text, "rounds": rounds}
        state.status = "done"
        trace.add("stop", reason="final", text=text)
        return {"status": "final", "text": text, "rounds": rounds}

    def _stop(self, state: ConversationState, trace: Trace, reason: str, text: str, rounds: int) -> Dict[str, Any]:
        state.status = "stopped"
        state.add_message("assistant", text)
        trace.add("stop", reason=reason, text=text)
        return {"status": "stopped", "reason": reason, "text": text, "rounds": rounds}

    @staticmethod
    def _observe_invalid(state: ConversationState, trace: Trace, reason: str) -> None:
        trace.add("observation", source="agent", ok=False, error=reason)


def build_agent(decider=None, loader=None, context_mode: str = "full", max_rounds: int = 10,
                max_clarifications: int = 3, backoff_s: float = 0.0, timeout_s: float = 2.0,
                max_retries: int = 2) -> Agent:
    """Wire the rules store, the tools, the executor and the agent together."""
    from .tools import RulesStore, ToolExecutor, make_tools

    store = RulesStore(loader=loader) if loader else RulesStore()
    tools = make_tools(store, timeout_s=timeout_s, max_retries=max_retries, backoff_s=backoff_s)
    return Agent(decider or MockDecider(max_clarifications), ToolExecutor(tools),
                 max_rounds=max_rounds, max_clarifications=max_clarifications, context_mode=context_mode)
