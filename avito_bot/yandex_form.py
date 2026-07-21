from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime

from .candidate import normalize_phone, split_full_name


class FormConfigurationError(RuntimeError):
    pass


class FormSubmissionError(RuntimeError):
    pass


class FormSubmissionUncertainError(FormSubmissionError):
    """The submit button was pressed but the success result is unknown."""

    pass


@dataclass(frozen=True)
class CandidateApplication:
    warehouse: str
    tariff: str
    last_name: str
    first_name: str
    citizenship: str
    phone: str
    internship_date: str

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("warehouse", self.warehouse),
                ("tariff", self.tariff),
                ("last_name", self.last_name),
                ("first_name", self.first_name),
                ("citizenship", self.citizenship),
                ("phone", self.phone),
                ("internship_date", self.internship_date),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"Не заполнены поля заявки: {', '.join(missing)}")
        split_full_name(f"{self.last_name} {self.first_name}")
        if normalize_phone(self.phone) != self.phone:
            raise ValueError("Телефон заявки должен иметь формат +7XXXXXXXXXX")
        if self.tariff != "Драйв":
            raise ValueError("Тариф заявки должен быть «Драйв»")
        if self.citizenship != "Российская Федерация":
            raise ValueError("Гражданство заявки должно быть «Российская Федерация»")
        try:
            datetime.strptime(self.internship_date, "%d.%m.%Y")
        except ValueError as exc:
            raise ValueError("Дата заявки должна иметь формат ДД.ММ.ГГГГ") from exc


class YandexFormSubmitter:
    """Fill a public Yandex Form by visible labels and verify its success screen."""

    def __init__(self, form_url: str, *, timeout_ms: int = 60_000) -> None:
        self.form_url = (form_url or "").strip()
        self.timeout_ms = timeout_ms

    @classmethod
    def from_env(cls) -> "YandexFormSubmitter":
        timeout = int(os.getenv("YANDEX_FORM_TIMEOUT_SECONDS", "60")) * 1000
        return cls(os.getenv("YANDEX_FORM_URL", ""), timeout_ms=timeout)

    def submit(self, application: CandidateApplication) -> None:
        self._run(application, do_submit=True)

    def validate_schema(self, application: CandidateApplication) -> None:
        """Fill all fields without pressing Submit; useful after the URL changes."""
        self._run(application, do_submit=False)

    def validate_submission_request(self, application: CandidateApplication) -> None:
        """Click Submit, block every write request, and verify its JSON payload."""
        self._run(application, do_submit=True, intercept_submission=True)

    def validate_warehouse_options(self, warehouses: list[str]) -> None:
        """Verify active warehouse options without filling or submitting an answer."""
        self._validate_url()
        options = list(dict.fromkeys(option.strip() for option in warehouses if option.strip()))
        if not options:
            raise ValueError("Нет складов для проверки")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FormConfigurationError(
                "Playwright не установлен; выполните python -m playwright install chromium"
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="ru-RU")
                page.goto(self.form_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                page.get_by_role("button", name="Отправить").wait_for(
                    state="visible", timeout=self.timeout_ms
                )
                for option in options:
                    self._select_option(page, "Выберите склад", option)
            finally:
                browser.close()

    def _run(
        self,
        application: CandidateApplication,
        *,
        do_submit: bool,
        intercept_submission: bool = False,
    ) -> None:
        application.validate()
        self._validate_url()

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FormConfigurationError(
                "Playwright не установлен; выполните python -m playwright install chromium"
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="ru-RU")
                page.goto(self.form_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                page.get_by_role("button", name="Отправить").wait_for(
                    state="visible", timeout=self.timeout_ms
                )

                self._select_option(page, "Выберите склад", application.warehouse)
                self._select_option(
                    page,
                    "На каком автомобиле хотели бы доставлять заказы?",
                    application.tariff,
                )
                self._fill_and_verify(
                    page.get_by_role("textbox", name=re.compile(r"^Фамилия")),
                    application.last_name,
                    "Фамилия",
                )
                self._fill_and_verify(
                    page.get_by_role("textbox", name=re.compile(r"^Имя")),
                    application.first_name,
                    "Имя",
                )
                self._select_option(page, "Гражданство", application.citizenship)
                self._fill_and_verify(
                    page.get_by_role("textbox", name=re.compile(r"^Телефон")),
                    application.phone,
                    "Телефон",
                )
                self._select_date(page, application.internship_date)
                consent = self._find_consent_checkbox(page)
                consent.check(timeout=self.timeout_ms)
                if not consent.is_checked():
                    raise FormSubmissionError("Форма не установила согласие на обработку данных")

                if not do_submit:
                    return

                captured_writes: list[dict[str, str | None]] = []
                if intercept_submission:

                    def intercept_write(route, request) -> None:
                        if request.method.upper() != "GET":
                            captured_writes.append(
                                {
                                    "method": request.method,
                                    "url": request.url,
                                    "post_data": request.post_data,
                                    "content_type": request.headers.get("content-type"),
                                }
                            )
                            route.abort()
                            return
                        route.continue_()

                    page.route("**/*", intercept_write)

                page.get_by_role("button", name="Отправить").click()
                if intercept_submission:
                    page.wait_for_timeout(min(3_000, self.timeout_ms))
                    self._verify_intercepted_submission(captured_writes, application)
                    return
                success_url = re.compile(r"/success(?:[/?#]|$)", re.IGNORECASE)
                try:
                    page.wait_for_url(success_url, timeout=self.timeout_ms)
                    return
                except PlaywrightTimeoutError:
                    pass

                success_pattern = re.compile(
                    os.getenv(
                        "YANDEX_FORM_SUCCESS_TEXT",
                        r"ответ отправлен|ответ записан|спасибо.*ответ",
                    ),
                    re.IGNORECASE,
                )
                try:
                    page.get_by_text(success_pattern).first.wait_for(
                        state="visible", timeout=min(5_000, self.timeout_ms)
                    )
                except PlaywrightTimeoutError as exc:
                    errors = page.locator('[role="alert"], [aria-invalid="true"]').all_inner_texts()
                    details = "; ".join(text.strip() for text in errors if text.strip())
                    suffix = f": {details}" if details else ""
                    error_type = FormSubmissionError if details else FormSubmissionUncertainError
                    raise error_type(
                        "Яндекс Форма не подтвердила сохранение ответа"
                        f" (текущий адрес: {page.url}){suffix}"
                    ) from exc
            finally:
                browser.close()

    @staticmethod
    def _verify_intercepted_submission(
        writes: list[dict[str, str | None]], application: CandidateApplication
    ) -> None:
        candidates = [
            write
            for write in writes
            if (write.get("method") or "").upper() == "POST"
            and "forms.yandex.ru" in (write.get("url") or "")
            and write.get("post_data")
        ]
        if len(candidates) != 1:
            raise FormSubmissionError(
                "Кнопка формы не сформировала единственный POST-запрос Яндекс Формы"
            )
        try:
            payload = json.loads(candidates[0]["post_data"] or "")
        except json.JSONDecodeError as exc:
            raise FormSubmissionError(
                "Яндекс Форма сформировала POST-запрос не в формате JSON"
            ) from exc

        serialized = json.dumps(payload, ensure_ascii=False)
        expected_date = datetime.strptime(
            application.internship_date, "%d.%m.%Y"
        ).strftime("%Y-%m-%d")
        missing = [
            label
            for label, value in (
                ("фамилия", application.last_name),
                ("имя", application.first_name),
                ("телефон", application.phone),
                ("дата", expected_date),
            )
            if value not in serialized
        ]
        if not any(value is True for value in _walk_json_values(payload)):
            missing.append("согласие")
        if missing:
            raise FormSubmissionError(
                "В перехваченном запросе формы отсутствуют поля: " + ", ".join(missing)
            )

    def _validate_url(self) -> None:
        if not self.form_url:
            raise FormConfigurationError("YANDEX_FORM_URL не настроен")
        if not self.form_url.startswith("https://forms.yandex.ru/"):
            raise FormConfigurationError("YANDEX_FORM_URL должен вести на forms.yandex.ru")

    @staticmethod
    def _find_consent_checkbox(page):
        """Find the single consent control without depending on its full wording."""
        by_label = page.get_by_role(
            "checkbox",
            name=re.compile(r"\bсогласие\b", re.IGNORECASE),
        )
        if by_label.count() == 1:
            return by_label

        by_field_type = page.locator(
            'input[type="checkbox"][name^="answer_boolean_"]'
        )
        if by_field_type.count() == 1:
            return by_field_type

        raise FormConfigurationError(
            "В Яндекс Форме не найден единственный чекбокс согласия; "
            "структура формы изменилась"
        )

    @staticmethod
    def _fill_and_verify(locator, value: str, label: str) -> None:
        locator.fill(value)
        actual = locator.input_value()
        if actual != value:
            raise FormSubmissionError(
                f"Поле «{label}» не приняло значение: ожидалось {value!r}, получено {actual!r}"
            )

    def _select_date(self, page, value: str) -> None:
        try:
            target = datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError as exc:
            raise FormSubmissionError(
                f"Дата стажировки должна быть в формате ДД.ММ.ГГГГ: {value!r}"
            ) from exc

        current = date.today().replace(day=1)
        target_month = target.replace(day=1)
        months_ahead = (target_month.year - current.year) * 12 + (
            target_month.month - current.month
        )
        if months_ahead < 0 or months_ahead > 24:
            raise FormSubmissionError(
                "Дата стажировки должна быть в пределах ближайших 24 месяцев"
            )

        page.get_by_role("button", name="Календарь").click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for(state="visible", timeout=self.timeout_ms)
        for _ in range(months_ahead):
            dialog.get_by_role("button", name="Вперёд", exact=True).click()

        label = self._russian_date_label(target)
        target_button = dialog.get_by_role("button", name=label, exact=True)
        if target_button.count() != 1:
            raise FormSubmissionError(
                f"В календаре не найдена дата «{label}»"
            )
        target_button.click()

        date_input = page.get_by_role("combobox", name="ДД.ММ.ГГГГ")
        actual_digits = re.sub(r"\D", "", date_input.input_value())
        expected_digits = target.strftime("%d%m%Y")
        if actual_digits != expected_digits:
            raise FormSubmissionError(
                f"Поле даты не приняло значение {value!r}: получено {date_input.input_value()!r}"
            )

    @staticmethod
    def _russian_date_label(value: date) -> str:
        weekdays = (
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        )
        months = (
            "",
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        )
        return f"{weekdays[value.weekday()]}, {value.day} {months[value.month]} {value.year} г."

    @staticmethod
    def _select_option(page, question: str, option: str) -> None:
        combobox = page.get_by_role("combobox", name=question).first
        if combobox.count() != 1:
            raise FormSubmissionError(f"В форме не найдено поле «{question}»")

        if combobox.evaluate("element => element.tagName.toLowerCase()") == "select":
            combobox.select_option(label=option)
            return

        combobox.click()
        filters = page.get_by_role("textbox", name="Фильтр")
        if filters.count():
            visible_filter = None
            for index in range(filters.count()):
                candidate = filters.nth(index)
                if candidate.is_visible():
                    visible_filter = candidate
            if visible_filter is not None:
                visible_filter.fill(option)
        choice = page.get_by_role("option", name=option, exact=True)
        if choice.count() != 1:
            raise FormSubmissionError(
                f"В поле «{question}» нет единственного варианта «{option}»"
            )
        choice.click()


def _walk_json_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_values(child)
    else:
        yield value
