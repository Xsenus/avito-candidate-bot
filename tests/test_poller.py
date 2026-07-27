import pytest

from avito_bot.conversation import INITIAL_MESSAGE, ConversationState
from avito_bot.invitations import InvitationCatalog
from avito_bot.regional_locations import RegionalLocationCatalog
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from poller import (
    extract_chat_context,
    infer_step_from_bot_message,
    initialize_message_cursor,
    is_job_application_system_message,
    iter_new_chat_messages,
    iter_unanswered_job_applications,
    migrate_legacy_completed_chats,
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


def job_application_message(message_id, created, flow_id):
    return {
        "id": message_id,
        "created": created,
        "direction": "in",
        "type": "system",
        "content": {"text": "system application", "flow_id": flow_id},
    }


def test_new_job_application_system_pair_starts_only_once(tmp_path):
    store = SQLiteStateStore(tmp_path / "system-application.sqlite3")
    messages = [
        job_application_message("enrichment", 200, "job_apply_enrichment"),
        job_application_message("job", 100, "job"),
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[0]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert [values[3] for values in yielded] == ["enrichment"]
    assert is_job_application_system_message(messages[0])
    assert store.is_processed("chat-1", "job")
    store.close()


def test_new_job_application_uses_newest_trigger_when_chat_preview_is_stale(
    tmp_path,
):
    store = SQLiteStateStore(tmp_path / "stale-chat-preview.sqlite3")
    messages = [
        job_application_message("enrichment", 200, "job_apply_enrichment"),
        job_application_message("job", 100, "job"),
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[1]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert [values[3] for values in yielded] == ["enrichment"]
    assert store.is_processed("chat-1", "job")
    store.close()


def test_unrelated_system_message_is_ignored(tmp_path):
    store = SQLiteStateStore(tmp_path / "unrelated-system.sqlite3")
    message = job_application_message("unrelated", 100, "some_other_flow")
    candidate_chat = chat()
    candidate_chat["last_message"] = message

    assert not list(
        iter_new_chat_messages(
            FakeHistoryClient([message]), [candidate_chat], store
        )
    )
    assert store.is_processed("chat-1", "unrelated")
    store.close()


def test_recent_unanswered_application_can_be_recovered(tmp_path):
    from datetime import datetime, timezone

    store = SQLiteStateStore(tmp_path / "recover-system.sqlite3")
    message = job_application_message("enrichment", 1_000, "job_apply_enrichment")
    candidate_chat = chat()
    candidate_chat["last_message"] = message
    store.mark_message_seen("chat-1", "enrichment", 1_000)

    recovered = list(
        iter_unanswered_job_applications(
            FakeHistoryClient([message]),
            [candidate_chat],
            store,
            now=datetime.fromtimestamp(1_100, timezone.utc),
        )
    )

    assert [values[3] for values in recovered] == ["enrichment"]
    store.close()


def test_unanswered_application_before_installation_boundary_is_not_recovered(
    tmp_path,
):
    from datetime import datetime, timezone

    store = SQLiteStateStore(tmp_path / "recover-boundary.sqlite3")
    message = job_application_message("old-job", 1_000, "job")
    candidate_chat = chat()
    candidate_chat["last_message"] = message

    recovered = list(
        iter_unanswered_job_applications(
            FakeHistoryClient([message]),
            [candidate_chat],
            store,
            now=datetime.fromtimestamp(1_200, timezone.utc),
            not_before_timestamp=1_100,
        )
    )

    assert recovered == []
    store.close()


def test_preinstallation_chat_discovered_later_is_quarantined(tmp_path):
    store = SQLiteStateStore(tmp_path / "late-history.sqlite3")
    messages = [
        job_application_message("old-job", 1_000, "job"),
        {
            "id": "later-answer",
            "created": 1_200,
            "direction": "in",
            "type": "text",
            "content": {"text": "да"},
        },
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[-1]

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient(messages),
            [candidate_chat],
            store,
            not_before_timestamp=1_100,
        )
    )

    state = store.load("chat-1")
    assert yielded == []
    assert state.step == "done"
    assert state.application_status == "manual"
    assert state.notes["preinstallation_chat_quarantined"] == "true"
    assert all(store.is_processed("chat-1", message["id"]) for message in messages)
    store.close()


def test_postinstallation_application_discovered_later_is_processed(tmp_path):
    store = SQLiteStateStore(tmp_path / "late-new-application.sqlite3")
    message = job_application_message("new-job", 1_200, "job")
    candidate_chat = chat()
    candidate_chat["last_message"] = message

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient([message]),
            [candidate_chat],
            store,
            not_before_timestamp=1_100,
        )
    )

    assert [values[3] for values in yielded] == ["new-job"]
    assert store.load("chat-1").application_status == "collecting"
    store.close()


def test_recovery_skips_chat_with_an_outgoing_reply(tmp_path):
    from datetime import datetime, timezone

    store = SQLiteStateStore(tmp_path / "recover-replied.sqlite3")
    messages = [
        {
            "id": "reply",
            "created": 1_100,
            "direction": "out",
            "type": "text",
            "content": {"text": "already answered"},
        },
        job_application_message("job", 1_000, "job"),
    ]
    candidate_chat = chat(direction="out")
    candidate_chat["last_message"] = messages[0]

    assert not list(
        iter_unanswered_job_applications(
            FakeHistoryClient(messages),
            [candidate_chat],
            store,
            now=datetime.fromtimestamp(1_200, timezone.utc),
        )
    )
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


def test_existing_database_migrates_latest_legacy_final_message(tmp_path):
    store = SQLiteStateStore(tmp_path / "existing-legacy.sqlite3")
    store.set_metadata("message_history_cursor_initialized_v2", "true")
    store.save("chat-1", ConversationState(step="awaiting_phone"))
    messages = [
        {
            "id": "legacy-final",
            "created": 200,
            "direction": "out",
            "type": "text",
            "content": {"text": "Готово, вы записаны! Адрес пришлю позднее."},
        },
        {
            "id": "phone-prompt",
            "created": 100,
            "direction": "out",
            "type": "text",
            "content": {"text": "И номер"},
        },
    ]

    changed = migrate_legacy_completed_chats(
        FakeHistoryClient(messages), store, [chat(direction="out")]
    )

    restored = store.load("chat-1")
    assert changed == 1
    assert restored.step == "done"
    assert restored.application_status == "completed"
    assert store.get_metadata("legacy_completed_chats_migrated_v1") == "true"
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


def regional_catalog():
    return RegionalLocationCatalog.from_csv(
        " ,СЦ,Куда приглашать на стажировку,Время стажировки\n"
        'Кемерово,Кемерово,"Кемерово, ул. Терешковой д.41/11",8:00:00\n'
        'Краснодар,Краснодар,"г. Краснодар, х. Октябрьский, '
        'ул. Подсолнечная, 44",10:30:00\n'
    )


def test_regional_application_immediately_sends_three_messages(tmp_path):
    store = SQLiteStateStore(tmp_path / "regional-intro.sqlite3")
    client = FakeClient()
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())
    message = job_application_message("regional-job", 100, "job")

    process_chat_message(
        client,
        workflow,
        store,
        "chat-regional",
        ConversationState(),
        message,
        "regional-job",
        "Краснодар",
        "8259136221",
        regional_locations=regional_catalog(),
    )

    restored = store.load("chat-regional")
    assert len(client.messages) == 3
    assert client.messages[0][1].startswith("1. 🚚 Водитель")
    assert client.messages[1][1].startswith("🛠 О работе")
    assert "ул. Подсолнечная, 44" in client.messages[2][1]
    assert "10:30:00" in client.messages[2][1]
    assert restored.step == "awaiting_datetime"
    assert restored.service_center == "Краснодар"
    assert restored.warehouse_selection_source == "regional_catalog"
    assert restored.address.endswith("ул. Подсолнечная, 44")
    assert restored.internship_time == "10:30:00"
    assert restored.regional_intro_messages_sent == 3
    assert restored.regional_intro_trigger_message_id is None
    assert store.is_processed("chat-regional", "regional-job")
    store.close()


def test_regional_intro_resumes_after_interrupted_send(tmp_path):
    class InterruptedClient(FakeClient):
        def send_message(self, chat_id, text):
            if len(self.messages) == 1:
                raise RuntimeError("temporary Avito failure")
            super().send_message(chat_id, text)

    store = SQLiteStateStore(tmp_path / "regional-resume.sqlite3")
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())
    message = job_application_message("regional-job", 100, "job")
    interrupted = InterruptedClient()

    with pytest.raises(RuntimeError, match="temporary Avito failure"):
        process_chat_message(
            interrupted,
            workflow,
            store,
            "chat-regional",
            ConversationState(),
            message,
            "regional-job",
            "Кемерово",
            "item",
            regional_locations=regional_catalog(),
        )

    partial = store.load("chat-regional")
    assert partial.step == "sending_regional_intro"
    assert partial.regional_intro_messages_sent == 1
    assert not store.is_processed("chat-regional", "regional-job")

    resumed = FakeClient()
    process_chat_message(
        resumed,
        workflow,
        store,
        "chat-regional",
        partial,
        message,
        "regional-job",
        "Кемерово",
        "item",
        regional_locations=regional_catalog(),
    )

    restored = store.load("chat-regional")
    assert len(resumed.messages) == 2
    assert resumed.messages[0][1].startswith("🛠 О работе")
    assert resumed.messages[1][1].startswith("Подобрали для вас склад")
    assert restored.step == "awaiting_datetime"
    assert restored.regional_intro_messages_sent == 3
    assert store.is_processed("chat-regional", "regional-job")
    store.close()


def test_moscow_application_keeps_existing_flow(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "moscow-existing-flow.sqlite3")
    client = FakeClient()
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())
    delayed = []
    monkeypatch.setattr(
        "poller.schedule_delayed_message",
        lambda client, chat_id, text, delay: delayed.append((chat_id, text, delay)),
    )
    message = job_application_message("moscow-job", 100, "job")

    process_chat_message(
        client,
        workflow,
        store,
        "chat-moscow-existing",
        ConversationState(),
        message,
        "moscow-job",
        "Москва",
        "moscow-item",
        regional_locations=regional_catalog(),
    )

    restored = store.load("chat-moscow-existing")
    assert len(client.messages) == 1
    assert client.messages[0][1] == INITIAL_MESSAGE
    assert len(delayed) == 1
    assert restored.step == "awaiting_interest"
    assert restored.regional_intro_messages_sent == 0
    store.close()


def test_regional_journey_uses_catalog_center_in_form(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "regional-journey.sqlite3")
    client = FakeClient()
    form = FakeForm()
    workflow = CandidateWorkflow(form, FakeInvitationSource())
    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date",
        lambda value: __import__("datetime").date(2026, 7, 30),
    )

    messages = [
        job_application_message("regional-job", 100, "job"),
        {"content": {"text": "четверг"}},
        {"content": {"text": "Иванов Иван"}},
        {"content": {"text": "8 999 123-45-67"}},
    ]
    for index, message in enumerate(messages):
        process_chat_message(
            client,
            workflow,
            store,
            "chat-regional-journey",
            store.load("chat-regional-journey"),
            message,
            str(message.get("id") or f"regional-answer-{index}"),
            "Кемерово",
            "regional-item",
            regional_locations=regional_catalog(),
        )

    restored = store.load("chat-regional-journey")
    assert restored.step == "done"
    assert restored.application_status == "completed"
    assert restored.service_center == "Кемерово"
    assert restored.internship_time == "8:00:00"
    assert form.applications[0].warehouse == "СЦ Кемерово"
    assert form.applications[0].internship_date == "30.07.2026"
    assert client.messages[-1][1].startswith(
        "Приглашение на 30.07., Кемерово"
    )
    store.close()


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
    assert client.messages[1][1].startswith("Приглашение на 23.07., Кемерово")
    assert client.messages[1][1].endswith("До встречи!")
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
    assert client.messages[-1][1].startswith("Приглашение на 23.07., Кемерово")
    assert client.messages[-1][1].endswith("До встречи!")
    assert len(delayed) == 1
    assert all(store.is_processed("chat-journey", f"message-{i}") for i in range(1, 7))
    store.close()


def test_moscow_journey_persists_selected_warehouse_and_uses_it_in_form(
    tmp_path, monkeypatch
):
    class MoscowInvitationSource:
        def load(self):
            return InvitationCatalog.from_csv(
                '"СЦ","Текст сообщения"\n'
                '"Печатники","Вы записаны ДАТА на склад Печатники"\n'
            )

    store = SQLiteStateStore(tmp_path / "moscow-journey.sqlite3")
    client = FakeClient()
    form = FakeForm()
    workflow = CandidateWorkflow(form, MoscowInvitationSource())
    monkeypatch.setattr("poller.schedule_delayed_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date",
        lambda value: __import__("datetime").date(2026, 7, 23),
    )

    answers = [
        "Здравствуйте",
        "Да",
        "Да",
        "4",
        "четверг",
        "Иванов Иван",
        "8 999 123-45-67",
    ]
    for index, answer in enumerate(answers, start=1):
        process_chat_message(
            client,
            workflow,
            store,
            "chat-moscow",
            store.load("chat-moscow"),
            {"content": {"text": answer}},
            f"moscow-message-{index}",
            "Москва",
            "moscow-item",
        )

    restored = store.load("chat-moscow")
    assert restored.step == "done"
    assert restored.application_status == "completed"
    assert restored.warehouse_choice == 4
    assert restored.warehouse_selection_source == "candidate"
    assert restored.service_center == "Печатники"
    assert restored.address == "Курьяновская набережная, 6с2"
    assert form.applications[0].warehouse == "СЦ Печатники"
    assert client.messages[-1][1].startswith("Вы записаны 23.07. на склад Печатники")
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
