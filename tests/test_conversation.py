import pytest

from avito_bot.conversation import (
    ADDRESS_FALLBACK,
    ConversationState,
    handle_user_message,
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

    assert "фамилия" in reply.lower()
    assert state.step == "awaiting_full_name"
    assert state.application_status == "collecting"


def test_unknown_city_does_not_expose_an_unrelated_spreadsheet():
    assert resolve_address("Неизвестный город") == ADDRESS_FALLBACK
    assert "docs.google.com" not in ADDRESS_FALLBACK


@pytest.mark.parametrize(
    "answer", ["Да, готов", "готова", "Конечно!", "хорошо, давайте"]
)
def test_natural_positive_answers_are_accepted(answer):
    assert is_positive(answer)


@pytest.mark.parametrize("answer", ["нет", "не готов", "не согласна", "не интересно"])
def test_negative_answers_are_not_mistaken_for_positive(answer):
    assert not is_positive(answer)


def test_weekday_sent_with_internship_consent_is_not_asked_twice(monkeypatch):
    state = ConversationState(step="awaiting_staj")
    monkeypatch.setattr(
        "avito_bot.conversation.resolve_internship_date",
        lambda value: __import__("datetime").date(2026, 7, 23),
    )

    reply = handle_user_message(state, "Да, в четверг")

    assert reply
    assert state.step == "awaiting_full_name"
    assert state.internship_date == "23.07.2026"
