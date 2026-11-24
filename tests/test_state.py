"""State tests: schema, separation between conversations, context modes."""
import pytest

from src.agent import build_agent
from src.state import ConversationState, SessionStore, build_context, context_size


def test_new_state_matches_its_schema():
    state = ConversationState(conversation_id="c1")
    assert state.validate() is None
    assert state.status == "active" and state.profile == {} and state.messages == []


def test_state_with_wrong_type_is_detected():
    state = ConversationState(conversation_id="c1")
    state.clarifications = "two"  # type: ignore
    assert "clarifications" in state.validate()
    state = ConversationState(conversation_id="c1")
    state.profile = {"ssn": "123"}  # a field outside the schema
    assert "profile" in state.validate()


def test_update_profile_validates_and_invalidates_old_results():
    state = ConversationState(conversation_id="c1")
    assert state.update_profile({"age": 26}) is True
    state.record_verification({"benefit_id": "work_grant", "eligible": None, "missing_fields": ["employed"], "reason": "x"})
    state.report = "old report"
    assert state.update_profile({"age": 26}) is False and state.verified  # nothing changed, nothing cleared
    assert state.update_profile({"age": 27}) is True and state.verified == {} and state.report is None
    with pytest.raises(ValueError):
        state.update_profile({"age": "twenty"})
    assert state.profile == {"age": 27}


def test_two_conversations_do_not_mix():
    """Proof that the state of two separate conversations stays separate."""
    store = SessionStore()
    a, b = store.get("conv-a"), store.get("conv-b")
    assert a is not b and store.get("conv-a") is a
    a.update_profile({"age": 26, "is_student": True, "monthly_income": 4000})
    a.add_message("user", "הודעה של שיחה א")
    assert b.profile == {} and b.messages == [] and b.turn == 0
    b.update_profile({"age": 40, "monthly_income": 9000})
    assert a.profile["age"] == 26 and a.profile["monthly_income"] == 4000
    assert a.validate() is None and b.validate() is None


def test_two_conversations_do_not_mix_through_the_agent():
    agent = build_agent()
    agent.handle_message("rich", "גיל 40, לא סטודנט, מועסק, הכנסה 9,000")
    agent.handle_message("student", "גיל 26, סטודנט, מועסק, הכנסה 4,000")
    rich, student = agent.store.get("rich"), agent.store.get("student")
    assert rich.eligible_ids == [] and sorted(student.eligible_ids) == ["arnona_discount", "student_transport", "work_grant"]
    assert rich.profile["age"] == 40 and student.profile["age"] == 26
    assert {m["content"] for m in rich.messages}.isdisjoint({m["content"] for m in student.messages if m["role"] == "user"})


def test_compact_context_is_smaller_and_keeps_the_profile():
    agent = build_agent()
    agent.handle_message("c", "גיל 26, סטודנט, מועסק, הכנסה 5,001")
    state = agent.store.get("c")
    state.add_message("user", "למה לא קיבלתי הנחה בארנונה?")
    full, compact = build_context(state, "full"), build_context(state, "compact")
    assert context_size(compact)["chars"] < context_size(full)["chars"]
    assert context_size(compact)["messages"] < context_size(full)["messages"]
    assert "5001" in compact["messages"][0]["content"]  # the structured profile survives the summary
    assert all(m["role"] != "tool" for m in compact["messages"])  # old tool outputs are dropped


def test_unknown_context_mode_is_rejected():
    with pytest.raises(ValueError):
        build_context(ConversationState(conversation_id="c"), "tiny")
