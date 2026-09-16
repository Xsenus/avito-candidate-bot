import pytest

from avito_bot.conversation import ConversationState
from avito_bot.storage import SQLiteStateStore


def test_state_and_processed_messages_survive_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = SQLiteStateStore(path)
    state = ConversationState(step="ready_to_submit", application_status="pending")
    state.phone = "+79916410399"
    first.save("chat-1", state)
    first.mark_processed("chat-1", "message-1")
    first.close()

    second = SQLiteStateStore(path)
    restored = second.load("chat-1")

    assert restored.phone == "+79916410399"
    assert second.is_processed("chat-1", "message-1")
    assert second.pending()[0][0] == "chat-1"
    second.close()


def test_bot_outgoing_message_ids_survive_reopen(tmp_path):
    path = tmp_path / "bot-outgoing.sqlite3"
    first = SQLiteStateStore(path)
    first.mark_bot_outgoing("chat-1", "outgoing-1")
    first.close()

    second = SQLiteStateStore(path)

    assert second.is_bot_outgoing("chat-1", "outgoing-1")
    assert not second.is_bot_outgoing("chat-1", "manual-1")
    second.close()


def test_interrupted_submission_is_quarantined_without_retry(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    store = SQLiteStateStore(path)
    store.save(
        "chat-interrupted",
        ConversationState(
            step="ready_to_submit",
            application_status="submitting",
            next_retry_at="2026-07-21T00:00:00+00:00",
        ),
    )

    assert store.quarantine_interrupted_submissions() == 1
    restored = store.load("chat-interrupted")
    assert restored.application_status == "uncertain"
    assert restored.next_retry_at is None
    assert "проверьте заявку вручную" in (restored.last_error or "")
    assert store.pending() == []
    assert store.quarantine_interrupted_submissions() == 0
    store.close()


def test_all_conversations_returns_persisted_states(tmp_path):
    store = SQLiteStateStore(tmp_path / "all.sqlite3")
    store.save("chat-1", ConversationState(step="awaiting_phone"))
    store.save("chat-2", ConversationState(step="done", application_status="completed"))

    conversations = dict(store.all_conversations())

    assert conversations["chat-1"].step == "awaiting_phone"
    assert conversations["chat-2"].application_status == "completed"
    store.close()


def test_account_binding_restores_each_account_snapshot_on_return(tmp_path):
    store = SQLiteStateStore(tmp_path / "account-switch.sqlite3")
    store.save(
        "old-chat",
        ConversationState(
            step="awaiting_datetime",
            application_status="collecting",
            reminder_due_at="2026-09-16T10:00:00+00:00",
        ),
    )
    store.mark_message_seen("old-chat", "incoming-1", 1.0)
    store.mark_bot_outgoing("old-chat", "outgoing-1")
    store.set_metadata("message_history_cursor_initialized_v2", "true")
    store.set_metadata("unanswered_recovery_started_at_v1", "1")

    assert store.bind_account("account-a") == (False, 0, 0)
    assert store.load("old-chat").step == "awaiting_datetime"
    assert store.bind_account("account-a") == (False, 0, 0)

    assert store.bind_account("account-b") == (True, 1, 0)
    assert store.all_conversations() == []
    assert not store.is_processed("old-chat", "incoming-1")
    assert not store.is_bot_outgoing("old-chat", "outgoing-1")
    assert store.get_message_cursor("old-chat") is None
    assert store.get_metadata("message_history_cursor_initialized_v2") is None
    assert store.get_metadata("unanswered_recovery_started_at_v1") is None

    store.save(
        "new-chat",
        ConversationState(step="awaiting_phone", application_status="collecting"),
    )
    store.mark_message_seen("new-chat", "incoming-2", 2.0)
    store.mark_bot_outgoing("new-chat", "outgoing-2")
    store.set_metadata("message_history_cursor_initialized_v2", "account-b-ready")
    store.set_metadata("manual_takeover_started_at_v1", "2")

    assert store.bind_account("account-a") == (True, 1, 1)
    assert store.load("old-chat").step == "awaiting_datetime"
    assert store.is_processed("old-chat", "incoming-1")
    assert store.is_bot_outgoing("old-chat", "outgoing-1")
    assert store.get_message_cursor("old-chat") == (1.0, "incoming-1")
    assert store.get_metadata("message_history_cursor_initialized_v2") == "true"
    assert store.get_metadata("unanswered_recovery_started_at_v1") == "1"
    assert not store.is_processed("new-chat", "incoming-2")
    assert store.get_metadata("manual_takeover_started_at_v1") is None

    assert store.bind_account("account-b") == (True, 1, 1)
    assert store.load("new-chat").step == "awaiting_phone"
    assert store.is_processed("new-chat", "incoming-2")
    assert store.is_bot_outgoing("new-chat", "outgoing-2")
    assert store.get_message_cursor("new-chat") == (2.0, "incoming-2")
    assert (
        store.get_metadata("message_history_cursor_initialized_v2")
        == "account-b-ready"
    )
    assert store.get_metadata("manual_takeover_started_at_v1") == "2"
    assert not store.is_processed("old-chat", "incoming-1")
    assert store.get_metadata("unanswered_recovery_started_at_v1") is None

    archived = store._connection.execute(
        """
        SELECT account_fingerprint, chat_id
        FROM archived_conversations
        ORDER BY account_fingerprint, chat_id
        """
    ).fetchall()
    assert archived == [
        ("account-a", "old-chat"),
        ("account-b", "new-chat"),
    ]
    store.close()


def test_account_binding_rejects_empty_fingerprint(tmp_path):
    store = SQLiteStateStore(tmp_path / "empty-account.sqlite3")
    try:
        with pytest.raises(ValueError, match="fingerprint"):
            store.bind_account("  ")
    finally:
        store.close()
