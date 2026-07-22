import pytest

from avito_bot.alerts import (
    TelegramAlertNotifier,
    TelegramDeliveryError,
    emit_alert,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = {"ok": True} if payload is None else payload

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_telegram_alert_sends_directly_without_proxy():
    http = FakeHttp()
    notifier = TelegramAlertNotifier("secret-token", "123", http=http)

    notifier.send("Form failed")

    url, kwargs = http.calls[0]
    assert url.endswith("/botsecret-token/sendMessage")
    assert kwargs["json"]["chat_id"] == "123"
    assert kwargs["json"]["text"] == "Form failed"
    assert "proxies" not in kwargs


@pytest.mark.parametrize("scheme", ["http", "https", "socks4", "socks5", "socks5h"])
def test_telegram_alert_uses_supported_proxy_for_http_and_https(scheme):
    http = FakeHttp()
    proxy = f"{scheme}://user:password@proxy.example:1080"
    notifier = TelegramAlertNotifier(
        "secret-token", "123", proxy_url=proxy, http=http
    )

    notifier.send("Form failed")

    _, kwargs = http.calls[0]
    assert kwargs["proxies"] == {"http": proxy, "https": proxy}


def test_disabled_telegram_alert_does_not_send():
    http = FakeHttp()
    TelegramAlertNotifier("", "", http=http).send("Form failed")
    assert http.calls == []


def test_telegram_network_error_does_not_expose_token():
    http = FakeHttp(error=RuntimeError("https://api.telegram.org/botsecret-token"))
    notifier = TelegramAlertNotifier("secret-token", "123", http=http)

    with pytest.raises(TelegramDeliveryError) as caught:
        notifier.send("Form failed")

    assert "secret-token" not in str(caught.value)


def test_telegram_api_rejection_is_reported_without_response_body():
    http = FakeHttp(response=FakeResponse(status_code=401, payload={"token": "leak"}))
    notifier = TelegramAlertNotifier("secret-token", "123", http=http)

    with pytest.raises(TelegramDeliveryError, match="HTTP 401"):
        notifier.send("Form failed")


def test_notifier_reads_empty_proxy_as_direct_connection(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_ALERT_PROXY_URL", "")

    notifier = TelegramAlertNotifier.from_env()

    assert notifier.enabled
    assert notifier.proxy_url is None


def test_notifier_rejects_unsupported_proxy_scheme(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_PROXY_URL", "ftp://proxy.example")

    with pytest.raises(ValueError, match="http, https"):
        TelegramAlertNotifier.from_env()


def test_emit_alert_survives_telegram_failure(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "123")
    monkeypatch.setattr(
        TelegramAlertNotifier,
        "send",
        lambda self, message: (_ for _ in ()).throw(
            TelegramDeliveryError("network failure")
        ),
    )

    emit_alert("application failed")

    output = capsys.readouterr().out
    assert "ALERT application failed" in output
    assert "telegram delivery failed" in output
    assert "secret-token" not in output
