from datetime import date

import pytest

from avito_bot.invitations import (
    GoogleSheetInvitationSource,
    InvitationCatalog,
)


SAMPLE_CSV = '''"",""
"СЦ","Текст сообщения"
"Новосибирск","Вы записаны на стажировку ДАТА по адресу склада"
"СЦ Бутово","Приходите ДАТА в 8:00"
'''


class FakeResponse:
    text = SAMPLE_CSV

    def raise_for_status(self):
        return None


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        return FakeResponse()


class FailingHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        raise RuntimeError("429 Too Many Requests")


def test_catalog_parses_sheet_and_matches_sc_prefix():
    catalog = InvitationCatalog.from_csv(SAMPLE_CSV)

    assert len(catalog) == 2
    assert catalog.find("СЦ Новосибирск").service_center == "Новосибирск"
    assert catalog.find("Бутово").service_center == "СЦ Бутово"


def test_invitation_replaces_date_marker():
    template = InvitationCatalog.from_csv(SAMPLE_CSV).find("Новосибирск")

    invitation = template.render(date(2026, 7, 28))

    assert "ДАТА" not in invitation
    assert "28.07." in invitation
    assert "Что взять с собой:" in invitation
    assert "Заряженный смартфон" in invitation
    assert invitation.endswith("До встречи!")


def test_invitation_does_not_duplicate_existing_footer():
    catalog = InvitationCatalog.from_csv(
        '"СЦ","Текст сообщения"\n'
        '"Кемерово","Приходите ДАТА. Что взять с собой: паспорт"\n'
    )

    invitation = catalog.find("Кемерово").render("23.07.2026")

    assert invitation.count("Что взять с собой:") == 1
    assert "23.07.." not in invitation


def test_questions_line_can_be_enabled(monkeypatch):
    monkeypatch.setenv("INVITATION_INCLUDE_QUESTIONS_LINE", "true")
    template = InvitationCatalog.from_csv(SAMPLE_CSV).find("Новосибирск")

    invitation = template.render("28.07.2026")

    assert invitation.endswith(
        "Остались вопросы? Пишите здесь или уточните уже на стажировке."
    )


@pytest.mark.parametrize(
    ("service_center", "source_time", "current_time"),
    [
        ("Ростов", "10:30", "9:00:00"),
        ("Краснодар", "10:30", "9:00:00"),
        ("Нижний Новгород", "10:30", "8:00:00"),
        ("Калуга", "7:00:00", "8:00:00"),
    ],
)
def test_regional_invitation_uses_current_location_time(
    service_center,
    source_time,
    current_time,
):
    catalog = InvitationCatalog.from_csv(
        '"СЦ","Текст сообщения"\n'
        f'"{service_center}","Стажировка начинается в {source_time} '
        'по адресу склада ДАТА"\n'
    )

    invitation = catalog.find(service_center).render(
        "28.07.2026",
        internship_time=current_time,
    )

    assert f"Стажировка начинается в {current_time} по адресу" in invitation


def test_other_cities_keep_time_from_sheet():
    catalog = InvitationCatalog.from_csv(
        '"СЦ","Текст сообщения"\n'
        '"Кемерово","Стажировка начинается в 11:15:00 по адресу склада ДАТА"\n'
    )

    invitation = catalog.find("Кемерово").render("28.07.2026")

    assert "Стажировка начинается в 11:15:00 по адресу" in invitation


def test_explicit_location_time_requires_time_in_template():
    catalog = InvitationCatalog.from_csv(
        '"СЦ","Текст сообщения"\n'
        '"Ростов","Приходите на стажировку ДАТА"\n'
    )

    with pytest.raises(ValueError, match="не найдено время начала стажировки"):
        catalog.find("Ростов").render(
            "28.07.2026",
            internship_time="9:00:00",
        )


def test_explicit_location_time_requires_hh_mm_ss_format():
    catalog = InvitationCatalog.from_csv(
        '"СЦ","Текст сообщения"\n'
        '"Ростов","Стажировка начинается в 10:30 по адресу ДАТА"\n'
    )

    with pytest.raises(ValueError, match="Некорректное время"):
        catalog.find("Ростов").render(
            "28.07.2026",
            internship_time="9:00",
        )


def test_catalog_rejects_invitation_without_date_marker():
    with pytest.raises(ValueError, match="нет маркера ДАТА"):
        InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n"Кемерово","Приходите на стажировку"\n'
        )


def test_catalog_rejects_invitation_over_avito_limit():
    text = "ДАТА " + "я" * 1000
    with pytest.raises(ValueError, match="превышает лимит Avito"):
        InvitationCatalog.from_csv(
            f'"СЦ","Текст сообщения"\n"Кемерово","{text}"\n'
        )


def test_unknown_service_center_fails_explicitly():
    catalog = InvitationCatalog.from_csv(SAMPLE_CSV)

    with pytest.raises(LookupError, match="не найдено"):
        catalog.find("Неизвестный склад")


def test_google_source_uses_expected_csv_endpoint():
    http = FakeHttp()
    source = GoogleSheetInvitationSource("sheet_123", "420777109", http=http)

    catalog = source.load()

    assert len(catalog) == 2
    assert http.calls == [
        (
            "https://docs.google.com/spreadsheets/d/sheet_123/gviz/tq"
            "?tqx=out:csv&gid=420777109",
            30,
        )
    ]


def test_google_source_persists_and_reuses_cache_after_restart(tmp_path):
    cache_path = tmp_path / "invitations.csv"
    live_http = FakeHttp()
    live_source = GoogleSheetInvitationSource(
        "sheet_123",
        "420777109",
        http=live_http,
        cache_path=cache_path,
    )

    assert len(live_source.load()) == 2
    assert cache_path.read_text(encoding="utf-8") == SAMPLE_CSV

    failed_http = FailingHttp()
    cached_source = GoogleSheetInvitationSource(
        "sheet_123",
        "420777109",
        http=failed_http,
        cache_path=cache_path,
    )

    assert len(cached_source.load()) == 2
    assert cached_source.last_load_used_cache
    assert failed_http.calls == []


def test_google_source_keeps_last_catalog_when_refresh_is_rate_limited(tmp_path):
    now = [0.0]
    cache_path = tmp_path / "invitations.csv"
    live_http = FakeHttp()
    source = GoogleSheetInvitationSource(
        "sheet_123",
        "420777109",
        http=live_http,
        cache_path=cache_path,
        refresh_interval_seconds=60,
        clock=lambda: now[0],
    )

    initial = source.load()
    source.http = FailingHttp()
    now[0] = 61

    refreshed = source.load()

    assert refreshed is initial
    assert source.last_load_used_cache
    assert isinstance(source.last_error, RuntimeError)


def test_google_source_retries_only_after_refresh_interval(tmp_path):
    now = [0.0]
    http = FakeHttp()
    source = GoogleSheetInvitationSource(
        "sheet_123",
        "420777109",
        http=http,
        cache_path=tmp_path / "invitations.csv",
        refresh_interval_seconds=60,
        clock=lambda: now[0],
    )

    source.load()
    now[0] = 59
    source.load()
    now[0] = 60
    source.load()

    assert len(http.calls) == 2


def test_google_source_returns_live_catalog_when_cache_cannot_be_written(
    tmp_path, monkeypatch
):
    source = GoogleSheetInvitationSource(
        "sheet_123",
        "420777109",
        http=FakeHttp(),
        cache_path=tmp_path / "invitations.csv",
    )
    monkeypatch.setattr(
        source,
        "_write_cache",
        lambda content: (_ for _ in ()).throw(PermissionError("read only")),
    )

    assert len(source.load()) == 2
    assert isinstance(source.last_error, PermissionError)
