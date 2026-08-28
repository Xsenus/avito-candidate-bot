from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

import requests

from .warehouses import WarehouseOption, normalize_location


DEFAULT_SHEET_ID = "1D6aP4Vjt05QMRIogvdtX0wKblbgnNrg-I8lF0Fq26zs"
DEFAULT_SHEET_GID = "1350376870"
TIME_PATTERN = re.compile(r"\d{1,2}:\d{2}:\d{2}")


class HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    def get(self, url: str, *, timeout: int) -> HttpResponse: ...


class WarehouseSheetCatalog:
    def __init__(self, rows: Mapping[str, tuple[str, str]]) -> None:
        if not rows:
            raise ValueError("Таблица складов пуста")
        self._rows = dict(rows)

    @classmethod
    def from_csv(cls, content: str) -> "WarehouseSheetCatalog":
        reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
        try:
            headers = next(reader)
        except StopIteration as exc:
            raise ValueError("Таблица складов пуста") from exc

        normalized_headers = [header.strip().casefold() for header in headers]
        center_index = _find_header(normalized_headers, "сц")
        address_index = _find_header(
            normalized_headers, "куда приглашать на стажировку"
        )
        time_index = _find_header(normalized_headers, "время стажировки")

        rows: dict[str, tuple[str, str]] = {}
        for row in reader:
            padded = row + [""] * (len(headers) - len(row))
            center = padded[center_index].strip().removeprefix("СЦ ").strip()
            address = " ".join(padded[address_index].split())
            time_match = TIME_PATTERN.search(padded[time_index].strip())
            if not center or not address or time_match is None:
                continue
            key = normalize_location(center)
            if key in rows:
                raise ValueError(f"СЦ повторяется в таблице: {center}")
            rows[key] = (address, time_match.group(0))
        return cls(rows)

    def overlay(
        self,
        groups: Mapping[str, tuple[WarehouseOption, ...]],
    ) -> dict[str, tuple[WarehouseOption, ...]]:
        refreshed: dict[str, tuple[WarehouseOption, ...]] = {}
        missing: list[str] = []
        for group, options in groups.items():
            updated: list[WarehouseOption] = []
            for option in options:
                row = self._rows.get(normalize_location(option.service_center))
                if row is None:
                    missing.append(option.service_center)
                    continue
                address, internship_time = row
                updated.append(
                    replace(
                        option,
                        address=address,
                        internship_time=internship_time,
                    )
                )
            refreshed[group] = tuple(updated)
        if missing:
            raise ValueError(
                "В таблице отсутствуют обязательные СЦ: "
                + ", ".join(sorted(missing))
            )
        return refreshed


class GoogleSheetWarehouseSource:
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
        self,
        cache_path: str | Path | None = None,
        validator: Callable[[WarehouseSheetCatalog], None] | None = None,
    ) -> WarehouseSheetCatalog:
        try:
            response = self.http_client.get(self.csv_url, timeout=self.timeout)
            response.raise_for_status()
            catalog = WarehouseSheetCatalog.from_csv(response.text)
            if validator is not None:
                validator(catalog)
        except Exception:
            if cache_path is None or not Path(cache_path).is_file():
                raise
            self.last_load_used_cache = True
            cached = WarehouseSheetCatalog.from_csv(
                Path(cache_path).read_text(encoding="utf-8")
            )
            if validator is not None:
                validator(cached)
            return cached

        self.last_load_used_cache = False
        if cache_path is not None:
            destination = Path(cache_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(response.text, encoding="utf-8")
            temporary.replace(destination)
        return catalog


class RefreshingWarehouseProvider:
    def __init__(
        self,
        source: GoogleSheetWarehouseSource,
        cache_path: str | Path,
        refresh_interval_seconds: float,
        base_groups: Mapping[str, tuple[WarehouseOption, ...]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.cache_path = Path(cache_path)
        self.refresh_interval_seconds = max(1.0, float(refresh_interval_seconds))
        self.base_groups = dict(base_groups)
        self.clock = clock
        catalog = source.load(self.cache_path, self._validate)
        self.groups = catalog.overlay(self.base_groups)
        self.next_refresh_at = self.clock() + self.refresh_interval_seconds
        self.last_error: Exception | None = None

    def refresh_if_due(self) -> bool:
        now = self.clock()
        if now < self.next_refresh_at:
            return False
        self.next_refresh_at = now + self.refresh_interval_seconds
        try:
            groups = self.source.load(
                self.cache_path, self._validate
            ).overlay(self.base_groups)
        except Exception as exc:
            self.last_error = exc
            return False
        self.groups = groups
        self.last_error = None
        return True

    def _validate(self, catalog: WarehouseSheetCatalog) -> None:
        catalog.overlay(self.base_groups)


def _find_header(headers: list[str], expected: str) -> int:
    try:
        return headers.index(expected)
    except ValueError as exc:
        raise ValueError(f"В таблице складов нет столбца «{expected}»") from exc
