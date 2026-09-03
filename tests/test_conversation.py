import pytest

from avito_bot.conversation import (
    ADDRESS_FALLBACK,
    CALL_HANDOFF_MESSAGE,
    FOLLOW_UP_MESSAGE,
    INITIAL_MESSAGE,
    MORE_INFO_MESSAGE,
    ConversationState,
    handle_user_message,
    initial_messages_for_city,
    is_positive,
    looks_like_datetime,
    resolve_address,
)


def test_candidate_answers_are_normalized(monkeypatch):
    state = ConversationState(step="awaiting_datetime")

    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date",
        lambda value: __import__("datetime").date(2026, 7, 28),
    )

    handle_user_message(state, "вторник")
    handle_user_message(state, "Иванов Иван Иванович")
    handle_user_message(state, "8 991 641-03-99")

    assert state.step == "ready_to_submit"
    assert state.application_status == "pending"
    assert state.last_name == "Иванов"
    assert state.first_name == "Иван"
    assert state.phone == "+79916410399"
    assert state.internship_date == "28.07.2026"
    assert state.tariff == "Драйв"
    assert state.citizenship == "Российская Федерация"


def test_invalid_phone_does_not_finish_conversation():
    state = ConversationState(step="awaiting_phone")

    reply = handle_user_message(state, "123")

    assert state.step == "awaiting_phone"
    assert state.phone is None
    assert "номер" in reply.lower()


@pytest.mark.parametrize("raw", ["четверг", "в четверг", "четвер", "чт"])
def test_conversation_recognizes_weekday_variants(raw):
    assert looks_like_datetime(raw)


def test_full_name_and_phone_can_be_sent_in_one_message():
    state = ConversationState(step="awaiting_full_name")

    reply = handle_user_message(state, "Травкин Виталий 8 (927) 206-97-01")

    assert reply == ""
    assert state.last_name == "Травкин"
    assert state.first_name == "Виталий"
    assert state.phone == "+79272069701"
    assert state.step == "ready_to_submit"
    assert state.application_status == "pending"


def test_first_name_and_phone_are_not_submitted_as_full_name():
    state = ConversationState(step="awaiting_full_name")

    reply = handle_user_message(state, "Виталий 8 (927) 206-97-01")

    assert "фамил" in reply.lower()
    assert state.step == "awaiting_full_name"
    assert state.application_status == "collecting"


def test_unknown_city_does_not_expose_an_unrelated_spreadsheet():
    assert resolve_address("Неизвестный город") == ADDRESS_FALLBACK
    assert "docs.google.com" not in ADDRESS_FALLBACK


def test_address_question_does_not_reset_candidate_progress():
    state = ConversationState(
        step="awaiting_phone",
        city="Кемерово",
        last_name="Травкин",
        first_name="Виталий",
        internship_date="23.07.2026",
    )

    reply = handle_user_message(state, "А где находится склад?")

    assert "Терешковой" in reply
    assert state.step == "awaiting_phone"
    assert state.last_name == "Травкин"
    assert state.internship_date == "23.07.2026"


@pytest.mark.parametrize(
    "answer", ["Да, готов", "готова", "Конечно!", "хорошо, давайте"]
)
def test_natural_positive_answers_are_accepted(answer):
    assert is_positive(answer)


@pytest.mark.parametrize("answer", ["нет", "не готов", "не согласна", "не интересно"])
def test_negative_answers_are_not_mistaken_for_positive(answer):
    assert not is_positive(answer)


@pytest.mark.parametrize(
    "city",
    [
        "Москва",
        "Мытищи",
        "Подольск",
        "Дзержинский",
        "Балашиха",
        "Красногорск",
        "Видное",
        "Домодедово",
        "Люберцы",
        "Королёв",
        "Королев",
        "Лыткарино",
        "Железнодорожный",
        "Пушкино",
    ],
)
def test_moscow_region_starts_with_three_messages(city):
    messages = initial_messages_for_city(city)

    assert len(messages) == 3
    assert messages[0] == INITIAL_MESSAGE
    assert messages[1] == FOLLOW_UP_MESSAGE
    assert messages[2].startswith("Подобрали для вас склады")
    assert "1. Железнодорожный" in messages[2]
    assert "Готовы пройти стажировку?" not in "\n".join(messages)
    assert all(len(message) <= 1000 for message in messages)


def test_moscow_and_saint_petersburg_price_is_per_trip():
    for city in ("Москва", "Санкт-Петербург"):
        messages = initial_messages_for_city(city)
        assert "от 6 000 ₽ за рейс" in messages[0]
        assert "за смену" not in messages[0]


@pytest.mark.parametrize("city", ["Санкт-Петербург", "СПб", "Бугры"])
def test_saint_petersburg_region_starts_with_three_messages(city):
    messages = initial_messages_for_city(city)

    assert len(messages) == 3
    assert "1. Троицкий" in messages[2]
    assert "2. Бугры" in messages[2]
    assert "2. Запад" not in messages[2]


def test_unsupported_city_is_ignored():
    state = ConversationState()

    reply = handle_user_message(state, "Отклик", city_hint="Кемерово")

    assert reply == ""
    assert state.step == "unsupported"
    assert initial_messages_for_city("Кемерово") == ()
    assert handle_user_message(state, "Где склад?") == ""


def test_moscow_candidate_selects_warehouse_before_date():
    state = ConversationState(step="awaiting_warehouse", city="Москва")

    reply = handle_user_message(state, "4", city_hint="Москва")

    assert state.step == "awaiting_datetime"
    assert state.warehouse_choice == 4
    assert state.service_center == "Печатники"
    assert state.address == "Курьяновская набережная, 6с2"
    assert state.internship_time == "7:30:00"
    assert "каждый день в 7:30:00" in reply
    assert "день недели" in reply


def test_saint_petersburg_candidate_selects_bugry_by_name():
    state = ConversationState(step="awaiting_warehouse", city="Санкт-Петербург")

    reply = handle_user_message(state, "Бугры?")

    assert state.step == "awaiting_datetime"
    assert state.warehouse_choice == 2
    assert state.service_center == "Бугры"
    assert state.internship_time == "7:00:00"
    assert "каждый день в 7:00:00" in reply


def test_selected_warehouse_uses_current_sheet_time(monkeypatch):
    from avito_bot.warehouses import WarehouseOption

    monkeypatch.setattr(
        "avito_bot.conversation.parse_warehouse_choice",
        lambda text, city: WarehouseOption(
            3,
            "Кувекино",
            "Кувекино",
            "Актуальный адрес",
            "9:15:00",
        ),
    )
    state = ConversationState(step="awaiting_warehouse", city="Москва")

    reply = handle_user_message(state, "3")

    assert state.internship_time == "9:15:00"
    assert reply.startswith("Стажировка каждый день в 9:15:00,")


def test_legacy_selected_state_recovers_warehouse_time():
    state = ConversationState(
        step="awaiting_arrival",
        city="Москва",
        warehouse_choice=6,
        service_center="Строгино",
    )

    reply = handle_user_message(state, "Да")

    assert state.step == "awaiting_datetime"
    assert state.internship_time == "8:30:00"
    assert reply.startswith("Стажировка каждый день в 8:30:00,")


def test_legacy_state_without_warehouse_asks_for_selection():
    state = ConversationState(step="awaiting_arrival", city="Санкт-Петербург")

    reply = handle_user_message(state, "Да")

    assert state.step == "awaiting_warehouse"
    assert "Чтобы указать точное время стажировки" in reply
    assert "1. Троицкий" in reply


def test_address_question_before_warehouse_choice_repeats_listing_options():
    state = ConversationState(step="awaiting_warehouse", city="Москва")

    reply = handle_user_message(state, "А где находится склад?")

    assert state.step == "awaiting_warehouse"
    assert state.address is None
    assert "Адрес зависит от выбранного склада" in reply
    assert "1. Железнодорожный" in reply
    assert "6. Строгино" in reply
    assert "7. Дзержинский" not in reply
    assert "8. СЦ Тарный" not in reply
    assert "Дмитровское шоссе 157с1" not in reply
    assert len(reply) <= 1000


def test_address_question_after_warehouse_choice_returns_selected_address():
    state = ConversationState(step="awaiting_warehouse", city="Москва")
    handle_user_message(state, "4")

    reply = handle_user_message(state, "А где находится склад?")

    assert state.step == "awaiting_datetime"
    assert reply == "Адрес склада: Курьяновская набережная, 6с2"


def test_invalid_warehouse_choice_offers_hr_call_without_more_reminders():
    state = ConversationState(step="awaiting_warehouse", city="Санкт-Петербург")

    reply = handle_user_message(state, "что-нибудь", city_hint=state.city)

    assert state.step == "awaiting_call"
    assert state.service_center is None
    assert reply == MORE_INFO_MESSAGE
    assert state.reminders_stopped


def test_zero_offers_hr_call_and_stops_reminders():
    state = ConversationState(step="awaiting_warehouse", city="Москва")

    reply = handle_user_message(state, "0", city_hint=state.city)

    assert state.step == "awaiting_call"
    assert state.application_status == "collecting"
    assert reply == MORE_INFO_MESSAGE
    assert state.reminders_stopped


@pytest.mark.parametrize("answer", ["Звонок", " звонок! ", "Хочу ЗВОНОК"])
def test_call_request_hands_dialog_to_hr(answer):
    state = ConversationState(
        step="awaiting_datetime",
        city="Тула",
        reminder_step="awaiting_datetime",
        reminder_due_at="2026-09-03T10:00:00+00:00",
    )

    reply = handle_user_message(state, answer)

    assert reply == CALL_HANDOFF_MESSAGE
    assert state.step == "manual_takeover"
    assert state.application_status == "manual"
    assert state.reminders_stopped
    assert state.reminder_due_at is None


def test_unknown_date_answer_offers_hr_call_without_more_reminders():
    state = ConversationState(step="awaiting_datetime", city="Тула")

    reply = handle_user_message(state, "Расскажите подробнее")

    assert reply == MORE_INFO_MESSAGE
    assert state.step == "awaiting_call"
    assert state.reminders_stopped


def test_awaiting_call_repeats_only_hr_offer_until_call_is_requested():
    state = ConversationState(
        step="awaiting_call", city="Тула", reminders_stopped=True
    )

    assert handle_user_message(state, "Хорошо") == MORE_INFO_MESSAGE
    assert state.step == "awaiting_call"
    assert handle_user_message(state, "Звонок") == CALL_HANDOFF_MESSAGE
    assert state.step == "manual_takeover"
