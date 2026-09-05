from datetime import datetime, timedelta, timezone

import pytest

import poller
from avito_bot import reminders
from avito_bot.conversation import (
    CALL_HANDOFF_MESSAGE,
    LEGACY_CALL_HANDOFF_MESSAGE,
    LEGACY_DATE_REMINDER_MESSAGE,
    LEGACY_MORE_INFO_MESSAGE,
    MORE_INFO_MESSAGE,
    ConversationState,
    handle_user_message,
    is_call_request,
    is_reactivation_acceptance,
)
from avito_bot.regional_locations import RegionalLocation, RegionalLocationCatalog
from avito_bot.reminders import ReminderConfig, arm_reminders, reactivation_message
from avito_bot.storage import SQLiteStateStore

UTC = timezone.utc
START = datetime(2026, 9, 4, 8, tzinfo=UTC)
CONFIG = ReminderConfig(enabled=True, single_24h_only=False)


@pytest.fixture(autouse=True)
def _legacy_operator_scenarios(monkeypatch):
    monkeypatch.setenv("OPERATOR_HANDOFF_ENABLED", "true")


class Clock(datetime):
    value = START

    @classmethod
    def now(cls, tz=None):
        return cls.value


class Client:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.sent = []
        self.fail_after_delivery = False

    def get_messages(self, chat_id, *, limit):
        return list(reversed(self.messages[-limit:]))

    def send_message(self, chat_id, text):
        self.sent.append(text)
        mid = f"bot-{len(self.sent)}"
        self.messages.append(msg(mid, "out", text, Clock.value))
        if self.fail_after_delivery:
            self.fail_after_delivery = False
            raise TimeoutError("response lost")
        return {"id": mid}


def msg(mid, direction, text, when=START):
    return {
        "id": mid,
        "direction": direction,
        "type": "text",
        "created": when.timestamp(),
        "content": {"text": text},
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    Clock.value = START
    monkeypatch.setattr(poller, "datetime", Clock)
    monkeypatch.setattr(reminders, "utc_now", lambda: Clock.value)
    db = SQLiteStateStore(tmp_path / "bot.sqlite3")
    yield db
    db.close()


def catalog():
    return RegionalLocationCatalog(
        [RegionalLocation("Тула", "Тула", "Новый адрес из таблицы", "9:35:00")]
    )


@pytest.mark.parametrize("direction", ["in", "out"])
def test_polling_does_not_drop_new_same_second_lower_id(store, direction):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    store.save("chat", state)
    store.mark_message_seen("chat", "z-previous", START.timestamp())
    incoming = msg("a-new", direction, "Вторник")
    client = Client([msg("z-previous", "out", "Вопрос"), incoming])
    chats = [{"id": "chat", "last_message": incoming}]
    events = list(poller.iter_new_chat_messages(
        client, chats, store, manual_takeover_after=START.timestamp()
    ))
    if direction == "in":
        assert [event[3] for event in events] == ["a-new"]
        # Once processed, repeated history must not replay the answer.
        store.mark_message_seen("chat", "a-new", START.timestamp())
        assert list(poller.iter_new_chat_messages(client, chats, store)) == []
    else:
        assert not events
        assert store.load("chat").application_status == "manual"


@pytest.mark.parametrize("reverse", [False, True])
def test_manual_activity_wins_over_pending_candidate_reply(store, reverse):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    events = [msg("a-candidate", "in", "Вторник"), msg("z-manager", "out", "Отвечу сам")]
    if reverse:
        events.reverse()
    assert poller._activity_after_reminder_arm(events, store, "chat", state) == "manual"
    state.step = "awaiting_reactivation"
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    client = Client(events)
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert not client.sent
    assert store.load("chat").application_status == "manual"


@pytest.mark.parametrize("elapsed", [13, 25])
def test_late_delivery_reconciliation_keeps_original_deadlines(elapsed):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    reminders.mark_reminder_inflight(state, reminders.reminder_message(state), now=START)
    current = START + timedelta(hours=elapsed)
    reminders.finish_reminder(state, CONFIG, sent_at=current)
    assert state.reminder_count == 2
    assert state.reminder_due_at == max(START + timedelta(days=1), current + timedelta(seconds=1)).isoformat()


def test_broken_anchor_stops_timer_and_unknown_city_cannot_render_offer():
    state = ConversationState(step="awaiting_datetime", reminder_armed_at="invalid")
    reminders.finish_reminder(state, CONFIG, sent_at=START)
    assert state.reminders_stopped and state.reminder_due_at is None
    with pytest.raises(ValueError, match="Неизвестен город"):
        reactivation_message(ConversationState())


@pytest.mark.parametrize("anchor", [None, "invalid", "2026-09-04T08:00:00"])
def test_reconciled_offer_with_invalid_anchor_cannot_arm_48h_followup(store, anchor):
    state = ConversationState(
        step="awaiting_datetime", city="Тула", reminder_count=2,
        reminder_armed_at=anchor, reminder_due_at=START.isoformat(),
    )
    reminders.mark_reminder_inflight(state, reactivation_message(state), now=START)
    reminders.finish_reminder(state, CONFIG, sent_at=START + timedelta(hours=24))
    store.save("chat", state)
    assert state.reminders_stopped and state.reminder_due_at is None
    assert not state.reminder_inflight_number
    client = Client()
    assert poller.process_due_reminders(client, store, CONFIG, now=START + timedelta(days=4)) == 0
    assert not client.sent


@pytest.mark.parametrize("status", ["completed", "manual", "cancelled", "submission_retry_exhausted", "collecting"])
@pytest.mark.parametrize("step", ["awaiting_reactivation", "sending_reactivation_intro"])
def test_stopped_states_do_not_even_query_history_for_pending_work(store, status, step):
    class NoCalls(Client):
        def get_messages(self, *args, **kwargs):
            pytest.fail("A stopped state must not query history")

        def send_message(self, *args, **kwargs):
            pytest.fail("A stopped state must not send messages")

    state = ConversationState(
        step=step, city="Тула", application_status=status,
        reminders_stopped=(status == "collecting"),
        reminder_count=3, reminder_step=step,
        reminder_armed_at=START.isoformat(), reminder_due_at=START.isoformat(),
        reminder_inflight_number=4, reminder_inflight_started_at=START.isoformat(),
        reminder_inflight_text="Pending reminder", reactivation_sent=True,
        reactivation_reply_messages=["Pending reply"],
        reactivation_reply_trigger_id="trigger", reactivation_reply_inflight_at=START.isoformat(),
    )
    store.save("chat", state)
    client = NoCalls()
    assert poller.process_due_reminders(client, store, CONFIG, now=START + timedelta(days=4)) == 0
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert store.load("chat") == state


@pytest.mark.parametrize("direction", ["in", "out"])
def test_reminders_detect_same_second_activity(store, direction):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START.replace(microsecond=800000))
    store.save("chat", state)
    client = Client([msg("new-activity", direction, "Ответ")])
    assert poller.process_due_reminders(
        client, store, CONFIG, now=START + timedelta(minutes=6)
    ) == 0
    assert not client.sent
    saved = store.load("chat")
    assert saved.reminder_due_at is None
    assert saved.reminders_stopped == (direction == "out")


@pytest.mark.parametrize("direction", ["in", "out"])
def test_reactivation_detects_same_second_activity(store, direction):
    Clock.value = START.replace(microsecond=800000)
    state = ConversationState(step="awaiting_reactivation", city="Тула")
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    client = Client([msg("new-activity", direction, "Ответ")])
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert not client.sent
    saved = store.load("chat")
    assert saved.step == ("manual_takeover" if direction == "out" else "sending_reactivation_intro")


@pytest.mark.parametrize("direction", ["in", "out"])
@pytest.mark.parametrize("same_second", [True, False])
def test_same_second_fix_does_not_block_known_or_older_activity(store, direction, same_second):
    Clock.value = START.replace(microsecond=800000)
    when = START if same_second else START - timedelta(seconds=1)
    client = Client([msg("previous", direction, "Предыдущее сообщение", when)])
    if same_second:
        if direction == "in":
            store.mark_message_seen("chat", "previous", int(when.timestamp()))
        else:
            store.mark_bot_outgoing("chat", "previous")
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=Clock.value)
    assert poller._activity_after_reminder_arm(client.get_messages("chat", limit=100), store, "chat", state) is None
    state.step = "awaiting_reactivation"
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert len(client.sent) == 2
    assert store.load("chat").step == "awaiting_datetime"


@pytest.mark.parametrize(
    "word",
    ["Оператор", "оператор", "ОПЕРАТОР!", "Оператор, есть вопрос", "Звонок", "звонок!"],
)
def test_new_and_legacy_handoff_words(word):
    state = ConversationState(step="awaiting_reactivation", reactivation_sent=True)
    assert handle_user_message(state, word) == CALL_HANDOFF_MESSAGE
    assert state.application_status == "manual"
    assert state.step == "manual_takeover" and state.reminders_stopped
    assert handle_user_message(state, word) == ""


@pytest.mark.parametrize("word", ["Да", "да", "DA", "da", "Da", "согласен", "Да!"])
def test_explicit_acceptances(word):
    assert is_reactivation_acceptance(word)


@pytest.mark.parametrize(
    "word",
    ["не согласен", "неактуально", "0", "куда", "операторский", "да нет", "дата"],
)
def test_other_responses_offer_operator_without_restarting(word):
    assert not is_reactivation_acceptance(word)
    assert not is_call_request(word)
    state = ConversationState(step="awaiting_reactivation", reactivation_sent=True)
    assert handle_user_message(state, word) == MORE_INFO_MESSAGE
    assert state.step == "awaiting_call" and state.reminders_stopped


@pytest.mark.parametrize(
    "city,income",
    [
        ("Тула", "4500"),
        ("Казань", "4500"),
        ("Москва", "6000"),
        ("Балашиха", "6000"),
        ("Питер", "6000"),
        ("Бугры", "6000"),
    ],
)
def test_offer_has_customer_conditions(city, income):
    text = reactivation_message(ConversationState(city=city))
    assert f"Доход от {income} за смену, до 240 000 ₽ в месяц;" in text
    assert "ответьте «Да»" in text
    assert "Форд транзит" in text
    assert len(text) <= 1000


def test_absolute_offsets_and_final_offer_then_no_more_offers(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = Client()
    for number, seconds in enumerate((300, 43200, 86400, 172800), 1):
        Clock.value = START + timedelta(seconds=seconds - 1)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
        Clock.value += timedelta(seconds=1)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
        assert store.load("chat").reminder_count == number
        if number == 3:
            assert store.load("chat").reminder_due_at == (START + timedelta(hours=48)).isoformat()
    result = store.load("chat")
    assert result.step == "awaiting_reactivation" and result.reactivation_sent
    assert result.reminder_due_at is None
    assert client.sent[-1] == reactivation_message(result)
    assert client.sent[-2] == client.sent[-1]
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=9)
        )
        == 0
    )


def test_long_outage_sends_only_final_offer(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = Client()
    Clock.value = START + timedelta(days=2)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert client.sent == [reactivation_message(state)]
    assert store.load("chat").reminder_count == 4
    assert store.load("chat").reminder_due_at is None
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value + timedelta(days=7)) == 0


def send_24h_offer(store, city="Тула"):
    state = ConversationState(step="awaiting_datetime", city=city)
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = Client()
    Clock.value = START + timedelta(hours=24)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    return client


@pytest.mark.parametrize("city,income", [("Тула", "4500"), ("Москва", "6000"), ("Питер", "6000")])
def test_48h_offer_survives_restart_with_correct_price(store, city, income):
    client = send_24h_offer(store, city)
    restarted = SQLiteStateStore(store.path)
    try:
        assert restarted.load("chat").reminder_armed_at == START.isoformat()
        Clock.value = START + timedelta(hours=48)
        assert poller.process_due_reminders(client, restarted, CONFIG, now=Clock.value) == 1
        assert len(client.sent) == 2
        assert client.sent[0] == client.sent[1]
        assert f"от {income} за смену" in client.sent[-1]
        assert restarted.load("chat").reminder_due_at is None
        assert poller.process_due_reminders(client, restarted, CONFIG, now=START + timedelta(days=10)) == 0
    finally:
        restarted.close()


@pytest.mark.parametrize("direction", ["in", "out"])
def test_activity_between_offers_cancels_48h_send(store, direction):
    client = send_24h_offer(store)
    client.messages.append(msg("activity", direction, "Ответ", START + timedelta(hours=30)))
    Clock.value = START + timedelta(hours=48)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    assert len(client.sent) == 1
    assert store.load("chat").reminder_due_at is None


@pytest.mark.parametrize("hours", [25, 49])
@pytest.mark.parametrize("answer", ["Да", "0", "Оператор"])
def test_response_after_either_offer_never_restarts_offer_series(store, hours, answer):
    client = send_24h_offer(store)
    if hours > 48:
        Clock.value = START + timedelta(hours=48)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    Clock.value = START + timedelta(hours=hours)
    poller.process_chat_message(
        client, None, store, "chat", store.load("chat"),
        msg("reply", "in", answer, Clock.value), "reply", "Тула", None,
        regional_locations=catalog(), reminder_config=CONFIG,
    )
    expected_offers = 1 if hours < 48 else 2
    for day in (3, 4, 10):
        Clock.value = START + timedelta(days=day)
        poller.process_due_reminders(client, store, CONFIG, now=Clock.value)
    assert sum(text.startswith("Здравствуйте.") for text in client.sent) == expected_offers
    assert store.load("chat").step == {"Да": "awaiting_datetime", "0": "awaiting_call", "Оператор": "manual_takeover"}[answer]


def test_48h_lost_response_is_reconciled_without_duplicate(store):
    client = send_24h_offer(store)
    Clock.value = START + timedelta(hours=48)
    client.fail_after_delivery = True
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    assert store.load("chat").reminder_inflight_number == 4
    assert len(client.sent) == 2
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value + timedelta(seconds=61)) == 0
    assert len(client.sent) == 2
    assert store.load("chat").reminder_count == 4
    assert store.load("chat").reminder_due_at is None


def test_late_24h_send_does_not_move_48h_deadline(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    Clock.value = START + timedelta(hours=30)
    client = Client()
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert store.load("chat").reminder_due_at == (START + timedelta(hours=48)).isoformat()


def test_fourth_offset_configuration(monkeypatch):
    assert ReminderConfig.from_env().all_offsets == (86400,)
    monkeypatch.setenv("FOLLOW_UP_SINGLE_24H_ONLY", "false")
    assert ReminderConfig.from_env().all_offsets == (300, 43200, 86400, 172800)
    monkeypatch.setenv("FOLLOW_UP_FOURTH_DELAY_SECONDS", "180000")
    assert ReminderConfig.from_env().fourth_delay_seconds == 180000
    for value in ("0", "86400", "-1", "invalid"):
        monkeypatch.setenv("FOLLOW_UP_FOURTH_DELAY_SECONDS", value)
        with pytest.raises(ValueError):
            ReminderConfig.from_env()


@pytest.mark.parametrize("hours", [24, 48, 72])
def test_first_offer_after_outage_retries_if_not_delivered(store, hours):
    class FailOnce(Client):
        failed = False

        def send_message(self, chat_id, text):
            if not self.failed:
                self.failed = True
                raise TimeoutError("Not delivered")
            return super().send_message(chat_id, text)

    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = FailOnce()
    Clock.value = START + timedelta(hours=hours)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    Clock.value += timedelta(seconds=61)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert client.sent == [reactivation_message(state)]
    if hours >= 48:
        assert store.load("chat").reminder_due_at is None


@pytest.mark.parametrize("city", ["Тула", "Москва", "Питер"])
@pytest.mark.parametrize("failed_send", [1, 2, 3, 4])
@pytest.mark.parametrize("failure", ["before", "after", "missing_id"])
def test_all_four_milestones_with_fault_and_restart(store, city, failed_send, failure):
    class FaultClient(Client):
        attempts = 0

        def send_message(self, chat_id, text):
            self.attempts += 1
            inject = self.attempts == failed_send
            if inject and failure == "before":
                raise TimeoutError("Not delivered")
            result = super().send_message(chat_id, text)
            if inject and failure == "after":
                raise TimeoutError("Delivery response lost")
            return {} if inject and failure == "missing_id" else result

    state = ConversationState(
        step="awaiting_datetime" if city == "Тула" else "awaiting_warehouse", city=city
    )
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = FaultClient()
    for offset in CONFIG.all_offsets:
        for retry_delay in (0, 61, 122):
            Clock.value = START + timedelta(seconds=offset + retry_delay)
            restarted = SQLiteStateStore(store.path)
            try:
                poller.process_due_reminders(client, restarted, CONFIG, now=Clock.value)
            finally:
                restarted.close()
    Clock.value = START + timedelta(days=30)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    assert len(client.sent) == 4
    assert client.sent[0] == client.sent[1]
    assert client.sent[2] == client.sent[3] == reactivation_message(state)
    final = store.load("chat")
    assert final.reminder_count == 4 and final.reminder_due_at is None
    assert not final.reminder_inflight_number


@pytest.mark.parametrize("direction", ["in", "out"])
def test_activity_during_failed_final_retry_prevents_send(store, direction):
    class AlwaysFail(Client):
        def send_message(self, chat_id, text):
            raise TimeoutError("Not delivered")

    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    Clock.value = START + timedelta(days=3)
    poller.process_due_reminders(AlwaysFail(), store, CONFIG, now=Clock.value)
    client = Client([msg("reply", direction, "Ответ", Clock.value)])
    Clock.value += timedelta(seconds=61)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    assert not client.sent
    assert store.load("chat").reminder_due_at is None


@pytest.mark.parametrize("answer", ["Вторник", "Оператор"])
def test_reply_after_lost_intro_ack_preserves_bot_message_identity(store, answer):
    client = Client()
    client.fail_after_delivery = True
    with pytest.raises(TimeoutError):
        accept(store, client)
    assert not store.is_bot_outgoing("chat", "bot-1")
    Clock.value += timedelta(seconds=1)
    poller.process_chat_message(
        client, None, store, "chat", store.load("chat"),
        msg("new-reply", "in", answer, Clock.value), "new-reply", "Тула", None,
        reminder_config=CONFIG,
    )
    # Do not forget that the timed-out outgoing was our own message when the
    # new incoming supersedes the remaining address/day sequence.
    assert store.is_bot_outgoing("chat", "bot-1")
    assert len(client.sent) == 2
    assert store.load("chat").step == ("manual_takeover" if answer == "Оператор" else "awaiting_full_name")


def test_superseding_reply_waits_for_history_without_losing_outbox(store, monkeypatch):
    client = Client()
    client.fail_after_delivery = True
    with pytest.raises(TimeoutError):
        accept(store, client)
    Clock.value += timedelta(seconds=1)
    message = msg("date", "in", "Вторник", Clock.value)

    def unavailable(*args, **kwargs):
        raise TimeoutError("History unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(client, "get_messages", unavailable)
        with pytest.raises(TimeoutError):
            poller.process_chat_message(
                client, None, store, "chat", store.load("chat"), message,
                "date", "Тула", None, reminder_config=CONFIG,
            )
    assert store.load("chat").reactivation_reply_inflight_at
    assert not store.is_processed("chat", "date")
    assert len(client.sent) == 1
    poller.process_chat_message(
        client, None, store, "chat", store.load("chat"), message,
        "date", "Тула", None, reminder_config=CONFIG,
    )
    assert store.is_bot_outgoing("chat", "bot-1")
    assert store.is_processed("chat", "date")
    assert store.load("chat").step == "awaiting_full_name"
    assert len(client.sent) == 2


def test_superseding_reply_after_undelivered_intro_does_not_invent_delivery(store):
    class Undelivered(Client):
        def send_message(self, chat_id, text):
            raise TimeoutError("Not delivered")

    with pytest.raises(TimeoutError):
        accept(store, Undelivered())
    client = Client()
    Clock.value += timedelta(seconds=1)
    poller.process_chat_message(
        client, None, store, "chat", store.load("chat"),
        msg("date", "in", "Вторник", Clock.value), "date", "Тула", None,
        reminder_config=CONFIG,
    )
    assert len(client.sent) == 1 and "Для пропуска" in client.sent[0]
    assert store.load("chat").reactivation_reply_messages == []


@pytest.mark.parametrize("direction", ["in", "out"])
def test_last_reply_reconciliation_checks_intervening_activity(store, direction):
    class LoseLastAck(Client):
        def send_message(self, chat_id, text):
            result = super().send_message(chat_id, text)
            if len(self.sent) == 2:
                raise TimeoutError("Last response lost")
            return result

    client = LoseLastAck()
    with pytest.raises(TimeoutError):
        accept(store, client)
    Clock.value += timedelta(seconds=2)
    client.messages.append(msg("intervention", direction, "Вторник", Clock.value))
    poller.resume_reactivation_sequence(client, store, "chat", store.load("chat"), CONFIG)
    assert len(client.sent) == 2
    assert store.is_bot_outgoing("chat", "bot-2")
    saved = store.load("chat")
    assert saved.reminder_due_at is None
    assert saved.step == ("manual_takeover" if direction == "out" else "sending_reactivation_intro")
    poller.resume_reactivation_sequence(client, store, "chat", saved, CONFIG)
    assert store.load("chat").reminder_due_at is None
    if direction == "in":
        poller.process_chat_message(
            client, None, store, "chat", store.load("chat"), client.messages[-1],
            "intervention", "Тула", None, reminder_config=CONFIG,
        )
        assert store.load("chat").step == "awaiting_full_name"
        assert len(client.sent) == 3


def test_history_failure_after_last_intro_does_not_repeat_delivered_messages(store):
    class HistoryFailure(Client):
        reads = 0

        def get_messages(self, chat_id, *, limit):
            self.reads += 1
            if self.reads == 3:
                raise TimeoutError("Final history check unavailable")
            return super().get_messages(chat_id, limit=limit)

    client = HistoryFailure()
    with pytest.raises(TimeoutError):
        accept(store, client)
    assert len(client.sent) == 2
    assert store.load("chat").reactivation_reply_index == 2
    poller.resume_reactivation_sequence(client, store, "chat", store.load("chat"), CONFIG)
    assert len(client.sent) == 2
    assert store.load("chat").step == "awaiting_datetime"
    assert store.is_processed("chat", "yes")


@pytest.mark.parametrize("seed", range(12))
def test_irregular_polling_never_sends_early_or_repeats_finished_series(store, seed):
    import random

    rng = random.Random(seed)
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = Client()
    # Simulate different polling delays and outages over four days, including
    # repeated checks at exactly the same instant.
    ticks = sorted({0, 299, 300, 345600, *(rng.randrange(1, 345600) for _ in range(60))})
    for seconds in ticks:
        Clock.value = START + timedelta(seconds=seconds)
        before = len(client.sent)
        poller.process_due_reminders(client, store, CONFIG, now=Clock.value)
        if len(client.sent) > before:
            saved = store.load("chat")
            assert seconds >= CONFIG.all_offsets[saved.reminder_count - 1]
            assert len(client.sent) == before + 1
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    assert 1 <= len(client.sent) <= 4
    assert sum(text.startswith("Здравствуйте.") for text in client.sent) <= 2
    saved = store.load("chat")
    assert saved.reminder_count == 4 and saved.reminder_due_at is None
    Clock.value = START + timedelta(days=365)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0


@pytest.mark.parametrize("utc_offset", [7, 3, -5])
def test_all_deadlines_use_instants_not_local_clock_labels(store, utc_offset):
    anchor = START.astimezone(timezone(timedelta(hours=utc_offset)))
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=anchor)
    store.save("chat", state)
    client = Client()
    for offset in CONFIG.all_offsets:
        Clock.value = START + timedelta(seconds=offset) - timedelta(microseconds=1)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
        Clock.value += timedelta(microseconds=1)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert len(client.sent) == 4
    assert store.load("chat").reminder_due_at is None
    assert store._connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def accept(store, client, city="Тула", answer="Да"):
    state = ConversationState(
        step="awaiting_reactivation",
        city=city,
        reactivation_sent=True,
        address="Старый адрес",
    )
    store.save("chat", state)
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("yes", "in", answer),
        "yes",
        city,
        None,
        regional_locations=catalog(),
        reminder_config=CONFIG,
    )
    return store.load("chat")


def test_regional_yes_sends_only_two_messages_from_current_catalog(store):
    client = Client()
    state = accept(store, client)
    assert len(client.sent) == 2
    assert "Новый адрес из таблицы" in client.sent[0]
    assert "9:35:00" in client.sent[0] and "на какой день" not in client.sent[0]
    assert "9:35:00" in client.sent[1] and "на какой день" in client.sent[1]
    assert state.step == "awaiting_datetime"
    assert state.address == "Новый адрес из таблицы"
    assert state.reactivation_sent
    assert store.is_processed("chat", "yes")


@pytest.mark.parametrize("city,count", [("Москва", 6), ("Питер", 2)])
def test_metro_yes_offers_appropriate_warehouses(store, city, count):
    client = Client()
    state = accept(store, client, city)
    assert len(client.sent) == 1
    assert f"(1–{count})" in client.sent[0]
    assert state.step == "awaiting_warehouse"
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("choice", "in", "1"),
        "choice",
        city,
        None,
        reminder_config=CONFIG,
    )
    assert store.load("chat").step == "awaiting_datetime"
    assert "на какой день" in client.sent[-1]


def test_post_acceptance_only_two_regular_reminders_no_loop(store):
    client = Client()
    accept(store, client)
    for seconds in (300, 43200):
        Clock.value = START + timedelta(seconds=seconds)
        assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=2)
        )
        == 0
    )
    assert len(client.sent) == 4
    assert store.load("chat").reminder_due_at is None


def test_resume_after_delivered_but_timed_out_reply_without_duplicates(store):
    client = Client()
    client.fail_after_delivery = True
    with pytest.raises(TimeoutError):
        accept(store, client)
    state = store.load("chat")
    assert state.reactivation_reply_inflight_at
    assert not store.is_processed("chat", "yes")
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert len(client.sent) == 2
    assert store.load("chat").step == "awaiting_datetime"
    assert store.is_bot_outgoing("chat", "bot-1")
    assert store.is_processed("chat", "yes")


def test_manual_message_cancels_partially_sent_reply(store):
    client = Client()
    state = ConversationState(
        step="awaiting_reactivation", city="Тула", reactivation_sent=True
    )
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    client.messages.append(msg("manager", "out", "Отвечу сам"))
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert not client.sent
    assert store.load("chat").application_status == "manual"


def test_existing_waiting_dialog_is_migrated_without_burst(store):
    state = ConversationState(step="awaiting_datetime", city="Тула", reminder_count=1)
    store.save("chat", state)
    question = msg(
        "question",
        "out",
        "Стажировка каждый день в 8:00:00, на какой день вас записать?",
    )
    store.mark_bot_outgoing("chat", "question")
    client = Client([question])
    Clock.value = START + timedelta(days=2)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert client.sent == [reactivation_message(state)]
    assert store.load("chat").reminder_policy_version == 2
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0


@pytest.mark.parametrize("direction", ["in", "out"])
def test_migration_does_not_revive_answered_or_manual_dialog(store, direction):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    store.save("chat", state)
    question = msg(
        "question",
        "out",
        "Стажировка каждый день в 8:00:00, на какой день вас записать?",
    )
    store.mark_bot_outgoing("chat", "question")
    client = Client(
        [question, msg("reply", direction, "Уже записан", START + timedelta(minutes=1))]
    )
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=2)
        )
        == 0
    )
    assert not client.sent
    assert store.load("chat").reminder_due_at is None


@pytest.mark.parametrize(
    "status", ["completed", "manual", "cancelled", "submission_retry_exhausted"]
)
def test_migration_never_restarts_terminal_states(store, status):
    state = ConversationState(
        step="awaiting_datetime", city="Тула", application_status=status
    )
    store.save("chat", state)
    client = Client()
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=2)
        )
        == 0
    )
    assert not client.sent


def test_legacy_texts_remain_recognizable():
    assert (
        poller.infer_step_from_bot_message(LEGACY_CALL_HANDOFF_MESSAGE)
        == "manual_takeover"
    )
    assert (
        poller.infer_step_from_bot_message(LEGACY_MORE_INFO_MESSAGE) == "awaiting_call"
    )
    assert (
        poller.infer_step_from_bot_message(LEGACY_DATE_REMINDER_MESSAGE)
        == "awaiting_datetime"
    )
    assert (
        poller.infer_step_from_bot_message(
            reactivation_message(ConversationState(city="Тула"))
        )
        == "awaiting_reactivation"
    )


@pytest.mark.parametrize(
    "values", [(0, 1, 2), (300, 200, 86400), (300, 300, 86400), (300, 43200, 100)]
)
def test_invalid_offsets_are_rejected(values):
    with pytest.raises(ValueError):
        ReminderConfig(delays_seconds=values)


def test_acceptance_immediately_after_lost_offer_response(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = Client()
    Clock.value = START + timedelta(days=1)
    client.fail_after_delivery = True
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 0
    state = store.load("chat")
    assert state.step == "awaiting_datetime" and state.reminder_inflight_number == 3
    Clock.value += timedelta(seconds=1)
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("yes", "in", "Да", Clock.value),
        "yes",
        "Тула",
        None,
        regional_locations=catalog(),
        reminder_config=CONFIG,
    )
    assert len(client.sent) == 3  # One offer, address, day question.
    assert store.load("chat").step == "awaiting_datetime"
    assert store.load("chat").reactivation_sent
    assert store.is_bot_outgoing("chat", "bot-1")


def test_lost_offer_response_is_not_mistaken_for_manual_operator(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    store.mark_message_seen("chat", "previous", START.timestamp())
    client = Client()
    Clock.value = START + timedelta(days=1)
    client.fail_after_delivery = True
    poller.process_due_reminders(client, store, CONFIG, now=Clock.value)
    outgoing = client.messages[-1]
    chats = [{"id": "chat", "last_message": outgoing}]
    assert (
        list(
            poller.iter_new_chat_messages(
                client, chats, store, manual_takeover_after=START.timestamp()
            )
        )
        == []
    )
    assert store.load("chat").application_status == "collecting"
    assert store.is_bot_outgoing("chat", outgoing["id"])


def test_reactivation_retry_before_delivery_waits_for_grace(store):
    class BeforeDeliveryClient(Client):
        def send_message(self, chat_id, text):
            raise TimeoutError("not delivered")

    client = BeforeDeliveryClient()
    with pytest.raises(TimeoutError):
        accept(store, client)
    state = store.load("chat")
    good = Client()
    poller.resume_reactivation_sequence(good, store, "chat", state, CONFIG)
    assert not good.sent
    Clock.value += timedelta(seconds=61)
    poller.resume_reactivation_sequence(good, store, "chat", store.load("chat"), CONFIG)
    assert len(good.sent) == 2
    assert store.load("chat").step == "awaiting_datetime"


def test_operator_interrupts_retry_sequence(store):
    client = Client()
    state = ConversationState(
        step="awaiting_reactivation", city="Тула", reactivation_sent=True
    )
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("operator", "in", "Оператор"),
        "operator",
        "Тула",
        None,
        reminder_config=CONFIG,
    )
    assert client.sent == [CALL_HANDOFF_MESSAGE]
    assert store.load("chat").application_status == "manual"


def test_candidate_date_supersedes_partial_sequence(store):
    client = Client()
    state = ConversationState(
        step="awaiting_reactivation", city="Тула", reactivation_sent=True
    )
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("date", "in", "Вторник"),
        "date",
        "Тула",
        None,
        reminder_config=CONFIG,
    )
    assert len(client.sent) == 1 and "Для пропуска" in client.sent[0]
    assert store.load("chat").step == "awaiting_full_name"
    assert store.load("chat").reminder_due_at is None


def test_new_incoming_event_defers_background_sequence(store):
    client = Client()
    state = ConversationState(
        step="awaiting_reactivation", city="Тула", reactivation_sent=True
    )
    poller.prepare_reactivation_sequence(store, "chat", state, "yes", catalog())
    client.messages.append(msg("date", "in", "Вторник"))
    poller.resume_reactivation_sequence(client, store, "chat", state, CONFIG)
    assert not client.sent


def test_migration_preserves_sent_milestones(store):
    state = ConversationState(step="awaiting_datetime", city="Тула", reminder_count=1)
    store.save("chat", state)
    question = msg(
        "question",
        "out",
        "Стажировка каждый день в 8:00:00, на какой день вас записать?",
    )
    store.mark_bot_outgoing("chat", "question")
    client = Client([question])
    poller.migrate_waiting_reminders(
        client, store, CONFIG, now=START + timedelta(minutes=20)
    )
    updated = store.load("chat")
    assert updated.reminder_count == 1
    assert updated.reminder_due_at == (START + timedelta(hours=12)).isoformat()
    assert not client.sent


def test_migration_without_trusted_history_is_fail_closed(store):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    store.save("chat", state)
    client = Client(
        [
            msg(
                "unknown",
                "out",
                "Стажировка каждый день в 8:00:00, на какой день вас записать?",
            )
        ]
    )
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=2)
        )
        == 0
    )
    assert store.load("chat").notes["reminder_migration"] == "no_verified_question"


@pytest.mark.parametrize("direction", ["in", "out"])
def test_migration_does_not_revive_same_second_answered_question(store, direction):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    store.save("chat", state)
    question = msg("z-question", "out", "Стажировка каждый день в 8:00:00, на какой день вас записать?")
    activity = msg("a-activity", direction, "Ответ")
    store.mark_bot_outgoing("chat", "z-question")
    client = Client([question, activity])
    assert poller.migrate_waiting_reminders(client, store, CONFIG, now=START + timedelta(days=1)) == 0
    saved = store.load("chat")
    assert saved.reminder_due_at is None
    assert saved.notes["reminder_migration"] == ("manual" if direction == "out" else "answered")
    assert not client.sent


@pytest.mark.parametrize("case", ["known_bot_same_second", "older_candidate", "older_manager"])
def test_migration_boundary_does_not_block_verified_unanswered_question(store, case):
    state = ConversationState(step="awaiting_datetime", city="Тула")
    store.save("chat", state)
    question = msg("z-question", "out", "Стажировка каждый день в 8:00:00, на какой день вас записать?")
    store.mark_bot_outgoing("chat", "z-question")
    when = START if case == "known_bot_same_second" else START - timedelta(seconds=1)
    direction = "in" if case == "older_candidate" else "out"
    previous = msg("a-previous", direction, "Предыдущее сообщение", when)
    if case == "known_bot_same_second":
        store.mark_bot_outgoing("chat", "a-previous")
    client = Client([previous, question])
    assert poller.migrate_waiting_reminders(client, store, CONFIG, now=START + timedelta(minutes=1)) == 1
    saved = store.load("chat")
    assert saved.reminder_armed_at == START.isoformat()
    assert saved.reminder_due_at == (START + timedelta(minutes=5)).isoformat()
    assert not saved.reminders_stopped
    assert not client.sent


def test_migration_api_outage_retries_later_without_sending(store):
    class Unavailable(Client):
        def get_messages(self, chat_id, *, limit):
            raise TimeoutError()

    store.save("chat", ConversationState(step="awaiting_datetime", city="Тула"))
    assert (
        poller.migrate_waiting_reminders(Unavailable(), store, CONFIG, now=START) == 0
    )
    assert "reminder_migration_retry_at" in store.load("chat").notes
    assert store.load("chat").reminder_policy_version == 0


def test_reactivation_missing_catalog_does_not_consume_acceptance(store):
    state = ConversationState(
        step="awaiting_reactivation", city="Тула", reactivation_sent=True
    )
    store.save("chat", state)
    client = Client()
    with pytest.raises(ValueError):
        poller.process_chat_message(
            client,
            None,
            store,
            "chat",
            state,
            msg("yes", "in", "Да"),
            "yes",
            "Тула",
            None,
            reminder_config=CONFIG,
        )
    assert store.load("chat").step == "awaiting_reactivation"
    assert not store.is_processed("chat", "yes")
    assert not client.sent


def test_full_regional_reactivation_to_completed_with_fake_form(store):
    from avito_bot.invitations import InvitationCatalog
    from avito_bot.workflow import CandidateWorkflow

    class Form:
        calls = 0

        def submit(self, application):
            self.calls += 1
            assert application.warehouse

    class Invitations:
        def load(self):
            return InvitationCatalog.from_csv(
                '"СЦ","Текст сообщения"\n"Тула","Вы записаны ДАТА. Стажировка начинается в 8:00:00"\n'
            )

    form = Form()
    workflow = CandidateWorkflow(form, Invitations())
    client = Client()
    accept(store, client)
    for mid, text in [
        ("day", "Вторник"),
        ("name", "Тестов Тест"),
        ("phone", "+79990000000"),
    ]:
        Clock.value += timedelta(seconds=1)
        state = store.load("chat")
        poller.process_chat_message(
            client,
            workflow,
            store,
            "chat",
            state,
            msg(mid, "in", text, Clock.value),
            mid,
            "Тула",
            None,
            regional_locations=catalog(),
            reminder_config=CONFIG,
        )
    state = store.load("chat")
    assert state.application_status == "completed" and state.reminder_due_at is None
    assert form.calls == 1
    assert "9:35:00" in client.sent[-1]
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=3)
        )
        == 0
    )
    assert form.calls == 1


@pytest.mark.parametrize("status", ["completed", "manual", "cancelled"])
def test_terminal_state_with_stale_inflight_offer_cannot_be_revived(store, status):
    state = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        application_status=status,
        reminder_inflight_number=3,
        reminder_inflight_started_at=START.isoformat(),
    )
    state.reminder_inflight_text = reactivation_message(state)
    store.save("chat", state)
    client = Client([msg("sent", "out", state.reminder_inflight_text)])
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=START + timedelta(days=1)
        )
        == 0
    )
    poller.process_chat_message(
        client,
        None,
        store,
        "chat",
        state,
        msg("yes", "in", "Да"),
        "yes",
        "Тула",
        None,
        reminder_config=CONFIG,
    )
    assert store.load("chat").application_status == status
    assert not client.sent


def test_outbox_handles_success_without_message_id(store):
    class MissingId(Client):
        def send_message(self, chat_id, text):
            super().send_message(chat_id, text)
            return {}

    client = MissingId()
    accept(store, client)
    assert store.load("chat").step == "sending_reactivation_intro"
    for _ in range(3):
        poller.resume_reactivation_sequence(
            client, store, "chat", store.load("chat"), CONFIG
        )
    assert len(client.sent) == 2
    assert store.load("chat").step == "awaiting_datetime"
    assert store.is_bot_outgoing("chat", "bot-1")
    assert store.is_bot_outgoing("chat", "bot-2")


def test_reminder_handles_success_without_message_id(store):
    class MissingId(Client):
        def send_message(self, chat_id, text):
            super().send_message(chat_id, text)
            return {}

    state = ConversationState(step="awaiting_datetime", city="Тула")
    arm_reminders(state, CONFIG, now=START)
    store.save("chat", state)
    client = MissingId()
    Clock.value = START + timedelta(days=1)
    poller.process_due_reminders(client, store, CONFIG, now=Clock.value)
    assert store.load("chat").reminder_inflight_number == 3
    poller.process_due_reminders(client, store, CONFIG, now=Clock.value)
    assert store.load("chat").step == "awaiting_reactivation"
    assert len(client.sent) == 1
    assert store.is_bot_outgoing("chat", "bot-1")


def test_post_acceptance_outage_does_not_burst_overdue_reminders(store):
    client = Client()
    accept(store, client)
    Clock.value = START + timedelta(days=2)
    assert poller.process_due_reminders(client, store, CONFIG, now=Clock.value) == 1
    assert (
        poller.process_due_reminders(
            client, store, CONFIG, now=Clock.value + timedelta(minutes=1)
        )
        == 0
    )
    assert len(client.sent) == 3  # Address, question, only the last regular reminder.


def test_pending_sequence_survives_new_database_connection(store):
    client = Client()
    client.fail_after_delivery = True
    with pytest.raises(TimeoutError):
        accept(store, client)
    restarted = SQLiteStateStore(store.path)
    try:
        saved = restarted.load("chat")
        assert saved.reactivation_sent and saved.reactivation_reply_inflight_at
        poller.resume_reactivation_sequence(client, restarted, "chat", saved, CONFIG)
        assert restarted.load("chat").step == "awaiting_datetime"
        assert restarted.is_processed("chat", "yes")
        assert len(client.sent) == 2
    finally:
        restarted.close()
