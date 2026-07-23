from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WarehouseOption:
    number: int
    label: str
    service_center: str
    address: str


MOSCOW_WAREHOUSES = (
    WarehouseOption(
        1,
        "Железнодорожный",
        "Железнодорожный",
        "г. Балашиха, микрорайон Железнодорожный, улица Советская, "
        "владение 89, строение 1",
    ),
    WarehouseOption(2, "Запад", "Запад", "МСК, Бережковская наб. 20, стр. 9"),
    WarehouseOption(
        3,
        "Кувекино",
        "Кувекино",
        "д. Евсеево, ул. Евсеевская, д. 15, стр. 1",
    ),
    WarehouseOption(
        4,
        "Печатники",
        "Печатники",
        "Курьяновская набережная, 6с2",
    ),
    WarehouseOption(5, "Север", "Север", "Москва, Осташковское шоссе, 17А"),
    WarehouseOption(
        6,
        "Строгино",
        "Строгино",
        "Москва, 2-я Лыковская улица, д. 63, стр. 6",
    ),
    WarehouseOption(
        7,
        "СЦ Дзержинский",
        "Дзержинский",
        "Дзержинский, Садовая, 6",
    ),
    WarehouseOption(
        8,
        "СЦ Тарный",
        "Тарный",
        "Промышленная улица, 12А",
    ),
)

SAINT_PETERSBURG_WAREHOUSES = (
    WarehouseOption(
        1,
        "Троицкий",
        "Троицкий",
        "г. Санкт-Петербург, Запорожская улица, д. 12, строение 1. "
        "Заезд через КПП по адресу: проспект Обуховской Обороны, 295БЖ",
    ),
    WarehouseOption(
        2,
        "Бугры",
        "Бугры",
        "Бугровское сельское поселение, деревня Порошкино, "
        "23 км КАД (внутреннее кольцо), стр. 3",
    ),
)

WAREHOUSE_GROUPS = {
    "moscow": MOSCOW_WAREHOUSES,
    "saint_petersburg": SAINT_PETERSBURG_WAREHOUSES,
}

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
        "Подберите для себя склад, с этого склада у вас начинается рабочий день "
        "и там необходимо будет пройти стажировку:",
        "",
    ]
    for option in options:
        lines.extend(
            [f"{option.number}. {option.label}", f"   {option.address}", ""]
        )
    lines.extend(
        [
            "Выберите номер склада, который вам удобнее.",
            "",
            "0. Я передумал",
        ]
    )
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
