import pytest

from avito_bot.warehouses import (
    parse_warehouse_choice,
    warehouse_group_for_city,
    warehouse_prompt_for_city,
)


@pytest.mark.parametrize(
    "city",
    [
        "Москва",
        "г. Москва",
        "МСК",
        "Мытищи",
        "Дзержинский",
        "Подольск",
        "Балашиха",
        "Красногорск",
        "Видное",
        "Домодедово",
        "Люберцы",
        "Королёв",
        "Королев",
        "Лыткарино",
        "Железнодорожный",
        "Пушкино",
        "Московская область, городской округ Красногорск",
        "Балашиха, микрорайон Железнодорожный",
    ],
)
def test_moscow_region_cities_use_six_warehouses(city):
    assert warehouse_group_for_city(city) == "moscow"
    prompt = warehouse_prompt_for_city(city)
    assert "1. Железнодорожный" in prompt
    assert "6. Строгино" in prompt
    assert "7. Дзержинский" not in prompt
    assert "8. СЦ Тарный" not in prompt
    assert "(1–6)" in prompt
    assert "🕥 Стажировка в 7:00:00" in prompt
    assert "🕥 Стажировка в 8:30:00" in prompt
    assert "Напишите номер подходящего склада (1–6)" in prompt
    assert (
        "1. Железнодорожный\n"
        "   📍 г. Балашиха, микрорайон Железнодорожный, улица Советская, "
        "владение 89, строение 1\n"
        "   🕥 Стажировка в 7:00:00\n\n"
        "2. Запад"
    ) in prompt
    assert len(prompt) <= 1000


@pytest.mark.parametrize(
    "city", ["Санкт-Петербург", "г. Санкт Петербург", "СПб", "Питер", "Бугры"]
)
def test_saint_petersburg_region_uses_two_warehouses(city):
    assert warehouse_group_for_city(city) == "saint_petersburg"
    prompt = warehouse_prompt_for_city(city)
    assert "1. Троицкий" in prompt
    assert "2. Бугры" in prompt
    assert "2. Запад" not in prompt
    assert "🕥 Стажировка в 7:30:00" in prompt
    assert "🕥 Стажировка в 7:00:00" in prompt
    assert "Напишите номер подходящего склада (1–2)" in prompt
    assert "3." not in prompt
    assert len(prompt) <= 1000


def test_troitsky_address_matches_customer_text_exactly():
    prompt = warehouse_prompt_for_city("Санкт-Петербург")

    assert (
        "📍 г. Санкт-Петербург, Запорожская улица, д.12, строение 1, "
        "Заезд через КПП по адресу: Проспект Обуховской Обороны, 295БЖ"
    ) in prompt


@pytest.mark.parametrize(
    "city",
    ["Кемерово", "Томск", "Омск", "Московская область", "Питерский район"],
)
def test_other_city_does_not_get_a_warehouse_prompt(city):
    assert warehouse_group_for_city(city) is None
    assert warehouse_prompt_for_city(city) is None


@pytest.mark.parametrize(
    "city",
    ["Королевский район", "Пушкин", "Люберецкий район", "Балашов"],
)
def test_similar_location_names_do_not_match_moscow_region_aliases(city):
    assert warehouse_group_for_city(city) is None


def test_warehouse_choice_accepts_number_and_name():
    assert parse_warehouse_choice("номер 4", "Москва").service_center == "Печатники"
    assert parse_warehouse_choice("Строгино", "Москва").number == 6
    assert parse_warehouse_choice("2", "Бугры").service_center == "Бугры"
    assert parse_warehouse_choice("0", "Москва") == 0
    assert parse_warehouse_choice("99", "Москва") is None


@pytest.mark.parametrize(
    "answer",
    ["Бугры", "Бугры?", "склад Бугры", "Запад", "Запад?"],
)
def test_saint_petersburg_bugry_accepts_current_and_legacy_names(answer):
    choice = parse_warehouse_choice(answer, "Санкт-Петербург")

    assert choice.number == 2
    assert choice.label == "Бугры"
    assert choice.service_center == "Бугры"


def test_warehouse_choice_does_not_match_part_of_another_word():
    assert parse_warehouse_choice("Западный район", "Санкт-Петербург") is None
