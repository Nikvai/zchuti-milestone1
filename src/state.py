"""Conversation state (explicit schema) and context management (milestone 1).

The state is defined in code, kept separately for every conversation, and the
context that goes to the model is built from it in one of two modes:
  full    - the whole history is passed to the model
  compact - a short structured summary plus only the current turn
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tools import PROFILE_SCHEMA, validate

SYSTEM_PROMPT = (
    "אתה סוכן שבודק זכאות להטבות (נתונים סינתטיים להדגמה). "
    "אתה מציע פעולה אחת בכל פעם. הקוד מאמת ומבצע. "
    "אסור להציג הטבה שהכלי לא החזיר כזכאי."
)

STATE_FIELDS = {
    "conversation_id": str,
    "profile": dict,
    "messages": list,
    "rules": (list, type(None)),
    "verified": dict,
    "report": (str, type(None)),
    "eligible_ids": list,
    "asked_fields": list,
    "pending_field": (str, type(None)),
    "clarifications": int,
    "turn": int,
    "pending_user": bool,
    "last_error": (dict, type(None)),
    "status": str,
}
STATUSES = ("active", "clarifying", "done", "stopped")


@dataclass
class ConversationState:
    conversation_id: str
    profile: Dict[str, Any] = field(default_factory=dict)  # what the user told us (validated)
    messages: List[Dict[str, Any]] = field(default_factory=list)  # full history
    rules: Optional[List[Dict[str, Any]]] = None  # result of check_policy_rules
    verified: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # benefit_id -> verify result
    report: Optional[str] = None
    eligible_ids: List[str] = field(default_factory=list)
    asked_fields: List[str] = field(default_factory=list)
    pending_field: Optional[str] = None  # the field of the clarification we are waiting for
    clarifications: int = 0
    turn: int = 0
    pending_user: bool = False  # a user message was not yet processed
    last_error: Optional[Dict[str, Any]] = None
    status: str = "active"

    # ---- schema ------------------------------------------------------------
    def validate(self) -> Optional[str]:
        """Return None if the state matches its schema, otherwise a message."""
        for name, expected in STATE_FIELDS.items():
            if not isinstance(getattr(self, name), expected):
                return f"state.{name}: wrong type"
        if self.status not in STATUSES:
            return "state.status: unknown status"
        return validate(PROFILE_SCHEMA, self.profile, "state.profile")

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy({name: getattr(self, name) for name in STATE_FIELDS})

    # ---- changes -----------------------------------------------------------
    def add_message(self, role: str, content: str, **extra: Any) -> None:
        if role == "user":
            self.turn += 1
            self.pending_user = True
        self.messages.append({"role": role, "content": content, "turn": self.turn, **extra})

    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message["role"] == "user":
                return message["content"]
        return ""

    def update_profile(self, fields: Dict[str, Any]) -> bool:
        """Merge validated fields. Returns True if the profile changed."""
        problem = validate(PROFILE_SCHEMA, fields, "fields")
        if problem:
            raise ValueError(problem)
        changed = any(self.profile.get(key) != value for key, value in fields.items())
        self.profile.update(fields)
        if changed:  # old results may no longer be right
            self.verified = {}
            self.report = None
            self.eligible_ids = []
        return changed

    def record_verification(self, result: Dict[str, Any]) -> None:
        self.verified[result["benefit_id"]] = {
            "benefit_id": result["benefit_id"],
            "eligible": result["eligible"],
            "missing_fields": list(result["missing_fields"]),
            "reason": result["reason"],
        }

    def undetermined_fields(self) -> List[str]:
        """Missing fields, ordered by how many benefits they block, then by a fixed order."""
        order = ["age", "is_student", "employed", "monthly_income"]
        counts: Dict[str, int] = {}
        for result in self.verified.values():
            if result["eligible"] is None:
                for name in result["missing_fields"]:
                    counts[name] = counts.get(name, 0) + 1
        return sorted(counts, key=lambda n: (-counts[n], order.index(n)))


class SessionStore:
    """Keeps a separate state object for every conversation id."""

    def __init__(self) -> None:
        self._sessions: Dict[str, ConversationState] = {}

    def get(self, conversation_id: str) -> ConversationState:
        if conversation_id not in self._sessions:
            self._sessions[conversation_id] = ConversationState(conversation_id=conversation_id)
        return self._sessions[conversation_id]

    def ids(self) -> List[str]:
        return list(self._sessions)


# --------------------------------------------------------------------------- #
# Context management
# --------------------------------------------------------------------------- #
def build_context(state: ConversationState, mode: str = "full") -> Dict[str, Any]:
    """The information that is passed to the decision component (the model)."""
    if mode == "full":
        messages = copy.deepcopy(state.messages)
    elif mode == "compact":
        summary = {
            "profile": state.profile,
            "eligible_ids": state.eligible_ids,
            "asked_fields": state.asked_fields,
        }
        current = [m for m in state.messages if m["turn"] == state.turn]
        messages = [{"role": "system", "content": "סיכום מצב: " + json.dumps(summary, ensure_ascii=False), "turn": 0}]
        messages += copy.deepcopy(current)
    else:
        raise ValueError("mode must be 'full' or 'compact'")
    return {"mode": mode, "system": SYSTEM_PROMPT, "messages": messages}


def context_size(context: Dict[str, Any]) -> Dict[str, int]:
    return {
        "messages": len(context["messages"]),
        "chars": len(context["system"]) + sum(len(m["content"]) for m in context["messages"]),
    }
