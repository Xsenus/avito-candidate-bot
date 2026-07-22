from avito_bot.conversation import ConversationState
from avito_bot.invitations import InvitationCatalog
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from poller import (
    extract_chat_context,
    infer_step_from_bot_message,
    initialize_message_cursor,
    iter_new_chat_messages,
    process_chat_message,
    reconcile_incomplete_applications,
    restore_collected_fields,
    schedule_retry,
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
    store.mark_message_seen("chat-1", "previously-processed", 50)
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


def test_history_bootstrap_restores_prompt_and_marks_later_reply_read(tmp_path):
    from avito_bot.conversation import CONFIRMATION_MESSAGE

    store = SQLiteStateStore(tmp_path / "migration.sqlite3")
    messages = [
        {
            "id": "candidate-name",
            "created": 200,
            "direction": "in",
            "type": "text",
            "content": {"text": "Травкин Виталий"},
        },
        {
            "id": "bot-prompt",
            "created": 100,
            "direction": "out",
            "type": "text",
            "content": {"text": CONFIRMATION_MESSAGE},
        },
    ]
    client = FakeHistoryClient(messages)

    initialize_message_cursor(client, store, [chat()])

    restored = store.load("chat-1")
    assert restored.step == "awaiting_full_name"
    assert restored.city == "Кемерово"
    assert store.is_processed("chat-1", "bot-prompt")
    assert store.is_processed("chat-1", "candidate-name")
    store.close()


def test_first_seen_old_chat_processes_only_current_unread_message(tmp_path):
    store = SQLiteStateStore(tmp_path / "first-seen.sqlite3")
    messages = [
        {
            "id": "current-unread",
            "created": 300,
            "direction": "in",
            "type": "text",
            "content": {"text": "Здравствуйте"},
        },
        {
            "id": "old-candidate-message",
            "created": 200,
            "direction": "in",
            "type": "text",
            "content": {"text": "Работа только"},
        },
        {
            "id": "old-bot-message",
            "created": 100,
            "direction": "out",
            "type": "text",
            "content": {"text": "Старый ответ"},
        },
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[0]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert [values[3] for values in yielded] == ["current-unread"]
    assert store.is_processed("chat-1", "old-candidate-message")
    assert store.is_processed("chat-1", "old-bot-message")
    assert not store.is_processed("chat-1", "current-unread")
    store.close()


def test_known_bot_prompts_map_to_expected_steps():
    from avito_bot.conversation import ADDRESS_MESSAGE, INTERNSHIP_MESSAGE

    assert infer_step_from_bot_message(INTERNSHIP_MESSAGE) == "awaiting_staj"
    assert infer_step_from_bot_message(ADDRESS_MESSAGE) == "awaiting_datetime"
    assert infer_step_from_bot_message("И номер") == "awaiting_phone"
    assert (
        infer_step_from_bot_message(
            "Готово, вы записаны! Пришлю вам сюда в чат адрес в течении 30 минут."
        )
        == "done"
    )
    assert infer_step_from_bot_message("ручное сообщение") is None


def test_bootstrap_marks_legacy_completed_chat_terminal(tmp_path):
    store = SQLiteStateStore(tmp_path / "legacy-completed.sqlite3")
    final_message = {
        "id": "legacy-final",
        "created": 100,
        "direction": "out",
        "type": "text",
        "content": {
            "text": "Готово, вы записаны! Пришлю вам сюда в чат адрес в течении 30 минут."
        },
    }
    existing_chat = chat(direction="out")
    existing_chat["last_message"] = final_message

    initialize_message_cursor(
        FakeHistoryClient([final_message]), store, [existing_chat]
    )

    restored = store.load("chat-1")
    assert restored.step == "done"
    assert restored.application_status == "completed"
    store.close()


def test_legacy_history_restores_date_name_and_phone():
    from avito_bot.conversation import ADDRESS_MESSAGE, CONFIRMATION_MESSAGE

    state = ConversationState()
    messages = [
        {
            "id": "day-prompt",
            "created": 1784592000,
            "direction": "out",
            "type": "text",
            "content": {"text": ADDRESS_MESSAGE},
        },
        {
            "id": "day-answer",
            "created": 1784592060,
            "direction": "in",
            "type": "text",
            "content": {"text": "четверг"},
        },
        {
            "id": "name-prompt",
            "created": 1784592120,
            "direction": "out",
            "type": "text",
            "content": {"text": CONFIRMATION_MESSAGE},
        },
        {
            "id": "candidate-data",
            "created": 1784592180,
            "direction": "in",
            "type": "text",
            "content": {"text": "Травкин Виталий 8 927 206-97-01"},
        },
    ]

    restore_collected_fields(state, messages)

    assert state.internship_date
    assert state.last_name == "Травкин"
    assert state.first_name == "Виталий"
    assert state.phone == "+79272069701"


def test_incomplete_pending_application_is_repaired_from_history(tmp_path):
    from avito_bot.conversation import ADDRESS_MESSAGE, CONFIRMATION_MESSAGE

    store = SQLiteStateStore(tmp_path / "reconcile.sqlite3")
    store.save(
        "chat-1",
        ConversationState(
            city="Кемерово",
            application_status="pending",
            phone="+79272069701",
        ),
    )
    messages = [
        {
            "created": 1784592000,
            "direction": "out",
            "type": "text",
            "content": {"text": ADDRESS_MESSAGE},
        },
        {
            "created": 1784592060,
            "direction": "in",
            "type": "text",
            "content": {"text": "четверг"},
        },
        {
            "created": 1784592120,
            "direction": "out",
            "type": "text",
            "content": {"text": CONFIRMATION_MESSAGE},
        },
        {
            "created": 1784592180,
            "direction": "in",
            "type": "text",
            "content": {"text": "Травкин Виталий"},
        },
    ]

    repaired, returned = reconcile_incomplete_applications(
        FakeHistoryClient(messages), store
    )

    restored = store.load("chat-1")
    assert (repaired, returned) == (1, 0)
    assert restored.application_status == "pending"
    assert restored.internship_date
    assert restored.last_name == "Травкин"
    assert restored.first_name == "Виталий"
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


def test_complete_candidate_journey_survives_state_reload(tmp_path, monkeypatch):
    store = SQLiteStateStore(tmp_path / "journey.sqlite3")
    client = FakeClient()
    form = FakeForm()
    workflow = CandidateWorkflow(form, FakeInvitationSource())
    delayed = []
    monkeypatch.setattr(
        "poller.schedule_delayed_message",
        lambda client, chat_id, text, delay: delayed.append((chat_id, text, delay)),
    )
    def fixed_internship_date(value):
        if "четверг" in value.lower():
            return __import__("datetime").date(2026, 7, 23)
        raise ValueError("not a date")

    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date", fixed_internship_date
    )

    answers = [
        "Здравствуйте",
        "Да, интересно",
        "Да, готов",
        "в четверг",
        "Травкин Виталий",
        "8 (927) 206-97-01",
    ]
    for index, answer in enumerate(answers, start=1):
        process_chat_message(
            client,
            workflow,
            store,
            "chat-journey",
            store.load("chat-journey"),
            {"content": {"text": answer}},
            f"message-{index}",
            "Кемерово",
            "8288057518",
        )

    restored = store.load("chat-journey")
    assert restored.step == "done"
    assert restored.application_status == "completed"
    assert restored.last_name == "Травкин"
    assert restored.first_name == "Виталий"
    assert restored.phone == "+79272069701"
    assert restored.internship_date == "23.07.2026"
    assert len(form.applications) == 1
    assert form.applications[0].warehouse == "СЦ Кемерово"
    assert form.applications[0].tariff == "Драйв"
    assert form.applications[0].citizenship == "Российская Федерация"
    assert client.messages[-1][1] == "Приглашение на 23.07.2026, Кемерово"
    assert len(delayed) == 1
    assert all(store.is_processed("chat-journey", f"message-{i}") for i in range(1, 7))
    store.close()


def test_retries_emit_one_alert_then_stop(tmp_path, monkeypatch, capsys):
    store = SQLiteStateStore(tmp_path / "retries.sqlite3")
    state = ConversationState(
        step="ready_to_submit",
        application_status="pending",
    )
    monkeypatch.setenv("APPLICATION_ALERT_AFTER_ATTEMPTS", "2")
    monkeypatch.setenv("APPLICATION_MAX_RETRIES", "3")

    schedule_retry(store, "chat-retry", state, RuntimeError("temporary"))
    assert state.application_status == "pending"
    assert "ALERT" not in capsys.readouterr().out

    schedule_retry(store, "chat-retry", state, RuntimeError("temporary"))
    assert "ALERT repeated application failure" in capsys.readouterr().out

    schedule_retry(store, "chat-retry", state, RuntimeError("temporary"))
    assert state.application_status == "submission_retry_exhausted"
    assert state.next_retry_at is None
    assert "ALERT application retries exhausted" in capsys.readouterr().out
    assert store.pending() == []
    store.close()


def test_exhausted_invitation_retry_preserves_submitted_form_status(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "invitation-retries.sqlite3")
    state = ConversationState(
        step="ready_to_submit",
        application_status="submitted",
    )
    monkeypatch.setenv("APPLICATION_MAX_RETRIES", "1")

    schedule_retry(store, "chat-invitation", state, RuntimeError("Avito unavailable"))

    assert state.application_status == "invitation_retry_exhausted"
    assert state.next_retry_at is None
    assert store.pending() == []
    store.close()


def test_partially_seen_chat_does_not_replay_older_unprocessed_history(tmp_path):
    store = SQLiteStateStore(tmp_path / "partial.sqlite3")
    store.save("chat-1", ConversationState(step="awaiting_datetime"))
    store.mark_processed("chat-1", "known-watermark")
    messages = [
        {
            "id": "new-system-event",
            "created": 300,
            "direction": "in",
            "type": "system",
            "content": {},
        },
        {
            "id": "known-watermark",
            "created": 200,
            "direction": "in",
            "type": "system",
            "content": {},
        },
        {
            "id": "old-unprocessed-answer",
            "created": 100,
            "direction": "in",
            "type": "text",
            "content": {"text": "old answer"},
        },
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[0]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert yielded == []
    assert store.is_processed("chat-1", "old-unprocessed-answer")
    assert store.is_processed("chat-1", "new-system-event")
    assert store.get_message_cursor("chat-1") == (300.0, "new-system-event")
    store.close()


def test_completed_chat_never_restarts_from_a_new_message(tmp_path):
    store = SQLiteStateStore(tmp_path / "completed.sqlite3")
    store.save(
        "chat-1", ConversationState(step="done", application_status="completed")
    )
    store.mark_message_seen("chat-1", "old-message", 100)
    current = chat()
    current["last_message"] = {
        "id": "new-message",
        "created": 200,
        "direction": "in",
        "type": "text",
        "content": {"text": "hello again"},
    }

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient([current["last_message"]]), [current], store
        )
    )

    assert yielded == []
    assert store.is_processed("chat-1", "new-message")
    store.close()
