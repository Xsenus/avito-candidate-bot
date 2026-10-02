from datetime import date

import pytest

from avito_bot.candidate import InternshipDateError, resolve_internship_date, validate_internship_date
from avito_bot.conversation import ConversationState, handle_user_message
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from poller import complete_pending_application, recover_invalid_date_failures


@pytest.mark.parametrize("raw", ["11.11.2070", "24.09.2926", "11/10/2099", "30-12-2030"])
def test_future_year_is_rejected_before_form(raw):
    with pytest.raises(InternshipDateError, match="24 месяцев"):
        resolve_internship_date(raw, today=date(2026, 10, 2))


def test_calendar_boundary_matches_form_month_navigation():
    assert validate_internship_date("31.10.2028", today=date(2026, 10, 2)) == date(2028, 10, 31)
    with pytest.raises(InternshipDateError):
        validate_internship_date("01.11.2028", today=date(2026, 10, 2))
    with pytest.raises(InternshipDateError):
        validate_internship_date("01.10.2026", today=date(2026, 10, 2))


@pytest.mark.parametrize("raw", ["11.11.2070", "31.04.2070"])
def test_recognizable_bad_date_does_not_handoff_to_operator(raw):
    state = ConversationState(step="awaiting_datetime")
    assert handle_user_message(state, raw)
    assert state.step == "awaiting_datetime"
    assert state.application_status == "collecting"


def blocked_state(status="submission_retry_exhausted"):
    return ConversationState(
        step="ready_to_submit", application_status=status,
        last_name="Тестов", first_name="Тест", phone="+79990000000",
        city="Кемерово", service_center="Кемерово", internship_date="11.10.2099",
        last_error="Дата стажировки должна быть в пределах ближайших 24 месяцев",
        submission_attempts=5, processing_notice_sent=True,
    )


def test_recovery_is_idempotent_and_keeps_identity(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.save("bad-date", blocked_state())
    for status in ("uncertain", "submitted", "completed", "manual", "cancelled"):
        store.save(status, blocked_state(status))
    manual = blocked_state()
    manual.manual_takeover_at = "2026-10-01T00:00:00Z"
    store.save("taken-over", manual)
    assert recover_invalid_date_failures(store) == 1
    assert recover_invalid_date_failures(store) == 0
    state = store.load("bad-date")
    assert state.step == "awaiting_datetime"
    assert state.internship_date is None
    assert state.phone == "+79990000000"
    assert state.last_name == "Тестов"
    assert state.service_center == "Кемерово"
    assert state.submission_attempts == 0
    assert not state.processing_notice_sent
    assert handle_user_message(state, "Завтра") == ""
    assert state.step == "ready_to_submit"
    assert state.application_status == "pending"
    for status in ("uncertain", "submitted", "completed", "manual", "cancelled"):
        assert store.load(status).application_status == status
    assert store.load("taken-over").application_status == "submission_retry_exhausted"


def test_invalid_pending_date_never_calls_form_or_invitation_source(tmp_path, monkeypatch):
    class NoExternalCalls:
        def load(self):
            pytest.fail("Date must be checked before catalog I/O")
        def submit(self, application):
            pytest.fail("Invalid date must not be submitted")
    workflow = CandidateWorkflow(NoExternalCalls(), NoExternalCalls())
    state = blocked_state("pending")
    with pytest.raises(InternshipDateError):
        workflow.complete(state)
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    replies = []
    monkeypatch.setattr("poller.send_bot_message", lambda *args: replies.append(args[-1]))
    assert not complete_pending_application(None, workflow, store, "date", state)
    assert len(replies) == 1
    assert "24 месяцев" in replies[0]
    assert store.load("date").step == "awaiting_datetime"
