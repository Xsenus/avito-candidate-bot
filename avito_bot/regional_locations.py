from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import requests

from .invitations import normalize_service_center
from .service_centers import CITY_ALIASES, normalize_city


DEFAULT_SHEET_ID = "1D6aP4Vjt05QMRIogvdtX0wKblbgnNrg-I8lF0Fq26zs"
DEFAULT_SHEET_GID = "1350376870"
AVITO_TEXT_LIMIT = 1000
TIME_PATTERN = re.compile(r"\d{1,2}:\d{2}:\d{2}")


class HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    def get(self, url: str, *, timeout: int) -> HttpResponse: ...


@dataclass(frozen=True)
class RegionalLocation:
    city: str
    service_center: str
    address: str
    internship_time: str


class RegionalLocationCatalog:
    def __init__(self, locations: list[RegionalLocation]) -> None:
        if not locations:
            raise ValueError("Таблица региональных складов пуста")

        self._by_center: dict[str, RegionalLocation] = {}
        self._by_city: dict[str, list[RegionalLocation]] = {}
        for location in locations:
            center_key = normalize_service_center(location.service_center)
            if center_key in self._by_center:
                raise ValueError(
                    f"СЦ повторяется в таблице адресов: {location.service_center}"
                )
            self._by_center[center_key] = location
            city_key = normalize_city(location.city).casefold()
            self._by_city.setdefault(city_key, []).append(location)

    def __len__(self) -> int:
        return len(self._by_center)

    @classmethod
    def from_csv(cls, content: str) -> "RegionalLocationCatalog":
        reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
        try:
            headers = next(reader)
        except StopIteration as exc:
            raise ValueError("Таблица региональных складов пуста") from exc

        normalized_headers = [header.strip().casefold() for header in headers]
        center_index = _find_header(normalized_headers, "сц")
        address_index = _find_header(
            normalized_headers, "куда приглашать на стажировку"
        )
        time_index = _find_header(normalized_headers, "время стажировки")

        locations: list[RegionalLocation] = []
        for row in reader:
            if not row:
                continue
            padded = row + [""] * (len(headers) - len(row))
            city = padded[0].strip()
            service_center = padded[center_index].strip()
            address = " ".join(padded[address_index].split())
            raw_time = padded[time_index].strip()
            time_match = TIME_PATTERN.search(raw_time)
            if not city or not service_center or not address or time_match is None:
                continue
            locations.append(
                RegionalLocation(
                    city=city,
                    service_center=service_center.removeprefix("СЦ ").strip(),
                    address=address,
                    internship_time=time_match.group(0),
                )
            )
        return cls(locations)

    def resolve(
        self,
        city: str | None,
        item_id: str | None,
        overrides: dict[str, str] | None = None,
    ) -> RegionalLocation:
        configured = overrides or {}
        normalized_city = normalize_city(city)
        target = configured.get(str(item_id or "").strip())
        if not target and normalized_city:
            target = configured.get(normalized_city.casefold())
        if not target and normalized_city:
            target = CITY_ALIASES.get(normalized_city.casefold(), normalized_city)
        if not target:
            raise LookupError("В объявлении Avito не указан город")

        by_center = self._by_center.get(normalize_service_center(target))
        if by_center:
            return by_center

        city_matches = self._by_city.get(normalize_city(target).casefold(), [])
        if len(city_matches) == 1:
            return city_matches[0]
        raise LookupError(
            f"Для города «{normalized_city or target}» склад не определяется "
            "однозначно; добавьте ID объявления в "
            "SERVICE_CENTER_OVERRIDES_JSON"
        )


class GoogleSheetRegionalLocationSource:
    def __init__(
        self,
        sheet_id: str = DEFAULT_SHEET_ID,
        gid: str = DEFAULT_SHEET_GID,
        *,
        http_client: HttpClient = cast(HttpClient, requests),
        timeout: int = 30,
    ) -> None:
        self.sheet_id = sheet_id
        self.gid = gid
        self.http_client = http_client
        self.timeout = timeout
        self.last_load_used_cache = False

    @property
    def csv_url(self) -> str:
        return (
            "https://docs.google.com/spreadsheets/d/"
            f"{self.sheet_id}/export?format=csv&gid={self.gid}"
        )

    def load(
        self, cache_path: str | Path | None = None
    ) -> RegionalLocationCatalog:
        try:
            response = self.http_client.get(self.csv_url, timeout=self.timeout)
            response.raise_for_status()
            catalog = RegionalLocationCatalog.from_csv(response.text)
        except Exception:
            if cache_path is None or not Path(cache_path).is_file():
                raise
            self.last_load_used_cache = True
            return RegionalLocationCatalog.from_csv(
                Path(cache_path).read_text(encoding="utf-8")
            )

        self.last_load_used_cache = False
        if cache_path is not None:
            destination = Path(cache_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(response.text, encoding="utf-8")
            temporary.replace(destination)
        return catalog


class RefreshingRegionalLocationProvider:
    def __init__(
        self,
        source: GoogleSheetRegionalLocationSource,
        cache_path: str | Path,
        refresh_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.cache_path = Path(cache_path)
        self.refresh_interval_seconds = max(
            1.0, float(refresh_interval_seconds)
        )
        self.clock = clock
        self.catalog = source.load(self.cache_path)
        self.next_refresh_at = self.clock() + self.refresh_interval_seconds
        self.last_error: Exception | None = None

    def refresh_if_due(self) -> bool:
        now = self.clock()
        if now < self.next_refresh_at:
            return False
        self.next_refresh_at = now + self.refresh_interval_seconds
        try:
            refreshed = self.source.load(self.cache_path)
        except Exception as exc:
            self.last_error = exc
            return False
        self.catalog = refreshed
        self.last_error = None
        return True


def regional_initial_messages(
    location: RegionalLocation,
) -> tuple[str, str, str, str]:
    first = (
        "1. 🚚 Водитель в Яндекс Маркет (на авто компании)\n\n"
        "Мы предлагаем работу на комфортных фургонах Ford Transit (МКПП). "
        "🕶 Все расходы мы берем на себя — вы просто зарабатываете.\n\n"
        "💰 Условия и доход\n\n"
        "• Ваша прибыль — это чистый доход: Мы полностью оплачиваем бензин, "
        "парковки и техническое обслуживание.\n\n"
        "• Прозрачная оплата: от 4 400 ₽ за рейс.\n\n"
        "• Высокий потенциал: Доход до 160 000 ₽ в месяц."
    )
    second = (
        "🛠 О работе\n\n"
        "• Задачи: Утренняя загрузка на складе и доставка мелкогабаритных "
        "посылок по ПВЗ и постаматам. Возможна вторая загрузка после обеда.\n\n"
        "• Комфорт: Возможно домашнее хранение автомобиля.\n\n"
        "• График: Вы сами выбираете удобные дни для работы.\n\n"
        "📝 Что требуется от вас?\n\n"
        "• Стаж вождения — более 2х лет.\n\n"
        "❌ Мы убрали все барьеры для старта: вам не нужно тратить деньги на "
        "аренду машины или топливо.\n\n"
        "✅ Вы выходите на смену, выполняете рейсы и забираете честно "
        "заработанные деньги.\n\n"
        "📍 Обучение: утром встреча с бригадиром — за 4–6 часов узнаете всё о "
        "работе изнутри. Оформление документов сразу после обучения."
    )
    third = (
        "Подобрали для вас склад в вашем городе:\n\n"
        f"📍 Адрес: {location.address}\n\n"
        f"🕥 Стажировка начинается в {location.internship_time}"
    )
    fourth = (
        f"Стажировка каждый день в {location.internship_time}, на какой день "
        "вас записать? Укажите день недели, например: Вторник"
    )
    messages = (first, second, third, fourth)
    if any(len(message) > AVITO_TEXT_LIMIT for message in messages):
        raise ValueError("Региональное сообщение превышает лимит Avito")
    return messages


def _find_header(headers: list[str], expected: str) -> int:
    try:
        return headers.index(expected)
    except ValueError as exc:
        raise ValueError(
            f"В таблице региональных складов нет столбца «{expected}»"
        ) from exc
