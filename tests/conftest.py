"""Keep unit tests independent of operator credentials and live services.

Install guards before test-module collection: importing poller otherwise loads
the operator's .env. These guards prevent accidental I/O, not hostile code.
"""

import os
import socket
import subprocess

import dotenv
import pytest


APP_ENV_PREFIXES = (
    "AVITO_", "YANDEX_FORM_", "TELEGRAM_ALERT_", "APPLICATION_",
    "INVITATION_", "INVITATIONS_", "REGIONAL_LOCATIONS_",
    "WAREHOUSE_LOCATIONS_", "FOLLOW_UP_", "POLL_",
)
APP_ENV_NAMES = {
    "STATE_DB_PATH", "PAUSE_ON_MANUAL_OUTGOING", "HEALTH_LOG_INTERVAL_SECONDS",
    "BOOTSTRAP_SKIP_EXISTING_MESSAGES", "SERVICE_CENTER_OVERRIDES_JSON",
    "FORM_WAREHOUSE_OVERRIDES_JSON",
}


def _deny_external_io(*args, **kwargs):
    """Never include arguments, which may contain credentials, in failures."""
    raise RuntimeError("Unit tests forbid network access and child processes; use a fake.")


def pytest_configure(config):
    """Guard collection as well as test execution; restore state at shutdown."""
    guard = pytest.MonkeyPatch()
    config.add_cleanup(guard.undo)
    guard.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    guard.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in list(os.environ):
        if name in APP_ENV_NAMES or name.startswith(APP_ENV_PREFIXES):
            guard.delenv(name, raising=False)
    guard.setattr(socket, "getaddrinfo", _deny_external_io)
    guard.setattr(socket.socket, "connect", _deny_external_io)
    guard.setattr(socket.socket, "connect_ex", _deny_external_io)
    guard.setattr(socket.socket, "sendto", _deny_external_io)
    guard.setattr(subprocess, "Popen", _deny_external_io)
    guard.setattr(os, "system", _deny_external_io)


@pytest.fixture(autouse=True)
def isolated_runtime_files(tmp_path, monkeypatch):
    """Redirect relative output and default bot persistence to per-test storage."""
    monkeypatch.chdir(tmp_path)
    for name, filename in {
        "STATE_DB_PATH": "bot.sqlite3",
        "INVITATIONS_CACHE_PATH": "invitations.csv",
        "REGIONAL_LOCATIONS_CACHE_PATH": "regional_locations.csv",
        "WAREHOUSE_LOCATIONS_CACHE_PATH": "warehouse_locations.csv",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / filename))
