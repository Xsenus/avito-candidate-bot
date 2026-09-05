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

PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\d)(?:\+?7|8)?(?:[\s().-]*\d){10}(?![\s().-]*\d)"
)
NAME_PART_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’-]*$")
NON_NAME_WORDS = {
    "работа",
    "только",
    "готов",
    "готова",
    "номер",
    "телефон",
    "стажировка",
    "сегодня",
    "завтра",
}

DATE_INPUT_ERROR_MESSAGE = "Укажите день недели или дату в формате ДД.ММ.ГГГГ"


def split_full_name(value: str) -> tuple[str, str]:
    """Return surname and first name from the candidate's answer."""
    raw = (value or "").strip()
    phone_matches = list(PHONE_CANDIDATE_RE.finditer(raw))
    if len(phone_matches) == 1:
        match = phone_matches[0]
        raw = raw[: match.start()] + " " + raw[match.end() :]
    parts = [
        part.strip(",.;:")
        for part in re.split(r"\s+", raw)
        if part.strip(",.;:")
    ]
    if len(parts) < 2:
        raise ValueError("Укажите фамилию и имя через пробел")
    if len(parts) > 3:
        raise ValueError("Укажите только фамилию, имя и при желании отчество")
    if any(not NAME_PART_RE.fullmatch(part) for part in parts):
        raise ValueError("Фамилия и имя должны содержать буквы, без номера телефона")
    if any(part.casefold() in NON_NAME_WORDS for part in parts):
        raise ValueError("Не удалось распознать ФИО. Напишите, например: Иванов Иван")
    if any(not part[0].isupper() for part in parts):
        raise ValueError("Напишите фамилию и имя с заглавной буквы, например: Иванов Иван")
    return parts[0], parts[1]


def normalize_phone(value: str) -> str:
    """Normalize a Russian phone number to +7XXXXXXXXXX."""
    raw = (value or "").strip()
    candidates = PHONE_CANDIDATE_RE.findall(raw)
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
            if days_ahead == 0:
                days_ahead = 7
            return base + timedelta(days=days_ahead)

    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = date.fromisoformat(
                "-".join(reversed(re.split(r"[./-]", cleaned)))
            ) if fmt == "%d.%m.%Y" else _parse_date(cleaned, fmt)
            if parsed < base:
                raise ValueError("Указанная дата уже прошла")
            return parsed
        except ValueError:
            continue

    raise ValueError(DATE_INPUT_ERROR_MESSAGE)


def _parse_date(value: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(value, fmt).date()
