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
