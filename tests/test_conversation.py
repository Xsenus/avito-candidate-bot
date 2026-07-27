import pytest

from avito_bot.conversation import (
    ADDRESS_FALLBACK,
    FOLLOW_UP_MESSAGE,
    INITIAL_MESSAGE,
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


@pytest.mark.parametrize("city", ["Москва", "Мытищи", "Подольск", "Дзержинский"])
def test_moscow_region_starts_with_three_messages(city):
    messages = initial_messages_for_city(city)

    assert len(messages) == 3
    assert messages[0] == INITIAL_MESSAGE
    assert messages[1] == FOLLOW_UP_MESSAGE
    assert messages[2].startswith("Подобрали для вас склады")
    assert "1. Железнодорожный" in messages[2]
    assert "Готовы пройти стажировку?" not in "\n".join(messages)
    assert all(len(message) <= 1000 for message in messages)


@pytest.mark.parametrize("city", ["Санкт-Петербург", "СПб", "Бугры"])
def test_saint_petersburg_region_starts_with_three_messages(city):
    messages = initial_messages_for_city(city)

    assert len(messages) == 3
    assert "1. Троицкий" in messages[2]
    assert "2. Запад" in messages[2]


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
    assert "день недели" in reply


def test_invalid_warehouse_choice_does_not_advance():
    state = ConversationState(step="awaiting_warehouse", city="Санкт-Петербург")

    reply = handle_user_message(state, "что-нибудь", city_hint=state.city)

    assert state.step == "awaiting_warehouse"
    assert state.service_center is None
    assert "Укажите номер" in reply


def test_zero_cancels_application_and_makes_it_terminal():
    state = ConversationState(step="awaiting_warehouse", city="Москва")

    reply = handle_user_message(state, "0", city_hint=state.city)

    assert state.step == "done"
    assert state.application_status == "cancelled"
    assert "отменил" in reply
