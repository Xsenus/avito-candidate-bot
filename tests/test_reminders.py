from datetime import datetime, timedelta, timezone

import pytest

import poller as poller_module
from avito_bot.conversation import (
    DATE_REMINDER_MESSAGE,
    ConversationState,
    stop_reminders,
)
from avito_bot.regional_locations import RegionalLocation, RegionalLocationCatalog
from avito_bot.reminders import (
    ReminderConfig,
    arm_reminders,
    finish_reminder,
    inflight_retry_is_due,
    mark_reminder_inflight,
    reminder_is_due,
    reminder_message,
)
from avito_bot.storage import SQLiteStateStore
from avito_bot.warehouses import WarehouseOption, replace_warehouse_groups
from poller import (
    infer_step_from_bot_message,
    iter_new_chat_messages,
    process_chat_message,
    process_due_reminders,
    send_initial_sequence,
    send_regional_initial_sequence,
)

UTC = timezone.utc


class ReminderClient:
    def __init__(self, now: datetime, messages=None, failures: int = 0):
        self.now = now
        self.messages = list(messages or [])
        self.failures = failures
        self.sent = []

    def get_messages(self, chat_id, *, limit):
        return list(reversed(self.messages[-limit:]))

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary send failure")
        message_id = f"bot-{len(self.sent)}"
        self.messages.append(
            {
                "id": message_id,
                "created": self.now.timestamp(),
                "direction": "out",
                "type": "text",
                "content": {"text": text},
            }
        )
        return {"id": message_id}


def config(*delays, grace=60):
    return ReminderConfig(
        enabled=True,
        delays_seconds=delays or (900, 3600, 86400),
        inflight_grace_seconds=grace,
        single_24h_only=False,
    )


def test_default_config_uses_question_relative_production_offsets(monkeypatch):
    for name in (
        "FOLLOW_UP_REMINDERS_ENABLED",
        "FOLLOW_UP_FIRST_DELAY_SECONDS",
        "FOLLOW_UP_SECOND_DELAY_SECONDS",
        "FOLLOW_UP_THIRD_DELAY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    value = ReminderConfig.from_env()

    assert not value.enabled
    assert value.delays_seconds == (5 * 60, 12 * 60 * 60, 24 * 60 * 60)
    assert value.single_24h_only
    assert value.all_offsets == (24 * 60 * 60,)


def test_test_delays_are_configurable_without_code_changes(monkeypatch):
    monkeypatch.setenv("FOLLOW_UP_REMINDERS_ENABLED", "true")
    monkeypatch.setenv("FOLLOW_UP_FIRST_DELAY_SECONDS", "60")
    monkeypatch.setenv("FOLLOW_UP_SECOND_DELAY_SECONDS", "300")
    monkeypatch.setenv("FOLLOW_UP_THIRD_DELAY_SECONDS", "600")

    value = ReminderConfig.from_env()

    assert value.enabled
    assert value.delays_seconds == (60, 300, 600)


def test_date_reminder_exactly_matches_customer_text():
    expected = (
        "Напоминаю — вакансия ещё актуальна.\n\n"
        "❗️Выберите дату, чтобы зафиксировать запись и внести вас в списки "
        "на стажировку. Если вы желаете выбрать другую дату напишите нужную "
        'дату в формате "ДД.ММ"'
    )

    assert DATE_REMINDER_MESSAGE == expected
    assert reminder_message(ConversationState(step="awaiting_datetime")) == expected
    assert len(expected) <= 1000


def test_warehouse_reminder_uses_current_dynamic_table_values():
    original = {
        "moscow": (
            WarehouseOption(1, "Тестовый", "Тестовый", "Новый адрес", "9:45:00"),
        ),
        "saint_petersburg": (),
    }
    from avito_bot.warehouses import WAREHOUSE_GROUPS

    previous = dict(WAREHOUSE_GROUPS)
    try:
        replace_warehouse_groups(original)
        text = reminder_message(
            ConversationState(step="awaiting_warehouse", city="Москва")
        )
    finally:
        replace_warehouse_groups(previous)

    assert text == (
        "❗️Напоминаю — вакансия ещё актуальна.\n"
        "Подобрали для вас склады — выберите удобный номером:\n\n"
        "1. Тестовый\n"
        "   📍 Новый адрес\n"
        "   🕥 Стажировка в 9:45:00\n\n"
        "Напишите номер подходящего склада (1–1) 👇"
    )
    assert text.startswith("❗️Напоминаю — вакансия ещё актуальна.")
    assert "Подобрали для вас склады" in text
    assert "📍 Новый адрес" in text
    assert "🕥 Стажировка в 9:45:00" in text
    assert text.endswith("Напишите номер подходящего склада (1–1) 👇")
    assert "Оператор" not in text
    assert '"0"' not in text
    assert len(text) <= 1000


def test_single_policy_sends_exactly_once_at_24_hours(tmp_path):
    started = datetime(2026, 9, 5, 5, 0, tzinfo=UTC)
    settings = ReminderConfig(enabled=True)
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, settings, now=started)
    store = SQLiteStateStore(tmp_path / "single-24h.sqlite3")
    store.save("chat-1", state)
    client = ReminderClient(started)

    assert state.reminder_policy_version == 3
    assert state.reminder_due_at == (started + timedelta(hours=24)).isoformat()
    assert process_due_reminders(
        client, store, settings, now=started + timedelta(hours=23, minutes=59)
    ) == 0
    client.now = started + timedelta(hours=24)
    assert process_due_reminders(client, store, settings, now=client.now) == 1
    restored = store.load("chat-1")
    assert restored.reminder_count == 1
    assert restored.reminder_due_at is None
    assert restored.reminder_step is None
    assert [text for _, text in client.sent] == [DATE_REMINDER_MESSAGE]
    assert process_due_reminders(
        client, store, settings, now=started + timedelta(days=10)
    ) == 0
    assert len(client.sent) == 1
    store.close()


def test_single_policy_migration_never_replays_old_campaign(tmp_path):
    now = datetime.now(UTC)
    settings = ReminderConfig(enabled=True)
    store = SQLiteStateStore(tmp_path / "single-migration.sqlite3")

    future = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        reminder_policy_version=2,
        reminder_armed_at=(now - timedelta(hours=1)).isoformat(),
        reminder_due_at=(now - timedelta(minutes=55)).isoformat(),
    )
    already_reminded = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        reminder_policy_version=2,
        reminder_count=1,
        reminder_armed_at=(now - timedelta(hours=2)).isoformat(),
        reminder_due_at=(now + timedelta(hours=10)).isoformat(),
    )
    missed = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        reminder_policy_version=2,
        reminder_armed_at=(now - timedelta(days=2)).isoformat(),
        reminder_due_at=(now - timedelta(days=1)).isoformat(),
    )
    for chat_id, state in (
        ("future", future),
        ("already", already_reminded),
        ("missed", missed),
    ):
        store.save(chat_id, state)

    client = ReminderClient(now)
    assert process_due_reminders(client, store, settings, now=now) == 0
    assert client.sent == []
    assert store.load("future").reminder_due_at == (
        now + timedelta(hours=23)
    ).isoformat()
    assert store.load("already").reminder_due_at is None
    assert store.load("missed").reminder_due_at is None
    assert all(
        store.load(chat_id).reminder_policy_version == 3
        for chat_id in ("future", "already", "missed")
    )
    store.close()


def test_single_policy_invalid_reply_repeats_question_and_rearms_once(tmp_path):
    now = datetime.now(UTC)
    settings = ReminderConfig(enabled=True)
    store = SQLiteStateStore(tmp_path / "single-invalid-reply.sqlite3")
    state = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        internship_time="8:00:00",
    )
    arm_reminders(state, settings, now=now - timedelta(hours=1))
    store.save("chat-1", state)
    client = ReminderClient(now)
    incoming = {
        "id": "candidate-question",
        "created": now.timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "Оператор"},
    }

    process_chat_message(
        client,
        object(),
        store,
        "chat-1",
        state,
        incoming,
        "candidate-question",
        "Тула",
        "item-1",
        reminder_config=settings,
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_datetime"
    assert restored.application_status == "collecting"
    assert restored.reminder_count == 0
    due = datetime.fromisoformat(restored.reminder_due_at)
    assert due >= now + timedelta(hours=23, minutes=59)
    assert len(client.sent) == 1
    assert client.sent[0][1] == (
        "Укажите день недели или дату в формате ДД.ММ.ГГГГ"
    )
    assert "Оператор" not in client.sent[0][1]
    store.close()


def test_schedule_is_question_relative_and_finishes_after_third_reminder():
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    state = ConversationState(step="awaiting_datetime")
    settings = config(900, 3600, 86400)

    arm_reminders(state, settings, now=started)

    assert state.reminder_due_at == (started + timedelta(minutes=15)).isoformat()
    assert not reminder_is_due(state, now=started + timedelta(minutes=14))
    assert reminder_is_due(state, now=started + timedelta(minutes=15))

    first = started + timedelta(minutes=15)
    mark_reminder_inflight(state, DATE_REMINDER_MESSAGE, now=first)
    finish_reminder(state, settings, sent_at=first)
    assert state.reminder_count == 1
    assert state.reminder_due_at == (started + timedelta(hours=1)).isoformat()

    second = started + timedelta(hours=1)
    mark_reminder_inflight(state, DATE_REMINDER_MESSAGE, now=second)
    finish_reminder(state, settings, sent_at=second)
    assert state.reminder_count == 2
    assert state.reminder_due_at == (started + timedelta(hours=24)).isoformat()

    third = started + timedelta(hours=24)
    mark_reminder_inflight(state, DATE_REMINDER_MESSAGE, now=third)
    finish_reminder(state, settings, sent_at=third)
    assert state.reminder_count == 3
    assert state.reminder_due_at is None
    assert state.reminder_step is None


def test_disabled_or_permanently_stopped_reminders_are_not_armed():
    now = datetime(2026, 9, 3, tzinfo=UTC)
    disabled = ConversationState(step="awaiting_datetime")
    arm_reminders(disabled, ReminderConfig(), now=now)
    assert disabled.reminder_due_at is None

    stopped = ConversationState(step="awaiting_datetime", reminders_stopped=True)
    arm_reminders(stopped, config(), now=now)
    assert stopped.reminder_due_at is None


@pytest.mark.parametrize(
    "state",
    [
        ConversationState(step="done", application_status="completed"),
        ConversationState(
            step="awaiting_datetime",
            application_status="manual",
            reminder_step="awaiting_datetime",
            reminder_due_at="2026-09-03T00:00:00+00:00",
        ),
        ConversationState(
            step="awaiting_datetime",
            reminder_step="awaiting_warehouse",
            reminder_due_at="2026-09-03T00:00:00+00:00",
        ),
        ConversationState(
            step="awaiting_datetime",
            reminder_step="awaiting_datetime",
            reminder_count=4,
            reminder_due_at="2026-09-03T00:00:00+00:00",
        ),
    ],
)
def test_terminal_mismatched_or_finished_state_is_never_due(state):
    assert not reminder_is_due(state, now=datetime(2026, 9, 4, tzinfo=UTC))


def test_invalid_persisted_timestamps_fail_closed():
    state = ConversationState(
        step="awaiting_datetime",
        reminder_step="awaiting_datetime",
        reminder_due_at="not-a-date",
        reminder_inflight_started_at="not-a-date",
    )

    assert not reminder_is_due(state)
    assert inflight_retry_is_due(state, config())
    assert not inflight_retry_is_due(ConversationState(), config())

    state.reminder_due_at = "2026-09-03T00:00:00"
    state.reminder_inflight_started_at = "2026-09-03T00:00:00"
    assert not reminder_is_due(state)
    assert inflight_retry_is_due(state, config())


def test_invalid_reminder_target_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="не предусмотрено"):
        reminder_message(ConversationState(step="awaiting_phone"))
    with pytest.raises(ValueError, match="не найден список"):
        reminder_message(
            ConversationState(step="awaiting_warehouse", city="Неизвестный город")
        )

    monkeypatch.setattr(
        "avito_bot.reminders.warehouse_prompt_for_city",
        lambda city: "x" * 1001,
    )
    with pytest.raises(ValueError, match="превышает лимит"):
        reminder_message(ConversationState(step="awaiting_warehouse", city="Москва"))


def test_invalid_due_reminder_is_quarantined_instead_of_retried_forever(
    tmp_path,
):
    settings = config(1, 2, 3)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(seconds=1)
    state = ConversationState(step="awaiting_warehouse", city="Неизвестный город")
    arm_reminders(state, settings, now=started)
    store = SQLiteStateStore(tmp_path / "invalid-reminder.sqlite3")
    store.save("chat-1", state)
    client = ReminderClient(due)

    assert process_due_reminders(client, store, settings, now=due) == 0
    restored = store.load("chat-1")
    assert restored.reminders_stopped
    assert restored.reminder_due_at is None
    assert client.sent == []
    store.close()


def test_reminder_schedule_survives_database_reopen(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    started = datetime(2026, 9, 3, tzinfo=UTC)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, config(), now=started)
    first = SQLiteStateStore(path)
    first.save("chat-1", state)
    first.close()

    second = SQLiteStateStore(path)
    restored = second.load("chat-1")
    second.close()

    assert restored.reminder_step == "awaiting_datetime"
    assert restored.reminder_due_at == (started + timedelta(minutes=15)).isoformat()
    assert not restored.reminders_stopped


def test_due_processor_sends_first_three_and_schedules_fourth(tmp_path):
    settings = config(900, 3600, 86400)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    store = SQLiteStateStore(tmp_path / "sequence.sqlite3")
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, settings, now=started)
    store.save("chat-1", state)
    client = ReminderClient(started)

    first = started + timedelta(minutes=15)
    client.now = first
    assert process_due_reminders(client, store, settings, now=first) == 1
    assert store.load("chat-1").reminder_count == 1

    second = started + timedelta(hours=1)
    client.now = second
    assert process_due_reminders(client, store, settings, now=second) == 1
    assert store.load("chat-1").reminder_count == 2

    third = started + timedelta(hours=24)
    client.now = third
    assert process_due_reminders(client, store, settings, now=third) == 1
    restored = store.load("chat-1")
    assert restored.reminder_count == 3
    assert restored.reminder_due_at == (started + timedelta(hours=48)).isoformat()
    from avito_bot.reminders import reactivation_message
    assert [text for _, text in client.sent] == [DATE_REMINDER_MESSAGE] * 2 + [reactivation_message(state)]
    assert restored.step == "awaiting_reactivation"
    assert restored.reactivation_sent
    store.close()


def test_candidate_date_after_first_reminder_continues_existing_flow(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    first = started + timedelta(minutes=15)
    store = SQLiteStateStore(tmp_path / "continue-after-reminder.sqlite3")
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, settings, now=started)
    store.save("chat-1", state)
    client = ReminderClient(first)
    assert process_due_reminders(client, store, settings, now=first) == 1

    state = store.load("chat-1")
    candidate = {
        "id": "candidate-date",
        "created": (first + timedelta(minutes=1)).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "Вторник"},
    }
    process_chat_message(
        client,
        object(),
        store,
        "chat-1",
        state,
        candidate,
        "candidate-date",
        "Тула",
        "item-1",
        reminder_config=settings,
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_full_name"
    assert restored.internship_date
    assert restored.reminder_due_at is None
    assert not restored.reminders_stopped
    assert "Для пропуска пришлите" in client.sent[-1][1]
    store.close()


def test_zero_after_reminder_stops_series_and_waits_for_call(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    first = started + timedelta(minutes=15)
    store = SQLiteStateStore(tmp_path / "zero-after-reminder.sqlite3")
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, settings, now=started)
    store.save("chat-1", state)
    client = ReminderClient(first)
    assert process_due_reminders(client, store, settings, now=first) == 1

    state = store.load("chat-1")
    candidate = {
        "id": "candidate-zero",
        "created": (first + timedelta(minutes=1)).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "0"},
    }
    process_chat_message(
        client,
        object(),
        store,
        "chat-1",
        state,
        candidate,
        "candidate-zero",
        "Тула",
        "item-1",
        reminder_config=settings,
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_call"
    assert restored.reminders_stopped
    assert restored.reminder_due_at is None
    assert (
        process_due_reminders(client, store, settings, now=first + timedelta(days=7))
        == 0
    )
    store.close()


def test_candidate_activity_cancels_due_reminder(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    incoming = {
        "id": "candidate-1",
        "created": (started + timedelta(minutes=1)).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "Вторник"},
    }
    client = ReminderClient(due, [incoming])
    store = SQLiteStateStore(tmp_path / "candidate.sqlite3")
    store.save("chat-1", state)

    assert process_due_reminders(client, store, settings, now=due) == 0
    restored = store.load("chat-1")
    assert restored.reminder_due_at is None
    assert not restored.reminders_stopped
    assert client.sent == []
    store.close()


def test_history_failure_for_one_chat_does_not_block_other_due_reminders(
    tmp_path,
):
    settings = config(1, 2, 3)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(seconds=1)
    store = SQLiteStateStore(tmp_path / "isolated-history-error.sqlite3")
    for chat_id in ("chat-failing", "chat-working"):
        state = ConversationState(step="awaiting_datetime")
        arm_reminders(state, settings, now=started)
        store.save(chat_id, state)

    class OneBrokenChatClient(ReminderClient):
        def get_messages(self, chat_id, *, limit):
            if chat_id == "chat-failing":
                raise RuntimeError("temporary history failure")
            return super().get_messages(chat_id, limit=limit)

    client = OneBrokenChatClient(due)

    assert process_due_reminders(client, store, settings, now=due) == 1
    assert store.load("chat-failing").reminder_count == 0
    assert store.load("chat-working").reminder_count == 1
    store.close()


def test_missing_chat_permanently_stops_its_reminders(tmp_path):
    settings = config(1, 2, 3)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(seconds=1)
    store = SQLiteStateStore(tmp_path / "missing-chat.sqlite3")
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    store.save("old-account-chat", state)

    class MissingChatError(RuntimeError):
        response = type("Response", (), {"status_code": 404})()

    class MissingChatClient(ReminderClient):
        def get_messages(self, chat_id, *, limit):
            raise MissingChatError("not found")

    assert process_due_reminders(
        MissingChatClient(due), store, settings, now=due
    ) == 0
    restored = store.load("old-account-chat")
    assert restored.reminders_stopped
    assert restored.reminder_due_at is None
    assert restored.notes["reminder_history_unavailable"] == "404"
    store.close()


def test_processed_candidate_message_does_not_cancel_new_series_on_clock_skew(
    tmp_path,
):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    previous_response = {
        "id": "candidate-previous",
        "created": (started + timedelta(seconds=5)).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "1"},
    }
    client = ReminderClient(due, [previous_response])
    store = SQLiteStateStore(tmp_path / "clock-skew.sqlite3")
    store.save("chat-1", state)
    store.mark_message_seen(
        "chat-1", "candidate-previous", previous_response["created"]
    )

    assert process_due_reminders(client, store, settings, now=due) == 1
    assert store.load("chat-1").reminder_count == 1
    store.close()


def test_avito_system_event_does_not_cancel_due_reminder(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    system_event = {
        "id": "system-1",
        "created": (started + timedelta(minutes=1)).timestamp(),
        "direction": "in",
        "type": "system",
        "content": {"flow_id": "job_apply_enrichment"},
    }
    client = ReminderClient(due, [system_event])
    store = SQLiteStateStore(tmp_path / "system.sqlite3")
    store.save("chat-1", state)

    assert process_due_reminders(client, store, settings, now=due) == 1
    assert store.load("chat-1").reminder_count == 1
    store.close()


def test_manual_activity_permanently_stops_due_reminders(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    manual = {
        "id": "manager-1",
        "created": (started + timedelta(minutes=1)).timestamp(),
        "direction": "out",
        "type": "text",
        "content": {"text": "Запишу вас вручную"},
    }
    client = ReminderClient(due, [manual])
    store = SQLiteStateStore(tmp_path / "manual.sqlite3")
    store.save("chat-1", state)

    assert process_due_reminders(client, store, settings, now=due) == 0
    restored = store.load("chat-1")
    assert restored.reminder_due_at is None
    assert restored.reminders_stopped
    assert client.sent == []
    store.close()


def test_inflight_delivery_is_reconciled_after_restart_without_duplicate(tmp_path):
    settings = config()
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    mark_reminder_inflight(state, DATE_REMINDER_MESSAGE, now=due)
    delivered = {
        "id": "delivered-before-crash",
        "created": due.timestamp(),
        "direction": "out",
        "type": "text",
        "content": {"text": DATE_REMINDER_MESSAGE},
    }
    client = ReminderClient(due + timedelta(seconds=10), [delivered])
    store = SQLiteStateStore(tmp_path / "reconcile.sqlite3")
    store.save("chat-1", state)

    assert (
        process_due_reminders(client, store, settings, now=due + timedelta(seconds=10))
        == 0
    )
    restored = store.load("chat-1")
    assert restored.reminder_count == 1
    assert restored.reminder_inflight_number is None
    assert store.is_bot_outgoing("chat-1", "delivered-before-crash")
    assert client.sent == []
    store.close()


def test_naive_inflight_timestamp_is_recovered_without_stopping_poller(tmp_path):
    settings = config(1, 2, 3, grace=5)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(seconds=1)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    mark_reminder_inflight(state, DATE_REMINDER_MESSAGE, now=due)
    state.reminder_inflight_started_at = "2026-09-03T05:00:01"
    store = SQLiteStateStore(tmp_path / "naive-inflight.sqlite3")
    store.save("chat-1", state)
    client = ReminderClient(due)

    assert process_due_reminders(client, store, settings, now=due) == 1
    assert store.load("chat-1").reminder_count == 1
    store.close()


def test_failed_send_waits_for_grace_period_before_retry(tmp_path):
    settings = config(grace=60)
    started = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    due = started + timedelta(minutes=15)
    state = ConversationState(step="awaiting_datetime")
    arm_reminders(state, settings, now=started)
    client = ReminderClient(due, failures=1)
    store = SQLiteStateStore(tmp_path / "retry.sqlite3")
    store.save("chat-1", state)

    assert process_due_reminders(client, store, settings, now=due) == 0
    assert len(client.sent) == 1
    assert store.load("chat-1").reminder_inflight_number == 1

    client.now = due + timedelta(seconds=30)
    assert process_due_reminders(client, store, settings, now=client.now) == 0
    assert len(client.sent) == 1

    client.now = due + timedelta(seconds=61)
    assert process_due_reminders(client, store, settings, now=client.now) == 1
    assert len(client.sent) == 2
    assert store.load("chat-1").reminder_count == 1
    store.close()


def test_stopping_reminders_clears_inflight_state():
    state = ConversationState(
        step="awaiting_datetime",
        reminder_step="awaiting_datetime",
        reminder_due_at="2026-09-03T05:15:00+00:00",
        reminder_armed_at="2026-09-03T05:00:00+00:00",
        reminder_inflight_number=1,
        reminder_inflight_started_at="2026-09-03T05:15:00+00:00",
        reminder_inflight_text=DATE_REMINDER_MESSAGE,
    )

    stop_reminders(state, permanently=True)

    assert state.reminder_due_at is None
    assert state.reminder_inflight_number is None
    assert state.reminders_stopped


def test_initial_moscow_sequence_arms_warehouse_reminders(tmp_path):
    store = SQLiteStateStore(tmp_path / "initial.sqlite3")
    state = ConversationState(city="Москва")
    client = ReminderClient(datetime(2026, 9, 3, tzinfo=UTC))

    assert send_initial_sequence(
        client, store, "chat-1", state, "application-1", config()
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_warehouse"
    assert restored.reminder_step == "awaiting_warehouse"
    assert restored.reminder_count == 0
    assert restored.reminder_due_at is not None
    assert len(client.sent) == 3
    store.close()


def test_initial_regional_sequence_arms_date_reminders_from_table(tmp_path):
    store = SQLiteStateStore(tmp_path / "regional-initial.sqlite3")
    state = ConversationState(city="Тула", item_id="item-1")
    client = ReminderClient(datetime(2026, 9, 3, tzinfo=UTC))
    catalog = RegionalLocationCatalog(
        [
            RegionalLocation(
                city="Тула",
                service_center="Тула",
                address="Актуальный адрес из таблицы",
                internship_time="9:20:00",
            )
        ]
    )

    send_regional_initial_sequence(
        client,
        store,
        "chat-1",
        state,
        "application-1",
        catalog,
        reminder_config=config(),
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_datetime"
    assert restored.reminder_step == "awaiting_datetime"
    assert restored.reminder_due_at is not None
    assert restored.address == "Актуальный адрес из таблицы"
    assert restored.internship_time == "9:20:00"
    assert "каждый день в 9:20:00" in client.sent[-1][1]
    assert len(client.sent) == 4
    store.close()


def test_warehouse_choice_starts_a_fresh_date_reminder_sequence(tmp_path):
    settings = config()
    store = SQLiteStateStore(tmp_path / "warehouse-choice.sqlite3")
    state = ConversationState(step="awaiting_warehouse", city="Москва")
    arm_reminders(state, settings, now=datetime(2026, 9, 3, 5, 0, tzinfo=UTC))
    store.save("chat-1", state)
    message = {
        "id": "candidate-choice",
        "created": datetime(2026, 9, 3, 5, 1, tzinfo=UTC).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "2"},
    }
    client = ReminderClient(datetime(2026, 9, 3, 5, 1, tzinfo=UTC))

    process_chat_message(
        client,
        object(),
        store,
        "chat-1",
        state,
        message,
        "candidate-choice",
        "Москва",
        "item-1",
        reminder_config=settings,
    )

    restored = store.load("chat-1")
    assert restored.step == "awaiting_datetime"
    assert restored.service_center == "Запад"
    assert restored.reminder_step == "awaiting_datetime"
    assert restored.reminder_count == 0
    assert restored.reminder_due_at is not None
    assert "на какой день вас записать" in client.sent[-1][1]
    store.close()


def test_candidate_response_cancels_due_reminder_even_if_processing_fails(
    tmp_path, monkeypatch
):
    settings = config()
    store = SQLiteStateStore(tmp_path / "response-failure.sqlite3")
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, settings, now=datetime(2026, 9, 3, 5, 0, tzinfo=UTC))
    store.save("chat-1", state)
    message = {
        "id": "candidate-date",
        "created": datetime(2026, 9, 3, 5, 15, tzinfo=UTC).timestamp(),
        "direction": "in",
        "type": "text",
        "content": {"text": "Вторник"},
    }
    client = ReminderClient(datetime(2026, 9, 3, 5, 15, tzinfo=UTC))
    monkeypatch.setattr(
        poller_module,
        "handle_user_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("parser failed")),
    )

    with pytest.raises(RuntimeError, match="parser failed"):
        process_chat_message(
            client,
            object(),
            store,
            "chat-1",
            state,
            message,
            "candidate-date",
            "Тула",
            "item-1",
            reminder_config=settings,
        )

    restored = store.load("chat-1")
    assert restored.reminder_due_at is None
    assert restored.reminder_inflight_number is None
    store.close()


def test_manual_outgoing_can_stop_only_reminders_without_stopping_core_flow(tmp_path):
    store = SQLiteStateStore(tmp_path / "manual-reminder-stop.sqlite3")
    state = ConversationState(
        step="awaiting_datetime",
        application_status="collecting",
        reminder_step="awaiting_datetime",
        reminder_due_at="1970-01-01T00:05:00+00:00",
        reminder_armed_at="1970-01-01T00:02:00+00:00",
    )
    store.save("chat-1", state)
    store.mark_message_seen("chat-1", "previous-message", 100)
    manual = {
        "id": "manager-message",
        "created": 200,
        "direction": "out",
        "type": "text",
        "content": {"text": "Запишу вручную"},
    }
    chat = {
        "id": "chat-1",
        "context": {"value": {"id": "item-1", "location": {"title": "Тула"}}},
        "last_message": manual,
    }

    yielded = list(
        iter_new_chat_messages(
            ReminderClient(datetime(2026, 9, 3, tzinfo=UTC), [manual]),
            [chat],
            store,
            reminder_manual_stop_after=150,
        )
    )

    restored = store.load("chat-1")
    assert yielded == []
    assert restored.step == "awaiting_datetime"
    assert restored.application_status == "collecting"
    assert restored.reminders_stopped
    assert restored.reminder_due_at is None
    store.close()


def test_recovery_recognizes_all_new_reminder_and_handoff_messages():
    warehouse = reminder_message(
        ConversationState(step="awaiting_warehouse", city="Москва")
    )

    assert infer_step_from_bot_message(warehouse) == "awaiting_warehouse"
    assert infer_step_from_bot_message(DATE_REMINDER_MESSAGE) == "awaiting_datetime"
    from avito_bot.conversation import CALL_HANDOFF_MESSAGE, MORE_INFO_MESSAGE

    assert infer_step_from_bot_message(MORE_INFO_MESSAGE) == "awaiting_call"
    assert infer_step_from_bot_message(CALL_HANDOFF_MESSAGE) == "manual_takeover"
