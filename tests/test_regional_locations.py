import pytest

from avito_bot.regional_locations import (
    GoogleSheetRegionalLocationSource,
    RegionalLocation,
    RegionalLocationCatalog,
    regional_initial_messages,
)


SHEET_CSV = """ ,ТИП ТС,ДРАЙВ,КГТ,ГИПЕРЛОК,ЭКСПРЕСС,На Драйв КГТ,На Заборку (ДО),На ДО Драйв,СЦ,Куда приглашать на стажировку,Ссылка яндекс карты,Как пройти,Дежурный номер на СЦ,Резервный номер СЦ,Время стажировки
Белгород,,,,,,,,,Белгород,"г. Белгород, ул Мичурина 104А",,,,,7:45:00
Краснодар,,,,,,,,,Краснодар,"г. Краснодар, х. Октябрьский, ул. Подсолнечная, 44",,,,,10:30:00
Краснодар ГИПЕР,,,,,,,,,Краснодар ПВЗ,ПВЗ тут,,,,,13:00:00
РОСТОВ-НА-ДОНУ,,,,,,,,,Ростов,"Аксайский район, Новочеркасское шоссе 111к2",,,,,10:30:00
"""


def test_catalog_parses_address_and_time_and_chooses_main_city_row():
    catalog = RegionalLocationCatalog.from_csv(SHEET_CSV)

    belgograd = catalog.resolve("Белгород", "item-1")
    krasnodar = catalog.resolve("Краснодар", "item-2")

    assert len(catalog) == 4
    assert belgograd.service_center == "Белгород"
    assert belgograd.address == "г. Белгород, ул Мичурина 104А"
    assert belgograd.internship_time == "7:45:00"
    assert krasnodar.service_center == "Краснодар"
    assert krasnodar.address.endswith("ул. Подсолнечная, 44")
    assert krasnodar.internship_time == "10:30:00"


@pytest.mark.parametrize("city", ["Кущевская", "Кущёвская", "Ростов-на-Дону"])
def test_catalog_reuses_existing_city_aliases(city):
    location = RegionalLocationCatalog.from_csv(SHEET_CSV).resolve(city, "item")

    assert location.service_center == "Ростов"
    assert location.internship_time == "10:30:00"


def test_item_override_is_used_before_city():
    catalog = RegionalLocationCatalog.from_csv(SHEET_CSV)

    location = catalog.resolve(
        "Неизвестный город",
        "special-item",
        {"special-item": "Белгород"},
    )

    assert location.service_center == "Белгород"


def test_unknown_or_ambiguous_city_fails_closed():
    catalog = RegionalLocationCatalog.from_csv(SHEET_CSV)

    with pytest.raises(LookupError, match="не определяется однозначно"):
        catalog.resolve("Неизвестный город", "item")


def test_regional_messages_match_requested_conditions():
    messages = regional_initial_messages(
        RegionalLocation(
            city="Белгород",
            service_center="Белгород",
            address="г. Белгород, ул Мичурина 104А",
            internship_time="7:45:00",
        )
    )

    assert len(messages) == 3
    assert messages[0].startswith("1. 🚚 Водитель")
    assert "от 4 400 ₽ за рейс" in messages[0]
    assert "Доход до 160 000 ₽ в месяц" in messages[0]
    assert messages[1].startswith("🛠 О работе")
    assert "📍 Адрес: г. Белгород, ул Мичурина 104А" in messages[2]
    assert messages[2].count("7:45:00") == 2
    assert messages[2].endswith("например: Вторник")
    assert all(len(message) <= 1000 for message in messages)


class FakeResponse:
    text = SHEET_CSV

    @staticmethod
    def raise_for_status():
        return None


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        return FakeResponse()


def test_google_sheet_source_uses_configured_gid():
    client = FakeHttpClient()
    source = GoogleSheetRegionalLocationSource(
        "sheet-id",
        "sheet-gid",
        http_client=client,
        timeout=17,
    )

    catalog = source.load()

    assert len(catalog) == 4
    assert client.calls == [
        (
            "https://docs.google.com/spreadsheets/d/"
            "sheet-id/export?format=csv&gid=sheet-gid",
            17,
        )
    ]


def test_google_sheet_source_updates_and_falls_back_to_cache(tmp_path):
    cache_path = tmp_path / "regional.csv"
    live_client = FakeHttpClient()
    live_source = GoogleSheetRegionalLocationSource(
        http_client=live_client
    )

    live_catalog = live_source.load(cache_path)

    assert len(live_catalog) == 4
    assert cache_path.read_text(encoding="utf-8") == SHEET_CSV
    assert not live_source.last_load_used_cache

    class FailingHttpClient:
        @staticmethod
        def get(url, timeout):
            raise OSError("Google is temporarily unavailable")

    cached_source = GoogleSheetRegionalLocationSource(
        http_client=FailingHttpClient()
    )

    cached_catalog = cached_source.load(cache_path)

    assert cached_catalog.resolve("Белгород", "item").internship_time == "7:45:00"
    assert cached_source.last_load_used_cache
