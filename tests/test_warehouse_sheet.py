from pathlib import Path

import pytest

from avito_bot.warehouse_sheet import (
    GoogleSheetWarehouseSource,
    RefreshingWarehouseProvider,
    WarehouseSheetCatalog,
)
from avito_bot.warehouses import WAREHOUSE_GROUPS


HEADERS = (
    "Город,Парк 1,Парк 2,Парк 3,Парк 4,Парк 5,Парк 6,Парк 7,"
    "Парк 8,Парк 9,СЦ,Куда приглашать на стажировку,Карта,"
    "Инструкция,Телефон 1,Телефон 2,Время стажировки\n"
)


def csv_for_groups(*, kuvekino_time: str = "7:30:00") -> str:
    rows = []
    values = {
        "Железнодорожный": ("Адрес 1", "7:00:00"),
        "Запад": ("Адрес 2", "7:00:00"),
        "Кувекино": ("Адрес 3", kuvekino_time),
        "Печатники": ("Адрес 4", "7:30:00"),
        "Север": ("Адрес 5", "7:30:00"),
        "Строгино": ("Адрес 6", "8:30:00"),
        "Дзержинский": ("Адрес 7", "7:30:00"),
        "Тарный": ("Адрес 8", "7:30:00"),
        "Троицкий": ("Адрес 9", "7:30:00"),
        "Бугры": ("Адрес 10", "7:00:00"),
    }
    for center, (address, internship_time) in values.items():
        rows.append(
            f"Город,,,,,,,,,,{center},{address},,,,,{internship_time}\n"
        )
    return HEADERS + "".join(rows)


class Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class HttpClient:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses

    def get(self, url: str, *, timeout: int) -> Response:
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


def test_catalog_overlays_addresses_and_times_without_changing_order():
    groups = WarehouseSheetCatalog.from_csv(
        csv_for_groups(kuvekino_time="9:00:00")
    ).overlay(WAREHOUSE_GROUPS)

    assert [option.label for option in groups["moscow"]] == [
        option.label for option in WAREHOUSE_GROUPS["moscow"]
    ]
    assert groups["moscow"][2].address == "Адрес 3"
    assert groups["moscow"][2].internship_time == "9:00:00"
    assert groups["saint_petersburg"][1].service_center == "Бугры"


def test_catalog_rejects_missing_required_warehouse():
    incomplete = csv_for_groups().replace(
        "Город,,,,,,,,,,Кувекино,Адрес 3,,,,,7:30:00\n", ""
    )

    with pytest.raises(ValueError, match="Кувекино"):
        WarehouseSheetCatalog.from_csv(incomplete).overlay(WAREHOUSE_GROUPS)


def test_provider_refreshes_when_due(tmp_path: Path):
    now = [100.0]
    source = GoogleSheetWarehouseSource(
        http_client=HttpClient(
            [csv_for_groups(), csv_for_groups(kuvekino_time="9:00:00")]
        )
    )
    provider = RefreshingWarehouseProvider(
        source,
        tmp_path / "locations.csv",
        300,
        WAREHOUSE_GROUPS,
        clock=lambda: now[0],
    )

    assert provider.groups["moscow"][2].internship_time == "7:30:00"
    now[0] = 399.0
    assert provider.refresh_if_due() is False
    now[0] = 400.0
    assert provider.refresh_if_due() is True
    assert provider.groups["moscow"][2].internship_time == "9:00:00"


def test_invalid_refresh_keeps_valid_memory_and_cache(tmp_path: Path):
    cache = tmp_path / "locations.csv"
    invalid = csv_for_groups().replace(
        "Город,,,,,,,,,,Кувекино,Адрес 3,,,,,7:30:00\n", ""
    )
    now = [100.0]
    source = GoogleSheetWarehouseSource(
        http_client=HttpClient([csv_for_groups(), invalid])
    )
    provider = RefreshingWarehouseProvider(
        source,
        cache,
        300,
        WAREHOUSE_GROUPS,
        clock=lambda: now[0],
    )
    original_cache = cache.read_text(encoding="utf-8")

    now[0] = 400.0
    assert provider.refresh_if_due() is True
    assert source.last_load_used_cache is True
    assert provider.groups["moscow"][2].internship_time == "7:30:00"
    assert cache.read_text(encoding="utf-8") == original_cache
