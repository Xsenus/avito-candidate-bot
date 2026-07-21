import pytest
from datetime import date

from avito_bot.yandex_form import (
    CandidateApplication,
    FormConfigurationError,
    YandexFormSubmitter,
)


def application(**overrides):
    values = {
        "warehouse": "СЦ Кемерово",
        "tariff": "Драйв",
        "last_name": "Иванов",
        "first_name": "Иван",
        "citizenship": "Российская Федерация",
        "phone": "+79916410399",
        "internship_date": "23.07.2026",
    }
    values.update(overrides)
    return CandidateApplication(**values)


def test_application_requires_every_form_field():
    with pytest.raises(ValueError, match="phone"):
        application(phone="").validate()


def test_form_url_is_required_before_browser_is_started():
    with pytest.raises(FormConfigurationError, match="YANDEX_FORM_URL"):
        YandexFormSubmitter("").submit(application())


def test_only_yandex_form_url_is_accepted():
    with pytest.raises(FormConfigurationError, match="forms.yandex.ru"):
        YandexFormSubmitter("https://example.com/form").submit(application())


def test_calendar_accessible_label_matches_yandex_format():
    assert YandexFormSubmitter._russian_date_label(date(2026, 7, 30)) == (
        "четверг, 30 июля 2026 г."
    )
