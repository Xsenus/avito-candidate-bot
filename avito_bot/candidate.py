from __future__ import annotations

import re
from datetime import date, timedelta


WEEKDAY_PATTERNS = {
    0: (r"понедельник(?:а|е)?", r"пн"),
    1: (r"вторник(?:а|е)?", r"вт"),
    2: (r"сред(?:а|у|ы|е)", r"ср"),
    3: (r"четверг(?:а|е)?", r"четвер", r"чт"),
    4: (r"пятниц(?:а|у|ы|е)", r"пт"),
    5: (r"суббот(?:а|у|ы|е)", r"сб"),
    6: (r"воскресень(?:е|я|ю|и)", r"вс"),
}


def split_full_name(value: str) -> tuple[str, str]:
    """Return surname and first name from the candidate's answer."""
    parts = [part for part in re.split(r"\s+", (value or "").strip()) if part]
    if len(parts) < 2:
        raise ValueError("Укажите фамилию и имя через пробел")
    return parts[0], parts[1]


def normalize_phone(value: str) -> str:
    """Normalize a Russian phone number to +7XXXXXXXXXX."""
    raw = (value or "").strip()
    candidates = re.findall(
        r"(?<!\d)(?:\+?7|8)?(?:[\s().-]*\d){10}(?![\s().-]*\d)", raw
    )
    digits = re.sub(r"\D", "", candidates[0] if len(candidates) == 1 else raw)
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        raise ValueError("Введите российский номер из 10 или 11 цифр")
    return "+" + digits


def resolve_internship_date(value: str, *, today: date | None = None) -> date:
    """Resolve a Russian relative day or weekday to its nearest occurrence."""
    base = today or date.today()
    cleaned = (value or "").strip().lower().replace("ё", "е")
    cleaned = re.sub(r"[^а-я0-9.\-/\s]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if "послезавтра" in cleaned:
        return base + timedelta(days=2)
    if "завтра" in cleaned:
        return base + timedelta(days=1)
    if "сегодня" in cleaned:
        return base

    for weekday, patterns in WEEKDAY_PATTERNS.items():
        if any(re.search(rf"\b(?:{pattern})\b", cleaned) for pattern in patterns):
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
