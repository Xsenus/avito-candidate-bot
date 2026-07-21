import pytest

from avito_bot.invitations import InvitationCatalog
from avito_bot.service_centers import (
    form_option_for,
    parse_service_center_overrides,
    resolve_service_center,
)


CATALOG = InvitationCatalog.from_csv(
    '"СЦ","Текст сообщения"\n'
    '"Кемерово","Приходите ДАТА"\n'
    '"Ростов","Приходите ДАТА"\n'
    '"Дмитровское","Приходите ДАТА"\n'
    '"Троицкий","Приходите ДАТА"\n'
)


def test_city_resolves_to_center_with_same_name():
    selection = resolve_service_center("Кемерово", "123", CATALOG)
    assert selection.name == "Кемерово"
    assert selection.form_option == "СЦ Кемерово"


def test_city_alias_resolves_to_sheet_name():
    selection = resolve_service_center("Ростов-на-Дону", None, CATALOG)
    assert selection.name == "Ростов"
    assert selection.form_option == "СЦ Ростов-на-Дону"


def test_form_warehouse_name_can_be_configured_separately():
    assert form_option_for("Шушары", {"Шушары": "СЦ СПБ Южный"}) == "СЦ СПБ Южный"


def test_item_override_has_priority():
    selection = resolve_service_center(
        "Москва", "8255883375", CATALOG, {"8255883375": "Дмитровское"}
    )
    assert selection.name == "Дмитровское"


@pytest.mark.parametrize(
    ("city", "expected"),
    [("Москва", "Дмитровское"), ("Санкт-Петербург", "Троицкий")],
)
def test_verified_big_city_defaults(city, expected):
    assert resolve_service_center(city, None, CATALOG).name == expected


def test_unknown_city_fails_instead_of_selecting_random_center():
    with pytest.raises(LookupError, match="SERVICE_CENTER_OVERRIDES_JSON"):
        resolve_service_center("Неизвестный город", "123", CATALOG)


def test_override_json_is_validated():
    assert parse_service_center_overrides('{"123": "Дмитровское"}') == {
        "123": "Дмитровское"
    }
    with pytest.raises(ValueError):
        parse_service_center_overrides("[]")
