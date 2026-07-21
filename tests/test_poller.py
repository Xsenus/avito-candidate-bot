from avito_bot.conversation import ConversationState
from avito_bot.invitations import InvitationCatalog
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from poller import (
    extract_chat_context,
    initialize_message_cursor,
    iter_new_chat_messages,
    process_chat_message,
)


def chat(direction="in"):
    return {
        "id": "chat-1",
        "context": {
            "type": "item",
            "value": {
                "id": 8288057518,
                "location": {"title": "Кемерово"},
            },
        },
        "last_message": {
            "id": "message-1",
            "direction": direction,
            "type": "text",
            "content": {"text": "четверг"},
        },
    }


def test_context_uses_item_location_from_live_avito_shape():
    assert extract_chat_context(chat()) == ("Кемерово", "8288057518")


def test_only_incoming_unprocessed_messages_are_yielded(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    incoming = chat()
    incoming_client = FakeHistoryClient([incoming["last_message"]])
    assert len(list(iter_new_chat_messages(incoming_client, [incoming], store))) == 1

    outgoing = chat(direction="out")
    outgoing_client = FakeHistoryClient([outgoing["last_message"]])
    assert len(list(iter_new_chat_messages(outgoing_client, [outgoing], store))) == 0
    store.mark_processed("chat-1", "message-1")
    assert len(list(iter_new_chat_messages(incoming_client, [incoming], store))) == 0
    store.close()


class FakeHistoryClient:
    def __init__(self, messages):
        self.messages = messages
        self.requested_chats = []

    def get_messages(self, chat_id, *, limit=100):
        self.requested_chats.append((chat_id, limit))
        return self.messages


def test_rapid_name_and_phone_messages_are_yielded_oldest_first(tmp_path):
    store = SQLiteStateStore(tmp_path / "rapid.sqlite3")
    store.save("chat-1", ConversationState(step="awaiting_full_name"))
    messages = [
        {
            "id": "message-phone",
            "created": 200,
            "direction": "in",
            "type": "text",
            "content": {"text": "8 927 206-97-01"},
        },
        {
            "id": "message-name",
            "created": 100,
            "direction": "in",
            "type": "text",
            "content": {"text": "Травкин Виталий"},
        },
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[0]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert [values[3] for values in yielded] == ["message-name", "message-phone"]
    assert yielded[0][1] is yielded[1][1]
    store.close()


def test_history_bootstrap_marks_every_recent_message(tmp_path):
    store = SQLiteStateStore(tmp_path / "bootstrap.sqlite3")
    messages = [
        {"id": "newest", "direction": "in", "type": "text"},
        {"id": "older", "direction": "in", "type": "text"},
    ]
    client = FakeHistoryClient(messages)

    initialize_message_cursor(client, store, [chat()])

    assert store.is_processed("chat-1", "newest")
    assert store.is_processed("chat-1", "older")
    assert store.get_metadata("message_history_cursor_initialized_v2") == "true"
    store.close()


class FakeClient:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class FakeForm:
    def __init__(self):
        self.applications = []

    def submit(self, application):
        self.applications.append(application)


class FakeInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Кемерово","Приглашение на ДАТА, Кемерово"\n'
        )


def test_phone_triggers_form_then_invitation_and_persists_completion(tmp_path):
    store = SQLiteStateStore(tmp_path / "workflow.sqlite3")
    state = ConversationState(
        step="awaiting_phone",
        city="Кемерово",
        item_id="8288057518",
        last_name="Травкин",
        first_name="Виталий",
        full_name="Травкин Виталий",
        internship_date="23.07.2026",
    )
    client = FakeClient()
    form = FakeForm()
    workflow = CandidateWorkflow(form, FakeInvitationSource())

    process_chat_message(
        client,
        workflow,
        store,
        "chat-1",
        state,
        {"content": {"text": "8 (927) 206-97-01"}},
        "message-1",
        "Кемерово",
        "8288057518",
    )

    restored = store.load("chat-1")
    assert form.applications[0].phone == "+79272069701"
    assert client.messages[0][1].startswith("Спасибо, данные получили")
    assert client.messages[1][1] == "Приглашение на 23.07.2026, Кемерово"
    assert restored.application_status == "completed"
    assert restored.step == "done"
    assert store.is_processed("chat-1", "message-1")
    store.close()
