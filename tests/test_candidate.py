from datetime import date

import pytest

from avito_bot.candidate import (
    normalize_phone,
    resolve_internship_date,
    split_full_name,
)


def test_split_full_name_uses_first_two_parts():
    assert split_full_name("Иванов Иван Иванович") == ("Иванов", "Иван")


def test_split_full_name_requires_two_parts():
    with pytest.raises(ValueError):
        split_full_name("Иван")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8 991 641-03-99", "+79916410399"),
        ("+7 (991) 641-03-99", "+79916410399"),
        ("9916410399", "+79916410399"),
        ("8.991.641.03.99", "+79916410399"),
        ("мой номер: +7 991 641 03 99", "+79916410399"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["123", "799164103991"])
def test_normalize_phone_rejects_invalid_number(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", ["Во вторник", "вторника", "вт"])
def test_resolve_weekday_to_nearest_date(raw):
    monday = date(2026, 7, 20)
    assert resolve_internship_date(raw, today=monday) == date(2026, 7, 21)


@pytest.mark.parametrize("raw", ["Четверг", "в четверг", "четвер", "чт"])
def test_resolve_thursday_variants_from_screenshot(raw):
    tuesday = date(2026, 7, 21)
    assert resolve_internship_date(raw, today=tuesday) == date(2026, 7, 23)


def test_resolve_same_weekday_to_today():
    thursday = date(2026, 7, 23)
    assert resolve_internship_date("четверг", today=thursday) == thursday


def test_resolve_relative_date():
    today = date(2026, 7, 21)
    assert resolve_internship_date("послезавтра", today=today) == date(2026, 7, 23)


def test_resolve_explicit_date():
    assert resolve_internship_date("25.07.2026") == date(2026, 7, 25)
