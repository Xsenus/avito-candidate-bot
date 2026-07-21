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
