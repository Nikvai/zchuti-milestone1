"""Tool tests: valid input, invalid input and transient failure for every tool.

They run with one command (pytest), without an external service. The failing
rules source is a MOCK (src/mocks.py).
"""
from src.mocks import FlakyLoader
from src.tools import RulesStore, ToolExecutor, ToolSpec, make_tools

GOOD_PROFILE = {"age": 26, "is_student": True, "employed": True, "monthly_income": 4000}


def executor(loader=None, **kwargs):
    store = RulesStore(loader=loader) if loader else RulesStore()
    kwargs.setdefault("backoff_s", 0.0)
    return ToolExecutor(make_tools(store, **kwargs))


def call(ex, name, args):
    return ex.execute({"name": name, "args": args})


# ---- check_policy_rules ------------------------------------------------------
def test_check_policy_rules_valid():
    result = call(executor(), "check_policy_rules", {"category": "all"})
    assert result["ok"] is True
    assert {b["id"] for b in result["benefits"]} == {"student_transport", "arnona_discount", "work_grant"}
    assert result["attempts"] == 1


def test_check_policy_rules_invalid_category():
    result = call(executor(), "check_policy_rules", {"category": "pensions"})
    assert result["ok"] is False and result["error"]["code"] == "invalid_arguments"
    extra = call(executor(), "check_policy_rules", {"category": "all", "debug": True})
    assert extra["error"]["code"] == "invalid_arguments"


def test_check_policy_rules_transient_failure_recovers_after_retry():
    flaky = FlakyLoader(failures=2)
    result = call(executor(flaky), "check_policy_rules", {"category": "arnona"})
    assert result["ok"] is True and result["attempts"] == 3 and flaky.calls == 3


def test_check_policy_rules_transient_failure_exhausts_retries():
    result = call(executor(FlakyLoader(failures=None)), "check_policy_rules", {"category": "all"})
    assert result["ok"] is False
    assert result["error"] == {"code": "temporary_unavailable", "message": "המאגר אינו זמין זמנית", "retryable": True}
    assert result["attempts"] == 3  # one try and two retries


def test_check_policy_rules_timeout_is_a_transient_error():
    slow = FlakyLoader(failures=None, mode="slow", delay_s=0.3)
    result = call(executor(slow, timeout_s=0.05, max_retries=1), "check_policy_rules", {"category": "all"})
    assert result["error"]["code"] == "timeout" and result["error"]["retryable"] is True


def test_permanent_error_is_not_retried():
    broken = FlakyLoader(failures=0, inner=lambda: {"unexpected": "shape"})
    result = call(executor(broken), "check_policy_rules", {"category": "all"})
    assert result["error"]["code"] == "rules_file_error" and result["error"]["retryable"] is False
    assert result["attempts"] == 1 and broken.calls == 1


# ---- verify_user_eligibility ---------------------------------------------------
def test_verify_valid_all_three_values():
    ex = executor()
    yes = call(ex, "verify_user_eligibility", {"benefit_id": "arnona_discount", "profile": GOOD_PROFILE})
    assert yes["eligible"] is True and yes["missing_fields"] == []
    no = call(ex, "verify_user_eligibility",
              {"benefit_id": "arnona_discount", "profile": {**GOOD_PROFILE, "monthly_income": 5001}})
    assert no["eligible"] is False and "5001" in no["reason"]
    unknown = call(ex, "verify_user_eligibility",
                   {"benefit_id": "arnona_discount", "profile": {"age": 26, "is_student": True}})
    assert unknown["eligible"] is None and unknown["missing_fields"] == ["monthly_income"]


def test_verify_boundary_exactly_on_threshold():
    ex = executor()
    on_limit = call(ex, "verify_user_eligibility",
                    {"benefit_id": "arnona_discount", "profile": {"is_student": True, "monthly_income": 5000}})
    assert on_limit["eligible"] is True


def test_verify_one_failed_condition_beats_missing_fields():
    ex = executor()
    result = call(ex, "verify_user_eligibility", {"benefit_id": "work_grant", "profile": {"age": 20}})
    assert result["eligible"] is False  # age 20 < 23, so no need to ask about the rest


def test_verify_invalid_input():
    ex = executor()
    assert call(ex, "verify_user_eligibility", {"benefit_id": "nope", "profile": {}})["error"]["code"] == "unknown_benefit"
    bad_type = call(ex, "verify_user_eligibility", {"benefit_id": "arnona_discount", "profile": {"age": "26"}})
    assert bad_type["error"]["code"] == "invalid_profile"
    negative = call(ex, "verify_user_eligibility", {"benefit_id": "arnona_discount", "profile": {"monthly_income": -5}})
    assert negative["error"]["code"] == "invalid_profile"
    bool_as_number = call(ex, "verify_user_eligibility", {"benefit_id": "arnona_discount", "profile": {"age": True}})
    assert bool_as_number["error"]["code"] == "invalid_profile"
    extra = call(ex, "verify_user_eligibility",
                 {"benefit_id": "arnona_discount", "profile": {"id_number": "123"}})
    assert extra["error"]["code"] == "invalid_profile"  # no field outside the schema is accepted


def test_verify_transient_failure():
    flaky = FlakyLoader(failures=1)
    ok_result = call(executor(flaky), "verify_user_eligibility", {"benefit_id": "work_grant", "profile": GOOD_PROFILE})
    assert ok_result["ok"] is True and ok_result["attempts"] == 2
    failed = call(executor(FlakyLoader(failures=None)), "verify_user_eligibility",
                  {"benefit_id": "work_grant", "profile": GOOD_PROFILE})
    assert failed["ok"] is False and failed["error"]["retryable"] is True


# ---- generate_rights_report ----------------------------------------------------
def test_report_valid_contains_only_eligible_benefits():
    results = [
        {"benefit_id": "student_transport", "eligible": True, "reason": "כל התנאים מתקיימים"},
        {"benefit_id": "arnona_discount", "eligible": False, "reason": "הכנסה חריגה"},
        {"benefit_id": "work_grant", "eligible": None, "missing_fields": ["employed"], "reason": "חסר מידע"},
    ]
    result = call(executor(), "generate_rights_report", {"results": results})
    assert result["ok"] is True and result["eligible_ids"] == ["student_transport"]
    assert "הנחה בתחבורה" in result["report"] and "ארנונה" not in result["report"] and "מענק" not in result["report"]
    assert "הערכה ראשונית לצורכי הדגמה בלבד" in result["report"]


def test_report_invalid_input():
    ex = executor()
    assert call(ex, "generate_rights_report", {"results": []})["error"]["code"] == "empty_results"
    assert call(ex, "generate_rights_report", {"results": [{"benefit_id": "x"}]})["error"]["code"] == "invalid_arguments"
    unknown = call(ex, "generate_rights_report", {"results": [{"benefit_id": "ghost", "eligible": True}]})
    assert unknown["error"]["code"] == "unknown_benefit"  # names come from the rules, not from the caller


def test_report_transient_failure():
    results = [{"benefit_id": "work_grant", "eligible": True, "reason": "ok"}]
    assert call(executor(FlakyLoader(failures=1)), "generate_rights_report", {"results": results})["ok"] is True
    failed = call(executor(FlakyLoader(failures=None)), "generate_rights_report", {"results": results})
    assert failed["ok"] is False and failed["error"]["retryable"] is True


# ---- permissions and contracts ---------------------------------------------------
def test_unknown_tool_is_not_allowed():
    result = call(executor(), "send_email", {"to": "someone"})
    assert result["error"]["code"] == "tool_not_allowed"


def test_tool_that_requires_approval_does_not_run_without_it():
    ran = []
    spec = ToolSpec("write_demo", "demo", {"type": "object", "properties": {}, "additionalProperties": False},
                    {"type": "object"}, lambda a: ran.append(1) or {"ok": True}, kind="write", requires_approval=True)
    ex = ToolExecutor({"write_demo": spec})
    assert ex.execute({"name": "write_demo", "args": {}})["error"]["code"] == "approval_required" and not ran
    assert ex.execute({"name": "write_demo", "args": {}}, approval_given=True)["ok"] is True and ran


def test_invalid_output_is_rejected_by_the_contract():
    spec = ToolSpec("bad", "returns wrong shape", {"type": "object"}, {"type": "object", "required": ["x"]},
                    lambda a: {"ok": True})
    result = ToolExecutor({"bad": spec}).execute({"name": "bad", "args": {}})
    assert result["error"]["code"] == "invalid_output"


def test_every_tool_has_a_complete_contract_and_none_writes():
    for spec in make_tools().values():
        contract = spec.contract()
        assert contract["name"] and contract["description"]
        assert contract["input_schema"]["type"] == "object" and contract["output_schema"]["type"] == "object"
        assert spec.kind in ("read", "compute") and spec.requires_approval is False
