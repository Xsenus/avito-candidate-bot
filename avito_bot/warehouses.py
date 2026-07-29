from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WarehouseOption:
    number: int
    label: str
    service_center: str
    address: str
    internship_time: str


MOSCOW_WAREHOUSES = (
    WarehouseOption(
        1,
        "Железнодорожный",
        "Железнодорожный",
        "г. Балашиха, микрорайон Железнодорожный, улица Советская, "
        "владение 89, строение 1",
        "7:00:00",
    ),
    WarehouseOption(
        2,
        "Запад",
        "Запад",
        "МСК, Бережковская наб 20 стр 9",
        "7:00:00",
    ),
    WarehouseOption(
        3,
        "Кувекино",
        "Кувекино",
        "Москва, район Троицк, деревня Евсеево, Евсеевская улица, 15",
        "8:00:00",
    ),
    WarehouseOption(
        4,
        "Печатники",
        "Печатники",
        "Курьяновская набережная, 6с2",
        "7:30:00",
    ),
    WarehouseOption(
        5,
        "Север",
        "Север",
        "Москва Осташковское шоссе 17а",
        "7:30:00",
    ),
    WarehouseOption(
        6,
        "Строгино",
        "Строгино",
        "Москва, 2-я Лыковская улица, д63, стр 6",
        "8:30:00",
    ),
    WarehouseOption(
        7,
        "Дзержинский",
        "Дзержинский",
        "Дзержинский, Садовая, 6",
        "7:30:00",
    ),
    WarehouseOption(
        8,
        "СЦ Тарный",
        "Тарный",
        "г. Москва ул. Промышленная улица, 12А",
        "7:30:00",
    ),
)

SAINT_PETERSBURG_WAREHOUSES = (
    WarehouseOption(
        1,
        "Троицкий",
        "Троицкий",
        "г. Санкт-Петербург, Запорожская улица, д.12, строение 1, "
        "Заезд через КПП по адресу: Проспект Обуховской Обороны, 295БЖ",
        "7:30:00",
    ),
    WarehouseOption(
        2,
        "Запад",
        "Бугры",
        "Бугровское сельское поселение, деревня Порошкино, "
        "23 км КАД (внутреннее кольцо) ул., стр. 3",
        "7:00:00",
    ),
)

WAREHOUSE_GROUPS = {
    "moscow": MOSCOW_WAREHOUSES,
    "saint_petersburg": SAINT_PETERSBURG_WAREHOUSES,
}


def replace_warehouse_groups(
    groups: dict[str, tuple[WarehouseOption, ...]],
) -> None:
    WAREHOUSE_GROUPS.clear()
    WAREHOUSE_GROUPS.update(groups)

MOSCOW_CITY_ALIASES = {
    "москва",
    "мск",
    "мытищи",
    "дзержинский",
    "подольск",
}

SAINT_PETERSBURG_CITY_ALIASES = {
    "санкт петербург",
    "спб",
    "питер",
    "бугры",
}


def normalize_location(value: str | None) -> str:
    normalized = (value or "").strip().casefold().replace("ё", "е")
    normalized = re.sub(r"\b(?:город|г|поселок|посёлок|п)\.?\s+", "", normalized)
    normalized = normalized.replace("-", " ")
    normalized = re.sub(r"[^a-zа-я0-9 ]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _contains_location_alias(normalized: str, aliases: set[str]) -> bool:
    words = normalized.split()
    for alias in aliases:
        alias_words = alias.split()
        width = len(alias_words)
        if any(words[index : index + width] == alias_words for index in range(len(words))):
            return True
    return False


def warehouse_group_for_city(city: str | None) -> str | None:
    normalized = normalize_location(city)
    if _contains_location_alias(normalized, MOSCOW_CITY_ALIASES):
        return "moscow"
    if _contains_location_alias(normalized, SAINT_PETERSBURG_CITY_ALIASES):
        return "saint_petersburg"
    return None


def warehouses_for_city(city: str | None) -> tuple[WarehouseOption, ...]:
    group = warehouse_group_for_city(city)
    return WAREHOUSE_GROUPS.get(group, ())


def warehouse_prompt_for_city(city: str | None) -> str | None:
    options = warehouses_for_city(city)
    if not options:
        return None
    lines = [
        "Подобрали для вас склады — выберите удобный номером:",
        "",
    ]
    for option in options:
        lines.extend(
            [
                f"{option.number}. {option.label}",
                f"   📍 {option.address}",
                f"   🕥 Стажировка в {option.internship_time}",
                "",
            ]
        )
    max_number = options[-1].number
    lines.append(f"Напишите номер подходящего склада (1–{max_number}) 👇")
    return "\n".join(lines)


def parse_warehouse_choice(
    text: str, city: str | None
) -> WarehouseOption | int | None:
    options = warehouses_for_city(city)
    if not options:
        return None
    normalized = normalize_location(text)
    number_match = re.search(r"(?<!\d)(\d+)(?!\d)", normalized)
    if number_match:
        number = int(number_match.group(1))
        if number == 0:
            return 0
        return next((option for option in options if option.number == number), None)
    for option in options:
        if normalize_location(option.label) in normalized:
            return option
    return None
