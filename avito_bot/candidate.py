from __future__ import annotations

import re
from datetime import date, timedelta


WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
}


def split_full_name(value: str) -> tuple[str, str]:
    """Return surname and first name from the candidate's answer."""
    parts = [part for part in re.split(r"\s+", (value or "").strip()) if part]
    if len(parts) < 2:
        raise ValueError("Укажите фамилию и имя через пробел")
    return parts[0], parts[1]


def normalize_phone(value: str) -> str:
    """Normalize a Russian phone number to +7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        raise ValueError("Введите российский номер из 10 или 11 цифр")
    return "+" + digits


def resolve_internship_date(value: str, *, today: date | None = None) -> date:
    """Resolve a Russian relative day or weekday to the nearest future date."""
    base = today or date.today()
    cleaned = re.sub(r"[^а-яё0-9.\-/\s]", "", (value or "").strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if "послезавтра" in cleaned:
        return base + timedelta(days=2)
    if "завтра" in cleaned:
        return base + timedelta(days=1)
    if "сегодня" in cleaned:
        return base

    for word, weekday in WEEKDAYS.items():
        if word in cleaned:
            days_ahead = (weekday - base.weekday()) % 7
            return base + timedelta(days=days_ahead)

    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return date.fromisoformat(
                "-".join(reversed(re.split(r"[./-]", cleaned)))
            ) if fmt == "%d.%m.%Y" else _parse_date(cleaned, fmt)
        except ValueError:
            continue

    raise ValueError("Укажите день недели или дату в формате ДД.ММ.ГГГГ")


def _parse_date(value: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(value, fmt).date()
