import pytest
from datetime import date

from avito_bot.yandex_form import (
    CandidateApplication,
    FormConfigurationError,
    FormSubmissionError,
    FormSubmissionUncertainError,
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


def test_application_rejects_conversation_words_in_name_fields():
    with pytest.raises(ValueError, match="распознать ФИО"):
        application(last_name="Работа", first_name="только").validate()


def test_application_enforces_fixed_tariff_and_citizenship():
    with pytest.raises(ValueError, match="Драйв"):
        application(tariff="Другой").validate()
    with pytest.raises(ValueError, match="Российская Федерация"):
        application(citizenship="Другое").validate()


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


class AnimatedDateButton:
    def __init__(self):
        self.counts = iter((2, 2, 1, 1))

    def count(self):
        return next(self.counts)


class AnimatedCalendarDialog:
    def __init__(self, button):
        self.button = button

    def get_by_role(self, role, *, name, exact):
        assert role == "button"
        assert name == "вторник, 4 августа 2026 г."
        assert exact is True
        return self.button


class AnimationPage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def test_calendar_waits_until_month_animation_removes_duplicate_date():
    button = AnimatedDateButton()
    dialog = AnimatedCalendarDialog(button)
    page = AnimationPage()

    result = YandexFormSubmitter("https://forms.yandex.ru/example")._wait_for_unique_date_button(
        page, dialog, "вторник, 4 августа 2026 г."
    )

    assert result is button
    assert page.waits == [100, 100]


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


def test_submission_write_detection_ignores_unrelated_form_requests():
    submit = {
        "method": "POST",
        "url": "https://forms.yandex.ru/gateway/root/form/postSurvey",
        "post_data": '{"values": {}}',
        "content_type": "application/json",
    }

    assert YandexFormSubmitter._is_form_submission_write(submit)
    assert not YandexFormSubmitter._is_form_submission_write(
        {**submit, "method": "GET"}
    )
    assert not YandexFormSubmitter._is_form_submission_write(
        {**submit, "url": "https://forms.yandex.ru/analytics"}
    )
    assert not YandexFormSubmitter._is_form_submission_write(
        {**submit, "url": "https://example.com/form/postSurvey"}
    )


@pytest.mark.parametrize("payload", [None, {}, {"status": "ok"}, {"success": True}])
def test_successful_submission_response_is_authoritative(payload):
    assert YandexFormSubmitter._classify_submission_response(200, payload) == "accepted"


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False},
        {"status": "failed"},
        {"result": {"errors": ["invalid answer"]}},
    ],
)
def test_success_status_with_explicit_form_failure_is_rejected(payload):
    assert YandexFormSubmitter._classify_submission_response(200, payload) == "rejected"


def test_http_failure_is_retryable_and_redirect_alone_is_not_conclusive():
    assert YandexFormSubmitter._classify_submission_response(429, None) == "rejected"
    assert YandexFormSubmitter._classify_submission_response(503, None) == "rejected"
    assert YandexFormSubmitter._classify_submission_response(302, None) == "unknown"


def test_missing_submission_request_is_safe_to_retry():
    error = YandexFormSubmitter._confirmation_error(
        details="",
        submission_request_seen=False,
        current_url="https://forms.yandex.ru/example",
    )

    assert type(error) is FormSubmissionError


def test_lost_response_after_submission_request_stays_uncertain():
    error = YandexFormSubmitter._confirmation_error(
        details="",
        submission_request_seen=True,
        current_url="https://forms.yandex.ru/example",
    )

    assert isinstance(error, FormSubmissionUncertainError)


def test_visible_form_validation_error_is_safe_to_retry():
    error = YandexFormSubmitter._confirmation_error(
        details="Обязательное поле не заполнено",
        submission_request_seen=True,
        current_url="https://forms.yandex.ru/example",
    )

    assert type(error) is FormSubmissionError


class ConsentLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class ConsentPage:
    def __init__(self, labelled_count, technical_count):
        self.labelled = ConsentLocator(labelled_count)
        self.technical = ConsentLocator(technical_count)
        self.pattern = None
        self.selector = None

    def get_by_role(self, role, *, name):
        assert role == "checkbox"
        self.pattern = name
        return self.labelled

    def locator(self, selector):
        self.selector = selector
        return self.technical


def test_consent_accepts_current_short_label():
    page = ConsentPage(labelled_count=1, technical_count=0)

    result = YandexFormSubmitter._find_consent_checkbox(page)

    assert result is page.labelled
    assert page.pattern.search("Я даю согласие")
    assert page.pattern.search("Согласие на обработку данных")


def test_consent_falls_back_to_unique_boolean_field():
    page = ConsentPage(labelled_count=0, technical_count=1)

    result = YandexFormSubmitter._find_consent_checkbox(page)

    assert result is page.technical
    assert page.selector == 'input[type="checkbox"][name^="answer_boolean_"]'


def test_consent_rejects_ambiguous_or_missing_structure():
    page = ConsentPage(labelled_count=0, technical_count=0)

    with pytest.raises(FormConfigurationError, match="структура формы изменилась"):
        YandexFormSubmitter._find_consent_checkbox(page)
