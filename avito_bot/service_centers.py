from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .invitations import InvitationCatalog


CITY_ALIASES = {
    "москва": "Дмитровское",
    "мытищи": "Север",
    "санкт-петербург": "Троицкий",
    "санкт петербург": "Троицкий",
    "спб": "Троицкий",
    "питер": "Троицкий",
    "ростов-на-дону": "Ростов",
    "ростов на дону": "Ростов",
    "кущевская": "Ростов",
    "кущёвская": "Ростов",
    "набережные челны": "Набережные Челны",
    "нижний новгород": "Нижний Новгород",
}

FORM_WAREHOUSE_ALIASES = {
    "ростов": "Ростов-на-Дону",
}


@dataclass(frozen=True)
class ServiceCenterSelection:
    name: str

    @property
    def form_option(self) -> str:
        return form_option_for(self.name)


def form_option_for(
    service_center: str, overrides: dict[str, str] | None = None
) -> str:
    configured = overrides or {}
    form_name = configured.get(service_center) or configured.get(service_center.casefold())
    if not form_name:
        form_name = FORM_WAREHOUSE_ALIASES.get(
            service_center.casefold(), service_center
        )
    return form_name if form_name.casefold().startswith("сц ") else f"СЦ {form_name}"


def parse_service_center_overrides(raw: str | None) -> dict[str, str]:
    if not (raw or "").strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SERVICE_CENTER_OVERRIDES_JSON содержит некорректный JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("SERVICE_CENTER_OVERRIDES_JSON должен быть JSON-объектом")
    return {
        str(key).strip(): str(center).strip()
        for key, center in value.items()
        if str(key).strip() and str(center).strip()
    }


def resolve_service_center(
    city: str | None,
    item_id: str | None,
    catalog: InvitationCatalog,
    overrides: dict[str, str] | None = None,
) -> ServiceCenterSelection:
    configured = overrides or {}
    override = configured.get(str(item_id or "").strip())
    if override:
        return ServiceCenterSelection(catalog.find(override).service_center.removeprefix("СЦ ").strip())

    normalized_city = normalize_city(city)
    city_override = configured.get(normalized_city.casefold()) if normalized_city else None
    if city_override:
        return ServiceCenterSelection(catalog.find(city_override).service_center.removeprefix("СЦ ").strip())

    if not normalized_city:
        raise LookupError("В объявлении Avito не указан город")

    candidate = CITY_ALIASES.get(normalized_city.casefold(), normalized_city)
    try:
        template = catalog.find(candidate)
    except LookupError as exc:
        raise LookupError(
            f"Для города «{normalized_city}» склад не определяется однозначно; "
            "добавьте ID объявления в SERVICE_CENTER_OVERRIDES_JSON"
        ) from exc
    return ServiceCenterSelection(template.service_center.removeprefix("СЦ ").strip())


def normalize_city(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    cleaned = re.sub(r"^г\.\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned
