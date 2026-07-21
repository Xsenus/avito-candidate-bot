from __future__ import annotations

import os
import re
from dataclasses import dataclass


class FormConfigurationError(RuntimeError):
    pass


class FormSubmissionError(RuntimeError):
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

    def _run(self, application: CandidateApplication, *, do_submit: bool) -> None:
        application.validate()
        if not self.form_url:
            raise FormConfigurationError("YANDEX_FORM_URL не настроен")
        if not self.form_url.startswith("https://forms.yandex.ru/"):
            raise FormConfigurationError("YANDEX_FORM_URL должен вести на forms.yandex.ru")

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
                page.get_by_role("textbox", name=re.compile(r"^Фамилия")).fill(
                    application.last_name
                )
                page.get_by_role("textbox", name=re.compile(r"^Имя")).fill(
                    application.first_name
                )
                self._select_option(page, "Гражданство", application.citizenship)
                page.get_by_role("textbox", name=re.compile(r"^Телефон")).fill(
                    application.phone
                )
                page.get_by_role("combobox", name="ДД.ММ.ГГГГ").fill(
                    application.internship_date
                )
                page.get_by_role(
                    "checkbox",
                    name=re.compile(r"согласие на обработку", re.IGNORECASE),
                ).check()

                if not do_submit:
                    return

                page.get_by_role("button", name="Отправить").click()
                success_pattern = re.compile(
                    os.getenv(
                        "YANDEX_FORM_SUCCESS_TEXT",
                        r"ответ отправлен|ответ записан|спасибо.*ответ",
                    ),
                    re.IGNORECASE,
                )
                try:
                    page.get_by_text(success_pattern).first.wait_for(
                        state="visible", timeout=self.timeout_ms
                    )
                except PlaywrightTimeoutError as exc:
                    errors = page.locator('[role="alert"], [aria-invalid="true"]').all_inner_texts()
                    details = "; ".join(text.strip() for text in errors if text.strip())
                    suffix = f": {details}" if details else ""
                    raise FormSubmissionError(
                        f"Яндекс Форма не подтвердила сохранение ответа{suffix}"
                    ) from exc
            finally:
                browser.close()

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
            filters.last.fill(option)
        choice = page.get_by_role("option", name=option, exact=True)
        if choice.count() != 1:
            raise FormSubmissionError(
                f"В поле «{question}» нет единственного варианта «{option}»"
            )
        choice.click()
