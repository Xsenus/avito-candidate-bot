from __future__ import annotations

import os
from datetime import date, timedelta

from dotenv import load_dotenv

from avito_bot.avito_client import AvitoClient
from avito_bot.invitations import GoogleSheetInvitationSource
from avito_bot.service_centers import (
    form_option_for,
    parse_service_center_overrides,
)
from avito_bot.warehouses import WAREHOUSE_GROUPS
from avito_bot.yandex_form import CandidateApplication, YandexFormSubmitter


def main() -> None:
    load_dotenv()
    form = YandexFormSubmitter.from_env()
    test_warehouse = os.getenv("YANDEX_FORM_TEST_WAREHOUSE", "СЦ Кемерово")
    application = CandidateApplication(
        warehouse=test_warehouse,
        tariff="Драйв",
        last_name="Тестов",
        first_name="Тест",
        citizenship=os.getenv(
            "YANDEX_FORM_CITIZENSHIP", "Российская Федерация"
        ),
        phone="+79990000000",
        internship_date=(date.today() + timedelta(days=7)).strftime("%d.%m.%Y"),
    )
    form.validate_schema(application)
    print("Yandex Form schema: OK (fields filled locally, response was not submitted)")
    form.validate_submission_request(application)
    print("Yandex Form submit request: OK (request was intercepted and not submitted)")

    active_warehouses = load_active_warehouses()
    form.validate_warehouse_options(active_warehouses)
    print(
        f"Yandex Form warehouses: OK ({len(active_warehouses)} active options, "
        "response was not submitted)"
    )


def load_active_warehouses() -> list[str]:
    client = AvitoClient(
        client_id=os.getenv("AVITO_CLIENT_ID", ""),
        client_secret=os.getenv("AVITO_CLIENT_SECRET", ""),
        user_id=os.getenv("AVITO_USER_ID", ""),
        base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
    )
    client.get_chats(unread_only=False, limit=1)

    catalog = GoogleSheetInvitationSource(
        os.getenv("INVITATIONS_SHEET_ID", "1D6aP4Vjt05QMRIogvdtX0wKblbgnNrg-I8lF0Fq26zs"),
        os.getenv("INVITATIONS_SHEET_GID", "420777109"),
    ).load()
    form_overrides = parse_service_center_overrides(
        os.getenv("FORM_WAREHOUSE_OVERRIDES_JSON", "")
    )

    warehouses = set()
    for options in WAREHOUSE_GROUPS.values():
        for option in options:
            catalog.find(option.service_center)
            warehouses.add(
                form_option_for(option.service_center, form_overrides)
            )
    return sorted(warehouses)


if __name__ == "__main__":
    main()
