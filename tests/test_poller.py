import pytest

import poller as poller_module

from avito_bot.conversation import INITIAL_MESSAGE, ConversationState
from avito_bot.invitations import InvitationCatalog
from avito_bot.regional_locations import RegionalLocationCatalog
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from poller import (
    complete_pending_application,
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
    restore_terminal_reapplications,
    schedule_retry,
    send_bot_message,
)


def test_main_retries_transient_avito_failure_without_exiting(
    tmp_path, monkeypatch, capsys
):
    class StopPolling(Exception):
        pass

    class FakeClient:
        instance = None

        def __init__(self, **kwargs):
            self.get_chats_calls = 0
            FakeClient.instance = self

        def get_access_token(self):
            raise RuntimeError("temporary auth failure")

        def get_chats(self, *, unread_only, limit):
            self.get_chats_calls += 1
            if self.get_chats_calls == 1:
                raise RuntimeError("temporary Avito 500")
            return []

    class EmptyRegionalCatalog:
        def __len__(self):
            return 0

    class FakeRegionalProvider:
        def __init__(self, *args, **kwargs):
            self.catalog = EmptyRegionalCatalog()
            self.next_refresh_at = float("inf")

    class FakeWarehouseProvider:
        def __init__(self, *args, **kwargs):
            self.groups = poller_module.WAREHOUSE_GROUPS
            self.next_refresh_at = float("inf")

    sleep_calls = 0

    def stop_after_successful_retry(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise StopPolling

    for name, value in {
        "AVITO_CLIENT_ID": "test-client",
        "AVITO_CLIENT_SECRET": "test-secret",
        "AVITO_USER_ID": "test-user",
        "STATE_DB_PATH": str(tmp_path / "startup-retry.sqlite3"),
        "POLL_INTERVAL_SECONDS": "5",
        "PAUSE_ON_MANUAL_OUTGOING": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(poller_module, "AvitoClient", FakeClient)
    monkeypatch.setattr(
        poller_module, "RefreshingRegionalLocationProvider", FakeRegionalProvider
    )
    monkeypatch.setattr(
        poller_module, "RefreshingWarehouseProvider", FakeWarehouseProvider
    )
    monkeypatch.setattr(poller_module, "replace_warehouse_groups", lambda groups: None)
    monkeypatch.setattr(poller_module.time, "sleep", stop_after_successful_retry)

    with pytest.raises(StopPolling):
        poller_module.main()

    output = capsys.readouterr().out
    assert "initial Avito auth failed; polling will retry" in output
    assert "poller error: temporary Avito 500" in output
    assert "Poller startup initialization completed" in output
    assert FakeClient.instance.get_chats_calls == 2

LEGACY_DAY_PROMPT = (
    "Стажировка каждый день в 8 утра, на какой день вас записать? "
    "Укажите день недели например: Вторник"
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


def test_new_manual_outgoing_message_pauses_only_that_chat(tmp_path):
    store = SQLiteStateStore(tmp_path / "manual-takeover.sqlite3")
    store.save(
        "chat-1",
        ConversationState(
            step="awaiting_warehouse",
            application_status="collecting",
            city="Москва",
        ),
    )
    store.mark_message_seen("chat-1", "previous-message", 100)
    outgoing = {
        "id": "manager-message",
        "created": 200,
        "direction": "out",
        "type": "text",
        "content": {"text": "Дальше отвечу вручную"},
    }
    candidate_chat = chat(direction="out")
    candidate_chat["last_message"] = outgoing

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient([outgoing]),
            [candidate_chat],
            store,
            manual_takeover_after=150,
        )
    )

    restored = store.load("chat-1")
    assert yielded == []
    assert restored.step == "manual_takeover"
    assert restored.application_status == "manual"
    assert restored.manual_takeover_at
    assert restored.manual_takeover_message_id == "manager-message"
    assert store.is_processed("chat-1", "manager-message")
    store.close()


def test_manual_takeover_is_disabled_without_boundary(tmp_path):
    store = SQLiteStateStore(tmp_path / "manual-disabled.sqlite3")
    store.save(
        "chat-1",
        ConversationState(step="awaiting_warehouse", application_status="collecting"),
    )
    outgoing = {
        "id": "manager-message",
        "created": 200,
        "direction": "out",
        "type": "text",
        "content": {"text": "Ручной ответ"},
    }
    candidate_chat = chat(direction="out")
    candidate_chat["last_message"] = outgoing

    assert not list(
        iter_new_chat_messages(
            FakeHistoryClient([outgoing]),
            [candidate_chat],
            store,
        )
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_warehouse"
    assert restored.application_status == "collecting"
    assert restored.manual_takeover_message_id is None
    store.close()


def test_old_and_bot_outgoing_messages_do_not_trigger_manual_takeover(tmp_path):
    store = SQLiteStateStore(tmp_path / "manual-filtering.sqlite3")
    store.save(
        "chat-1",
        ConversationState(step="awaiting_warehouse", application_status="collecting"),
    )
    store.mark_message_seen("chat-1", "previous-message", 50)
    store.mark_bot_outgoing("chat-1", "bot-message")
    messages = [
        {
            "id": "bot-message",
            "created": 200,
            "direction": "out",
            "type": "text",
            "content": {"text": "Сообщение бота"},
        },
        {
            "id": "old-manager-message",
            "created": 100,
            "direction": "out",
            "type": "text",
            "content": {"text": "Старое сообщение менеджера"},
        },
    ]
    candidate_chat = chat(direction="out")
    candidate_chat["last_message"] = messages[0]

    assert not list(
        iter_new_chat_messages(
            FakeHistoryClient(messages),
            [candidate_chat],
            store,
            manual_takeover_after=150,
        )
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_warehouse"
    assert restored.application_status == "collecting"
    assert restored.manual_takeover_message_id is None
    assert all(store.is_processed("chat-1", message["id"]) for message in messages)
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

    assert [values[3] for values in yielded] == ["job"]
    assert not is_job_application_system_message(messages[0])
    assert is_job_application_system_message(messages[1])
    assert store.is_processed("chat-1", "enrichment")
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

    assert [values[3] for values in yielded] == ["job"]
    assert store.is_processed("chat-1", "enrichment")
    store.close()


@pytest.mark.parametrize(
    ("previous_status", "previous_step"),
    [
        ("completed", "done"),
        ("manual", "manual_takeover"),
        ("cancelled", "done"),
    ],
)
def test_new_job_application_never_restarts_terminal_chat(
    tmp_path, previous_status, previous_step,
):
    store = SQLiteStateStore(tmp_path / "terminal-reapplication.sqlite3")
    store.save(
        "chat-1",
        ConversationState(
            step=previous_step,
            application_status=previous_status,
            city="Старый город",
            full_name="Старые данные",
            phone="+79990000000",
        ),
    )
    store.mark_message_seen("chat-1", "old-message", 100)
    messages = [
        {
            "id": "old-message",
            "created": 100,
            "direction": "in",
            "type": "text",
            "content": {"text": "старый ответ"},
        },
        job_application_message("job", 200, "job"),
        job_application_message("enrichment", 201, "job_apply_enrichment"),
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[-1]

    yielded = list(
        iter_new_chat_messages(FakeHistoryClient(messages), [candidate_chat], store)
    )

    assert yielded == []
    unchanged = store.load("chat-1")
    assert unchanged.step == previous_step
    assert unchanged.application_status == previous_status
    assert unchanged.city == "Старый город"
    assert unchanged.full_name == "Старые данные"
    assert unchanged.phone == "+79990000000"
    assert store.is_processed("chat-1", "job")
    assert store.is_processed("chat-1", "enrichment")
    store.close()


def test_outgoing_after_completed_application_does_not_reopen_chat(tmp_path):
    store = SQLiteStateStore(tmp_path / "terminal-reapplication-manual.sqlite3")
    store.save(
        "chat-1",
        ConversationState(step="done", application_status="completed"),
    )
    store.mark_message_seen("chat-1", "old-message", 100)
    messages = [
        {
            "id": "old-message",
            "created": 100,
            "direction": "out",
            "type": "text",
            "content": {"text": "старый ответ менеджера"},
        },
        job_application_message("job", 200, "job"),
        job_application_message("enrichment", 201, "job_apply_enrichment"),
        {
            "id": "new-manual",
            "created": 220,
            "direction": "out",
            "type": "text",
            "content": {"text": "новый ответ менеджера"},
        },
    ]
    candidate_chat = chat(direction="out")
    candidate_chat["last_message"] = messages[-1]

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient(messages),
            [candidate_chat],
            store,
            manual_takeover_after=150,
        )
    )

    assert yielded == []
    state = store.load("chat-1")
    assert state.step == "done"
    assert state.application_status == "completed"
    assert state.manual_takeover_message_id is None
    assert store.is_processed("chat-1", "job")
    assert store.is_processed("chat-1", "enrichment")
    assert store.is_processed("chat-1", "new-manual")
    store.close()


def test_terminal_application_before_installation_boundary_is_not_restarted(tmp_path):
    store = SQLiteStateStore(tmp_path / "terminal-old-application.sqlite3")
    store.save(
        "chat-1",
        ConversationState(step="manual_takeover", application_status="manual"),
    )
    store.mark_message_seen("chat-1", "older-message", 50)
    message = job_application_message("old-job", 100, "job")
    candidate_chat = chat()
    candidate_chat["last_message"] = message

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient([message]),
            [candidate_chat],
            store,
            not_before_timestamp=150,
        )
    )

    assert yielded == []
    state = store.load("chat-1")
    assert state.step == "manual_takeover"
    assert state.application_status == "manual"
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


def test_enrichment_event_alone_is_not_recovered_as_an_application(tmp_path):
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

    assert recovered == []
    store.close()


def test_enrichment_after_warehouse_answer_does_not_hide_candidate_reply(tmp_path):
    store = SQLiteStateStore(tmp_path / "warehouse-answer-enrichment.sqlite3")
    store.save(
        "chat-1",
        ConversationState(
            step="awaiting_warehouse",
            application_status="collecting",
            city="Москва",
        ),
    )
    store.mark_message_seen("chat-1", "warehouse-prompt", 100)
    candidate_answer = {
        "id": "candidate-answer",
        "created": 200,
        "direction": "in",
        "type": "text",
        "content": {"text": "4"},
    }
    enrichment = job_application_message(
        "enrichment", 201, "job_apply_enrichment"
    )
    candidate_chat = chat()
    candidate_chat["last_message"] = enrichment

    yielded = list(
        iter_new_chat_messages(
            FakeHistoryClient([candidate_answer, enrichment]),
            [candidate_chat],
            store,
        )
    )

    assert [values[3] for values in yielded] == ["candidate-answer"]
    assert store.is_processed("chat-1", "enrichment")
    assert not store.is_processed("chat-1", "candidate-answer")
    store.close()


@pytest.mark.parametrize(
    ("previous_status", "previous_step"),
    [
        ("completed", "done"),
        ("manual", "manual_takeover"),
        ("cancelled", "done"),
    ],
)
def test_restore_terminal_reapplications_is_idempotent(
    tmp_path, previous_status, previous_step,
):
    store = SQLiteStateStore(tmp_path / "restore-reapplications.sqlite3")
    state = ConversationState(
        step="awaiting_phone",
        application_status="collecting",
        reminder_due_at="2026-09-11T00:00:00+00:00",
        reactivation_sent=True,
        reactivation_reply_messages=["duplicate"],
        last_error="temporary error",
        next_retry_at="2026-09-11T00:00:00+00:00",
    )
    state.notes["reapplication_previous_status"] = previous_status
    state.notes["reapplication_previous_step"] = previous_step
    store.save("chat-1", state)

    assert restore_terminal_reapplications(store) == 1
    assert restore_terminal_reapplications(store) == 0

    restored = store.load("chat-1")
    assert restored.application_status == previous_status
    assert restored.step == previous_step
    assert restored.reminders_stopped
    assert restored.reminder_due_at is None
    assert not restored.reactivation_sent
    assert restored.reactivation_reply_messages == []
    assert restored.last_error is None
    assert restored.next_retry_at is None
    assert "reapplication_previous_status" not in restored.notes
    assert "reapplication_previous_step" not in restored.notes
    assert restored.notes["terminal_reapplication_restored"] == "true"
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
    from avito_bot.conversation import FOLLOW_UP_MESSAGE, INITIAL_MESSAGE
    from avito_bot.warehouses import warehouse_prompt_for_city

    assert infer_step_from_bot_message(INITIAL_MESSAGE) == "sending_intro"
    assert infer_step_from_bot_message(FOLLOW_UP_MESSAGE) == "sending_intro"
    assert (
        infer_step_from_bot_message(warehouse_prompt_for_city("Москва"))
        == "awaiting_warehouse"
    )
    assert infer_step_from_bot_message(LEGACY_DAY_PROMPT) == "awaiting_datetime"
    assert (
        infer_step_from_bot_message(
            "Стажировка каждый день в 7:30:00, на какой день вас записать? "
            "Укажите день недели например: Вторник"
        )
        == "awaiting_datetime"
    )
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
    from avito_bot.conversation import CONFIRMATION_MESSAGE

    state = ConversationState()
    messages = [
        {
            "id": "day-prompt",
            "created": 1784592000,
            "direction": "out",
            "type": "text",
            "content": {"text": LEGACY_DAY_PROMPT},
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
    from avito_bot.conversation import CONFIRMATION_MESSAGE

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
            "content": {"text": LEGACY_DAY_PROMPT},
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
        return {"id": f"sent-{len(self.messages)}"}


def test_send_bot_message_tracks_returned_avito_message_id(tmp_path):
    store = SQLiteStateStore(tmp_path / "sent-message.sqlite3")
    client = FakeClient()

    response = send_bot_message(client, store, "chat-1", "Сообщение бота")

    assert response == {"id": "sent-1"}
    assert client.messages == [("chat-1", "Сообщение бота")]
    assert store.is_bot_outgoing("chat-1", "sent-1")
    store.close()


class FakeForm:
    def __init__(self):
        self.applications = []

    def submit(self, application):
        self.applications.append(application)


class FakeInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Кемерово","Приглашение на ДАТА, Кемерово. '
            'Стажировка начинается в 8:00:00"\n'
            '"Бугры","Приглашение на ДАТА, Бугры"\n'
        )


class RegionalInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Краснодар","Стажировка начинается в 10:30:00 '
            'по адресу склада ДАТА"\n'
        )


def regional_catalog():
    return RegionalLocationCatalog.from_csv(
        " ,СЦ,Куда приглашать на стажировку,Время стажировки\n"
        'Кемерово,Кемерово,"Кемерово, ул. Терешковой д.41/11",8:00:00\n'
        'Краснодар,Краснодар,"г. Краснодар, х. Октябрьский, '
        'ул. Подсолнечная, 44",10:30:00\n'
    )


def test_regional_application_immediately_sends_four_messages(tmp_path):
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
    assert len(client.messages) == 4
    assert client.messages[0][1].startswith("1. 🚚 Водитель")
    assert client.messages[1][1].startswith("🛠 О работе")
    assert "ул. Подсолнечная, 44" in client.messages[2][1]
    assert "10:30:00" in client.messages[2][1]
    assert client.messages[3][1].startswith(
        "Стажировка каждый день в 10:30:00"
    )
    assert restored.step == "awaiting_datetime"
    assert restored.service_center == "Краснодар"
    assert restored.warehouse_selection_source == "regional_catalog"
    assert restored.address.endswith("ул. Подсолнечная, 44")
    assert restored.internship_time == "10:30:00"
    assert restored.regional_intro_messages_sent == 4
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
    assert len(resumed.messages) == 3
    assert resumed.messages[0][1].startswith("🛠 О работе")
    assert resumed.messages[1][1].startswith("Подобрали для вас склад")
    assert resumed.messages[2][1].startswith("Стажировка каждый день в 8:00:00")
    assert restored.step == "awaiting_datetime"
    assert restored.regional_intro_messages_sent == 4
    assert store.is_processed("chat-regional", "regional-job")
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
    assert client.messages[-1][1].startswith("Приглашение на 30.07., Кемерово")
    assert "Стажировка начинается в 8:00:00" in client.messages[-1][1]
    store.close()


def test_regional_completion_refreshes_time_from_current_location_catalog(
    tmp_path,
):
    store = SQLiteStateStore(tmp_path / "regional-current-time.sqlite3")
    state = ConversationState(
        step="awaiting_phone",
        city="Краснодар",
        service_center="Краснодар",
        item_id="regional-item",
        address="старый адрес",
        internship_time="10:30:00",
        warehouse_selection_source="regional_catalog",
        last_name="Иванов",
        first_name="Иван",
        full_name="Иванов Иван",
        phone="+79991234567",
        internship_date="30.07.2026",
        application_status="pending",
    )
    store.save("chat-regional-current-time", state)
    client = FakeClient()
    form = FakeForm()
    workflow = CandidateWorkflow(form, RegionalInvitationSource())
    current_locations = RegionalLocationCatalog.from_csv(
        " ,СЦ,Куда приглашать на стажировку,Время стажировки\n"
        'Краснодар,Краснодар,"актуальный адрес",9:00:00\n'
    )

    completed = complete_pending_application(
        client,
        workflow,
        store,
        "chat-regional-current-time",
        state,
        current_locations,
    )

    restored = store.load("chat-regional-current-time")
    assert completed
    assert restored.address == "актуальный адрес"
    assert restored.internship_time == "9:00:00"
    assert len(form.applications) == 1
    assert client.messages[-1][1].startswith(
        "Стажировка начинается в 9:00:00"
    )
    assert "10:30" not in client.messages[-1][1]
def test_initial_application_sends_three_messages_and_waits_for_warehouse(
    tmp_path,
):
    store = SQLiteStateStore(tmp_path / "three-messages.sqlite3")
    client = FakeClient()
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())

    process_chat_message(
        client,
        workflow,
        store,
        "chat-moscow-intro",
        ConversationState(),
        {"content": {"text": "system application"}},
        "intro-message",
        "Мытищи",
        "moscow-item",
    )

    restored = store.load("chat-moscow-intro")
    assert len(client.messages) == 3
    assert client.messages[0][1].startswith("1. 🚚 Водитель")
    assert client.messages[1][1].startswith("🛠 О работе")
    assert client.messages[2][1].startswith("Подобрали для вас склады")
    assert restored.step == "awaiting_warehouse"
    assert restored.intro_messages_sent == 3
    assert store.is_processed("chat-moscow-intro", "intro-message")
    store.close()


def test_interrupted_initial_sequence_resumes_with_only_missing_message(
    tmp_path,
):
    store = SQLiteStateStore(tmp_path / "resume-intro.sqlite3")
    state = ConversationState(
        step="sending_intro",
        city="Москва",
        intro_messages_sent=2,
        intro_trigger_message_id="intro-message",
    )
    store.save("chat-resume", state)
    client = FakeClient()
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())

    process_chat_message(
        client,
        workflow,
        store,
        "chat-resume",
        store.load("chat-resume"),
        {"content": {"text": "system application"}},
        "intro-message",
        "Москва",
        "moscow-item",
    )

    restored = store.load("chat-resume")
    assert len(client.messages) == 1
    assert client.messages[0][1].startswith("Подобрали для вас склады")
    assert restored.step == "awaiting_warehouse"
    assert restored.intro_trigger_message_id is None
    store.close()


def test_unsupported_city_is_marked_without_outgoing_messages(tmp_path):
    store = SQLiteStateStore(tmp_path / "unsupported-city.sqlite3")
    client = FakeClient()
    workflow = CandidateWorkflow(FakeForm(), FakeInvitationSource())

    process_chat_message(
        client,
        workflow,
        store,
        "chat-unsupported",
        ConversationState(),
        {"content": {"text": "system application"}},
        "unsupported-message",
        "Кемерово",
        "other-item",
    )

    restored = store.load("chat-unsupported")
    assert client.messages == []
    assert restored.step == "unsupported"
    assert store.is_processed("chat-unsupported", "unsupported-message")
    store.close()


def test_manual_outgoing_wins_when_candidate_replies_before_next_poll(tmp_path):
    store = SQLiteStateStore(tmp_path / "manual-before-candidate.sqlite3")
    store.save(
        "chat-1",
        ConversationState(
            step="awaiting_datetime",
            application_status="collecting",
            city="Тюмень",
        ),
    )
    store.mark_message_seen("chat-1", "previous-message", 100)
    manager = {
        "id": "manager-message",
        "created": 200,
        "direction": "out",
        "type": "text",
        "content": {"text": "Минуту, запишу вас"},
    }
    candidate = {
        "id": "candidate-message",
        "created": 201,
        "direction": "in",
        "type": "text",
        "content": {"text": "+7 900 000-00-00"},
    }
    candidate_chat = chat()
    candidate_chat["last_message"] = candidate

    assert not list(
        iter_new_chat_messages(
            FakeHistoryClient([candidate, manager]),
            [candidate_chat],
            store,
            manual_takeover_after=150,
        )
    )
    restored = store.load("chat-1")
    assert restored.step == "manual_takeover"
    assert restored.application_status == "manual"
    assert restored.manual_takeover_message_id == "manager-message"
    assert store.is_processed("chat-1", "manager-message")
    assert store.is_processed("chat-1", "candidate-message")
    store.close()


def test_same_second_manual_outgoing_wins_regardless_of_opaque_id_order(tmp_path):
    store = SQLiteStateStore(tmp_path / "same-second-manual.sqlite3")
    store.save(
        "chat-1",
        ConversationState(step="awaiting_datetime", application_status="collecting"),
    )
    store.mark_message_seen("chat-1", "previous-message", 100)
    messages = [
        {
            "id": "z-candidate",
            "created": 200,
            "direction": "in",
            "type": "text",
            "content": {"text": "Ответ кандидата"},
        },
        {
            "id": "a-manager",
            "created": 200,
            "direction": "out",
            "type": "text",
            "content": {"text": "Отвечу вручную"},
        },
    ]
    candidate_chat = chat()
    candidate_chat["last_message"] = messages[0]

    assert not list(
        iter_new_chat_messages(
            FakeHistoryClient(messages),
            [candidate_chat],
            store,
            manual_takeover_after=150,
        )
    )
    restored = store.load("chat-1")
    assert restored.application_status == "manual"
    assert restored.manual_takeover_message_id == "a-manager"
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
    def fixed_internship_date(value):
        if "четверг" in value.lower():
            return __import__("datetime").date(2026, 7, 23)
        raise ValueError("not a date")

    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date", fixed_internship_date
    )

    answers = [
        "system application",
        "2",
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
            "Санкт-Петербург",
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
    assert form.applications[0].warehouse == "СЦ Бугры"
    assert form.applications[0].tariff == "Драйв"
    assert form.applications[0].citizenship == "Российская Федерация"
    assert client.messages[0][1].startswith("1. 🚚 Водитель")
    assert client.messages[1][1].startswith("🛠 О работе")
    assert "2. Бугры" in client.messages[2][1]
    assert client.messages[-1][1].startswith("Приглашение на 23.07., Бугры")
    assert client.messages[-1][1].endswith("До встречи!")
    assert all(store.is_processed("chat-journey", f"message-{i}") for i in range(1, 6))
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
    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date",
        lambda value: __import__("datetime").date(2026, 7, 23),
    )

    answers = [
        "system application",
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
    assert restored.internship_time == "7:30:00"
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
