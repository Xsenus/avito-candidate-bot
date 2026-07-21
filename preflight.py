from __future__ import annotations

import os
from datetime import date, timedelta

from dotenv import load_dotenv

from avito_bot.yandex_form import CandidateApplication, YandexFormSubmitter


def main() -> None:
    load_dotenv()
    application = CandidateApplication(
        warehouse=os.getenv("YANDEX_FORM_TEST_WAREHOUSE", "СЦ Кемерово"),
        tariff="Драйв",
        last_name="Тестов",
        first_name="Тест",
        citizenship=os.getenv(
            "YANDEX_FORM_CITIZENSHIP", "Российская Федерация"
        ),
        phone="+79990000000",
        internship_date=(date.today() + timedelta(days=7)).strftime("%d.%m.%Y"),
    )
    YandexFormSubmitter.from_env().validate_schema(application)
    print("Yandex Form schema: OK (fields filled locally, response was not submitted)")


if __name__ == "__main__":
    main()
