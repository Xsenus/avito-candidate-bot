import pytest

from avito_bot.regional_locations import (
    GoogleSheetRegionalLocationSource,
    RegionalLocation,
    RegionalLocationCatalog,
    RefreshingRegionalLocationProvider,
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


def test_kstovo_uses_nizhny_novgorod_location():
    catalog = RegionalLocationCatalog.from_csv(
        SHEET_CSV
        + 'Нижний Новгород,,,,,,,,,Нижний Новгород,'
        '"Нижний Новгород, Московское шоссе, 52",,,,,10:30:00\n'
    )

    location = catalog.resolve("Кстово", "item")

    assert location.service_center == "Нижний Новгород"
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

    assert len(messages) == 4
    assert messages[0].startswith("1. 🚚 Водитель")
    assert "от 4 400 ₽ за рейс" in messages[0]
    assert "Доход до 160 000 ₽ в месяц" in messages[0]
    assert messages[1].startswith("🛠 О работе")
    assert "📍 Адрес: г. Белгород, ул Мичурина 104А" in messages[2]
    assert messages[2].count("7:45:00") == 1
    assert messages[2].endswith("7:45:00")
    assert messages[3] == (
        "Стажировка каждый день в 7:45:00, на какой день вас записать? "
        "Укажите день недели, например: Вторник"
    )
    assert all(len(message) <= 1000 for message in messages)


def test_first_two_regional_messages_keep_customer_spacing():
    first, second, _, _ = regional_initial_messages(
        RegionalLocation(
            city="Белгород",
            service_center="Белгород",
            address="г. Белгород, ул Мичурина 104А",
            internship_time="7:45:00",
        )
    )

    assert first.startswith(
        "1. 🚚 Водитель в Яндекс Маркет (на авто компании)\n\n"
        "Мы предлагаем работу"
    )
    assert "\n\n💰 Условия и доход\n\n• Ваша прибыль" in first
    assert "\n\n• Прозрачная оплата" in first
    assert "\n\n• Высокий потенциал" in first
    assert second.startswith("🛠 О работе\n\n• Задачи")
    assert "\n\n• Комфорт" in second
    assert "\n\n• График" in second
    assert "\n\n📝 Что требуется от вас?\n\n• Стаж" in second
    assert "\n\n❌ Мы убрали" in second
    assert "\n\n✅ Вы выходите" in second
    assert "\n\n📍 Обучение" in second
    assert "\n\n\n" not in first
    assert "\n\n\n" not in second


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


def test_provider_refreshes_when_due_and_keeps_last_valid_catalog(tmp_path):
    class MutableHttpClient:
        def __init__(self):
            self.text = SHEET_CSV
            self.fail = False

        def get(self, url, timeout):
            if self.fail:
                raise OSError("Google is temporarily unavailable")
            response = FakeResponse()
            response.text = self.text
            return response

    now = [0.0]
    http = MutableHttpClient()
    source = GoogleSheetRegionalLocationSource(http_client=http)
    cache_path = tmp_path / "regional.csv"
    provider = RefreshingRegionalLocationProvider(
        source,
        cache_path,
        300,
        clock=lambda: now[0],
    )

    assert (
        provider.catalog.resolve("Краснодар", "item").internship_time
        == "10:30:00"
    )
    http.text = SHEET_CSV.replace("10:30:00", "9:00:00")
    now[0] = 299
    assert not provider.refresh_if_due()
    assert (
        provider.catalog.resolve("Краснодар", "item").internship_time
        == "10:30:00"
    )

    now[0] = 300
    assert provider.refresh_if_due()
    assert provider.catalog.resolve(
        "Краснодар", "item"
    ).internship_time == "9:00:00"

    cache_path.unlink()
    http.fail = True
    now[0] = 600
    assert not provider.refresh_if_due()
    assert provider.last_error is not None
    assert provider.catalog.resolve(
        "Краснодар", "item"
    ).internship_time == "9:00:00"
