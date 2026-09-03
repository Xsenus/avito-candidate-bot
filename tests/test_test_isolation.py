"""Regression checks for the offline unit-test boundary."""

import os
from pathlib import Path
import socket
import subprocess
import sys

import dotenv
import pytest

import poller


def test_operator_settings_are_not_inherited():
    for name in (
        "AVITO_CLIENT_SECRET", "TELEGRAM_ALERT_BOT_TOKEN",
        "YANDEX_FORM_URL", "FOLLOW_UP_REMINDERS_ENABLED",
    ):
        assert name not in os.environ


def test_dotenv_is_disabled_before_poller_import(tmp_path, monkeypatch):
    monkeypatch.delenv("AVITO_CLIENT_SECRET", raising=False)
    fixture = tmp_path / ".env"
    fixture.write_text("AVITO_CLIENT_SECRET=fixture-only\n", encoding="utf-8")
    assert dotenv.load_dotenv(fixture) is False
    assert poller.load_dotenv(fixture) is False
    assert "AVITO_CLIENT_SECRET" not in os.environ


def test_default_persistence_is_temporary(tmp_path):
    assert Path.cwd() == tmp_path
    for name in (
        "STATE_DB_PATH", "INVITATIONS_CACHE_PATH",
        "REGIONAL_LOCATIONS_CACHE_PATH", "WAREHOUSE_LOCATIONS_CACHE_PATH",
    ):
        assert Path(os.environ[name]).parent == tmp_path


def test_dns_is_blocked():
    with pytest.raises(RuntimeError, match="Unit tests forbid"):
        socket.getaddrinfo("example.invalid", 443)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_tcp_is_blocked_even_with_literal_ip(method):
    with socket.socket() as connection:
        with pytest.raises(RuntimeError, match="Unit tests forbid"):
            getattr(connection, method)(("127.0.0.1", 9))


def test_udp_is_blocked():
    with socket.socket(type=socket.SOCK_DGRAM) as connection:
        with pytest.raises(RuntimeError, match="Unit tests forbid"):
            connection.sendto(b"fixture", ("127.0.0.1", 9))


def test_child_process_is_blocked():
    with pytest.raises(RuntimeError, match="Unit tests forbid"):
        subprocess.run([sys.executable, "-c", "raise SystemExit(0)"], check=True)


def test_shell_is_blocked():
    with pytest.raises(RuntimeError, match="Unit tests forbid"):
        os.system("echo fixture")
