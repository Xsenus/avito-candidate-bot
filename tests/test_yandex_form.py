import pytest
from datetime import date

from avito_bot.yandex_form import (
    CandidateApplication,
    FormConfigurationError,
    FormSubmissionError,
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


def test_intercepted_submission_contains_candidate_fields():
    payload = {
        "values": {
            "surname": "Иванов",
            "name": "Иван",
            "phone": "+79916410399",
            "date": "2026-07-23",
            "consent": True,
        }
    }

    YandexFormSubmitter._verify_intercepted_submission(
        [
            {
                "method": "POST",
                "url": "https://forms.yandex.ru/gateway/root/form/postSurvey",
                "post_data": __import__("json").dumps(payload, ensure_ascii=False),
                "content_type": "application/json",
            }
        ],
        application(),
    )


def test_intercepted_submission_rejects_missing_phone():
    payload = {
        "values": {
            "surname": "Иванов",
            "name": "Иван",
            "date": "2026-07-23",
            "consent": True,
        }
    }
    with pytest.raises(FormSubmissionError, match="телефон"):
        YandexFormSubmitter._verify_intercepted_submission(
            [
                {
                    "method": "POST",
                    "url": "https://forms.yandex.ru/gateway/root/form/postSurvey",
                    "post_data": __import__("json").dumps(payload, ensure_ascii=False),
                    "content_type": "application/json",
                }
            ],
            application(),
        )
