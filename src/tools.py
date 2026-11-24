"""Tools and their contracts (milestone 1).

Every tool has an explicit contract: name, description, input schema and output
schema. The executor validates the request and the permissions BEFORE running a
tool, always returns a structured JSON-like dict, separates transient errors
(retryable) from permanent ones, and enforces a timeout and a retry limit.

All data is synthetic (data/rules.json). No real personal data is used.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "rules.json"
DISCLAIMER = "הערכה ראשונית לצורכי הדגמה בלבד, ואינה תחליף לבדיקה מול הגוף הרשמי"

CATEGORIES = ["all", "transport", "arnona", "work_grant"]
FIELD_LABELS = {
    "age": "גיל",
    "is_student": "סטודנט",
    "employed": "מועסק",
    "monthly_income": "הכנסה חודשית",
}
OPS = ("<=", ">=", "<", ">", "==")
OP_WORDS = {"<=": "לכל היותר", ">=": "לפחות", "<": "פחות מ-", ">": "יותר מ-", "==": "בדיוק"}


# --------------------------------------------------------------------------- #
# Results and errors
# --------------------------------------------------------------------------- #
class TransientError(Exception):
    """Temporary failure: retrying may help."""

    def __init__(self, message: str = "temporary failure", code: str = "temporary_unavailable"):
        super().__init__(message)
        self.code = code


class PermanentError(Exception):
    """Permanent failure: retrying will not help."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def ok(**data: Any) -> Dict[str, Any]:
    return {"ok": True, **data}


def err(code: str, message: str, retryable: bool = False) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


# --------------------------------------------------------------------------- #
# A tiny JSON-schema-like validator (standard library only)
# --------------------------------------------------------------------------- #
def _type_ok(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def validate(schema: Dict[str, Any], value: Any, path: str = "args") -> Optional[str]:
    """Return None if value matches schema, otherwise a short error message."""
    types = schema.get("type")
    if types is not None:
        type_list = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t) for t in type_list):
            return f"{path}: expected {'/'.join(type_list)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: must be one of {schema['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: must be <= {schema['maximum']}"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key}: required"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    return f"{path}.{key}: unexpected field"
        for key, sub in props.items():
            if key in value:
                problem = validate(sub, value[key], f"{path}.{key}")
                if problem:
                    return problem
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path}: needs at least {schema['minItems']} items"
        if "items" in schema:
            for i, item in enumerate(value):
                problem = validate(schema["items"], item, f"{path}[{i}]")
                if problem:
                    return problem
    return None


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
PROFILE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "age": {"type": "integer", "minimum": 0, "maximum": 120},
        "is_student": {"type": "boolean"},
        "employed": {"type": "boolean"},
        "monthly_income": {"type": "number", "minimum": 0},
    },
    "additionalProperties": False,
}

_CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "op": {"type": "string", "enum": list(OPS)},
        "value": {"type": ["integer", "number", "boolean"]},
    },
    "required": ["field", "op", "value"],
    "additionalProperties": False,
}

CHECK_POLICY_INPUT = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": CATEGORIES}},
    "required": ["category"],
    "additionalProperties": False,
}
CHECK_POLICY_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "benefits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "required_fields": {"type": "array", "items": {"type": "string"}},
                    "conditions": {"type": "array", "items": _CONDITION_SCHEMA},
                    "source": {"type": "string"},
                },
                "required": ["id", "name", "category", "required_fields", "conditions", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ok", "benefits"],
    "additionalProperties": False,
}

VERIFY_INPUT = {
    "type": "object",
    "properties": {"benefit_id": {"type": "string"}, "profile": PROFILE_SCHEMA},
    "required": ["benefit_id", "profile"],
    "additionalProperties": False,
}
VERIFY_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "benefit_id": {"type": "string"},
        "eligible": {"type": ["boolean", "null"]},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["ok", "benefit_id", "eligible", "missing_fields", "reason"],
    "additionalProperties": False,
}

REPORT_INPUT = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "benefit_id": {"type": "string"},
                    "eligible": {"type": ["boolean", "null"]},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["benefit_id", "eligible"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
REPORT_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "report": {"type": "string"},
        "eligible_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "report", "eligible_ids"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Rules store (synthetic data). The loader can be replaced in tests.
# --------------------------------------------------------------------------- #
def _load_file(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PermanentError("rules_file_error", "קובץ הכללים לא נמצא")
    except json.JSONDecodeError:
        raise PermanentError("rules_file_error", "קובץ הכללים פגום")


def _check_rules_structure(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("benefits"), list):
        raise PermanentError("rules_file_error", "מבנה קובץ הכללים אינו תקין")
    for benefit in data["benefits"]:
        if not isinstance(benefit, dict):
            raise PermanentError("rules_file_error", "מבנה קובץ הכללים אינו תקין")
        for key in ("id", "name", "category", "source", "conditions"):
            if key not in benefit:
                raise PermanentError("rules_file_error", f"בהטבה חסר השדה {key}")
        for cond in benefit["conditions"]:
            if validate(_CONDITION_SCHEMA, cond) or cond["field"] not in FIELD_LABELS:
                raise PermanentError("rules_file_error", "תנאי זכאות לא תקין בקובץ הכללים")


class RulesStore:
    """Loads and validates the rules. `loader` may raise TransientError."""

    def __init__(self, loader: Optional[Callable[[], Dict[str, Any]]] = None, path: Path = DEFAULT_RULES_PATH):
        self._loader = loader or (lambda: _load_file(Path(path)))

    def load(self) -> Dict[str, Any]:
        data = self._loader()
        _check_rules_structure(data)
        return data

    def get_benefit(self, benefit_id: str) -> Dict[str, Any]:
        for benefit in self.load()["benefits"]:
            if benefit["id"] == benefit_id:
                return benefit
        raise PermanentError("unknown_benefit", f"הטבה לא מוכרת: {benefit_id}")


# --------------------------------------------------------------------------- #
# Eligibility logic: deterministic code, never the model
# --------------------------------------------------------------------------- #
def _evaluate(cond: Dict[str, Any], profile: Dict[str, Any]) -> Optional[bool]:
    """True / False, or None when the profile does not have the field."""
    field = cond["field"]
    if field not in profile or profile[field] is None:
        return None
    actual, expected, op = profile[field], cond["value"], cond["op"]
    if op == "==":
        return actual == expected
    if op == "<=":
        return actual <= expected
    if op == ">=":
        return actual >= expected
    if op == "<":
        return actual < expected
    return actual > expected


def _describe_failure(cond: Dict[str, Any], profile: Dict[str, Any]) -> str:
    label = FIELD_LABELS[cond["field"]]
    if isinstance(cond["value"], bool):
        return f"לא עומד בתנאי: {label}" if cond["value"] else f"לא עומד בתנאי: לא {label}"
    return f"{label} {profile[cond['field']]} אינה עומדת בתנאי: {OP_WORDS[cond['op']]} {cond['value']}"


def _describe_condition(cond: Dict[str, Any]) -> str:
    label = FIELD_LABELS[cond["field"]]
    if isinstance(cond["value"], bool):
        return label if cond["value"] else f"לא {label}"
    return f"{label} {OP_WORDS[cond['op']]} {cond['value']}"


# --------------------------------------------------------------------------- #
# Tool handlers. They receive validated arguments.
# --------------------------------------------------------------------------- #
def make_check_policy_rules(store: RulesStore) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handler(args: Dict[str, Any]) -> Dict[str, Any]:
        category = args["category"]
        benefits = []
        for benefit in store.load()["benefits"]:
            if category != "all" and benefit["category"] != category:
                continue
            benefits.append(
                {
                    "id": benefit["id"],
                    "name": benefit["name"],
                    "category": benefit["category"],
                    "required_fields": sorted({c["field"] for c in benefit["conditions"]}),
                    "conditions": [dict(c) for c in benefit["conditions"]],
                    "source": benefit["source"],
                }
            )
        if not benefits:
            raise PermanentError("unknown_category", f"לא נמצאו הטבות בקטגוריה {category}")
        return ok(benefits=benefits)

    return handler


def make_verify_user_eligibility(store: RulesStore) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handler(args: Dict[str, Any]) -> Dict[str, Any]:
        benefit = store.get_benefit(args["benefit_id"])
        profile = args["profile"]
        failed, missing = [], []
        for cond in benefit["conditions"]:
            status = _evaluate(cond, profile)
            if status is False:
                failed.append(_describe_failure(cond, profile))
            elif status is None and cond["field"] not in missing:
                missing.append(cond["field"])
        if failed:  # one failed condition is enough, even if other fields are missing
            return ok(benefit_id=benefit["id"], eligible=False, missing_fields=[], reason="; ".join(failed))
        if missing:
            labels = ", ".join(FIELD_LABELS[f] for f in missing)
            return ok(benefit_id=benefit["id"], eligible=None, missing_fields=missing, reason=f"חסר מידע: {labels}")
        conditions = ", ".join(_describe_condition(c) for c in benefit["conditions"])
        return ok(benefit_id=benefit["id"], eligible=True, missing_fields=[], reason=f"כל התנאים מתקיימים ({conditions})")

    return handler


def make_generate_rights_report(store: RulesStore) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handler(args: Dict[str, Any]) -> Dict[str, Any]:
        results = args["results"]
        if not results:
            raise PermanentError("empty_results", "אין תוצאות לבניית דוח")
        eligible = [r for r in results if r["eligible"] is True]
        lines = ["דוח זכויות", ""]
        eligible_ids: List[str] = []
        if eligible:
            lines.append("הטבות שהקוד אישר כזכאי להן:")
            for index, item in enumerate(eligible, start=1):
                benefit = store.get_benefit(item["benefit_id"])  # name and source come from the rules, not from the caller
                eligible_ids.append(benefit["id"])
                lines.append(f"{index}. {benefit['name']}: {item.get('reason', '')} (מקור: {benefit['source']})")
        else:
            lines.append("לא נמצאו הטבות שהקוד אישר כזכאי להן על סמך הנתונים הקיימים.")
        lines += ["", DISCLAIMER + "."]
        return ok(report="\n".join(lines), eligible_ids=eligible_ids)

    return handler


# --------------------------------------------------------------------------- #
# Tool specs and executor
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    kind: str = "read"  # read | compute | write
    requires_approval: bool = False
    timeout_s: float = 2.0
    max_retries: int = 2
    backoff_s: float = 0.05

    def contract(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "requires_approval": self.requires_approval,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


def make_tools(
    store: Optional[RulesStore] = None,
    timeout_s: float = 2.0,
    max_retries: int = 2,
    backoff_s: float = 0.05,
) -> Dict[str, ToolSpec]:
    store = store or RulesStore()
    common = dict(timeout_s=timeout_s, max_retries=max_retries, backoff_s=backoff_s)
    specs = [
        ToolSpec(
            "check_policy_rules",
            "שליפת כללי ההטבות מקובץ הכללים הבדוי, לפי קטגוריה (או all).",
            CHECK_POLICY_INPUT, CHECK_POLICY_OUTPUT, make_check_policy_rules(store), kind="read", **common),
        ToolSpec(
            "verify_user_eligibility",
            "חישוב זכאות להטבה אחת לפי כללים בקוד: eligible הוא true, false או null (חסר מידע).",
            VERIFY_INPUT, VERIFY_OUTPUT, make_verify_user_eligibility(store), kind="compute", **common),
        ToolSpec(
            "generate_rights_report",
            "בניית דוח מתבנית קבועה בקוד. כולל רק הטבות שהוחזרו כזכאי. מפיק טקסט בלבד, לא שולח ולא שומר.",
            REPORT_INPUT, REPORT_OUTPUT, make_generate_rights_report(store), kind="compute", **common),
    ]
    return {spec.name: spec for spec in specs}


class ToolExecutor:
    """Validates, authorizes and runs tools. Always returns a structured dict."""

    def __init__(self, specs: Dict[str, ToolSpec]):
        self.specs = specs

    def execute(self, action: Dict[str, Any], approval_given: bool = False) -> Dict[str, Any]:
        name = action.get("name") if isinstance(action, dict) else None
        spec = self.specs.get(name)
        if spec is None:  # permissions: only registered tools may run
            return err("tool_not_allowed", f"הכלי {name!r} אינו מורשה")
        args = action.get("args")
        problem = validate(spec.input_schema, args)
        if problem:
            code = "invalid_profile" if problem.startswith("args.profile") else "invalid_arguments"
            return err(code, problem)
        if spec.requires_approval and not approval_given:
            return err("approval_required", f"הכלי {name} דורש אישור משתמש")

        attempts, last = 0, None
        for attempt in range(spec.max_retries + 1):
            attempts = attempt + 1
            try:
                result = self._run_with_timeout(spec, args)
            except concurrent.futures.TimeoutError:
                last = ("timeout", "הכלי לא ענה בזמן")
            except TransientError as exc:
                last = (exc.code, str(exc))
            except PermanentError as exc:
                return {**err(exc.code, str(exc)), "attempts": attempts}  # permanent: no retry
            except Exception:  # never leak internals
                return {**err("internal_error", "שגיאה פנימית בכלי"), "attempts": attempts}
            else:
                problem = validate(spec.output_schema, result, "output")
                if problem:
                    return {**err("invalid_output", problem), "attempts": attempts}
                return {**result, "attempts": attempts}
            if attempt < spec.max_retries:
                time.sleep(spec.backoff_s)
        return {**err(last[0], last[1], retryable=True), "attempts": attempts}

    @staticmethod
    def _run_with_timeout(spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(spec.handler, args).result(timeout=spec.timeout_s)
        finally:
            pool.shutdown(wait=False)
