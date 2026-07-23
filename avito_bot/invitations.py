from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import requests


DEFAULT_SHEET_ID = "1D6aP4Vjt05QMRIogvdtX0wKblbgnNrg-I8lF0Fq26zs"
DEFAULT_SHEET_GID = "420777109"
DATE_MARKER = "ДАТА"
AVITO_TEXT_LIMIT = 1000
DEFAULT_INVITATION_FOOTER = """Что взять с собой:
- Паспорт
- Заряженный смартфон

❗️За день до стажировки до 20:00 вам поступит информация по вашему бригадиру. Свяжитесь с ним утром, когда приедете на склад — он вас встретит.

Если планы изменятся — просто напишите мне.
До встречи!"""
QUESTIONS_LINE = "Остались вопросы? Пишите здесь или уточните уже на стажировке."


class HttpClient(Protocol):
    def get(self, url: str, *, timeout: int): ...


@dataclass(frozen=True)
class InvitationTemplate:
    service_center: str
    text: str

    def render(self, internship_date: date | str) -> str:
        formatted = format_invitation_date(internship_date)
        if not formatted:
            raise ValueError("Дата стажировки не указана")
        rendered = replace_date_marker(self.text, formatted)
        if "Что взять с собой:" not in rendered:
            footer = os.getenv("INVITATION_FOOTER_TEXT", "").strip()
            if not footer:
                footer = DEFAULT_INVITATION_FOOTER
            if os.getenv("INVITATION_INCLUDE_QUESTIONS_LINE", "false").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                footer = f"{footer}\n\n{QUESTIONS_LINE}"
            rendered = f"{rendered.rstrip()}\n\n{footer}"
        if len(rendered) > AVITO_TEXT_LIMIT:
            raise ValueError(
                f"Приглашение для СЦ «{self.service_center}» превышает "
                f"лимит Avito {AVITO_TEXT_LIMIT} символов"
            )
        return rendered


class InvitationCatalog:
    def __init__(self, templates: list[InvitationTemplate]) -> None:
        self._by_center: dict[str, InvitationTemplate] = {}
        for template in templates:
            key = normalize_service_center(template.service_center)
            if not key:
                continue
            if DATE_MARKER not in template.text:
                raise ValueError(
                    f"В приглашении для СЦ «{template.service_center}» нет маркера ДАТА"
                )
            if len(
                replace_date_marker(template.text, "31.12.")
                + "\n\n"
                + DEFAULT_INVITATION_FOOTER
            ) > AVITO_TEXT_LIMIT:
                raise ValueError(
                    f"Приглашение для СЦ «{template.service_center}» превышает "
                    f"лимит Avito {AVITO_TEXT_LIMIT} символов"
                )
            if key in self._by_center:
                raise ValueError(f"СЦ повторяется в таблице: {template.service_center}")
            self._by_center[key] = template

    def __len__(self) -> int:
        return len(self._by_center)

    def find(self, service_center: str) -> InvitationTemplate:
        key = normalize_service_center(service_center)
        try:
            return self._by_center[key]
        except KeyError as exc:
            raise LookupError(f"Приглашение для СЦ «{service_center}» не найдено") from exc

    @classmethod
    def from_csv(cls, content: str) -> "InvitationCatalog":
        rows = list(csv.reader(io.StringIO(content.lstrip("\ufeff"))))
        header_index = _find_header(rows)
        templates: list[InvitationTemplate] = []
        for row in rows[header_index + 1 :]:
            if len(row) < 2:
                continue
            center, text = row[0].strip(), row[1].strip()
            if center and text:
                templates.append(InvitationTemplate(center, text))
        return cls(templates)


class GoogleSheetInvitationSource:
    def __init__(
        self,
        sheet_id: str = DEFAULT_SHEET_ID,
        sheet_gid: str = DEFAULT_SHEET_GID,
        *,
        http: HttpClient | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
            raise ValueError("Некорректный ID Google-таблицы")
        if not sheet_gid.isdigit():
            raise ValueError("Некорректный gid листа")
        self.sheet_id = sheet_id
        self.sheet_gid = sheet_gid
        self.http = http or requests

    @property
    def csv_url(self) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/gviz/tq"
            f"?tqx=out:csv&gid={self.sheet_gid}"
        )

    def load(self) -> InvitationCatalog:
        response = self.http.get(self.csv_url, timeout=30)
        response.raise_for_status()
        return InvitationCatalog.from_csv(response.text)


def normalize_service_center(value: str) -> str:
    normalized = (value or "").strip().casefold().replace("ё", "е")
    normalized = re.sub(r"^сц[\s:_-]+", "", normalized)
    return re.sub(r"\s+", " ", normalized)


def format_invitation_date(internship_date: date | str) -> str:
    if isinstance(internship_date, date):
        return internship_date.strftime("%d.%m.")
    raw = str(internship_date or "").strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.", "%d.%m"):
        try:
            return datetime.strptime(raw, pattern).strftime("%d.%m.")
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw[:2]}.{raw[2:]}."
    return raw


def replace_date_marker(text: str, formatted: str) -> str:
    if formatted.endswith("."):
        return text.replace(f"{DATE_MARKER}.", formatted).replace(DATE_MARKER, formatted)
    return text.replace(DATE_MARKER, formatted)


def _find_header(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        normalized = [cell.strip().casefold() for cell in row[:2]]
        if normalized == ["сц", "текст сообщения"]:
            return index
    raise ValueError("В таблице нет колонок «СЦ» и «Текст сообщения»")
