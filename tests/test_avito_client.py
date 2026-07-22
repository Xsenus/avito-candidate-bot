from avito_bot.avito_client import AvitoClient


class FakeResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"messages": [{"id": "message-2"}, {"id": "message-1"}]}


def test_get_messages_uses_v3_chat_history_endpoint(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return FakeResponse()

    client = AvitoClient("client", "secret", "123")
    client._access_token = "token"
    monkeypatch.setattr("avito_bot.avito_client.requests.request", fake_request)

    messages = client.get_messages("chat-id", limit=500)

    assert [message["id"] for message in messages] == ["message-2", "message-1"]
    assert captured["method"] == "GET"
    assert captured["url"] == (
        "https://api.avito.ru/messenger/v3/accounts/123/chats/chat-id/messages/"
    )
    assert captured["kwargs"]["params"] == {"limit": 100, "offset": 0}


class TokenResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"access_token": "fresh-token", "expires_in": 3600}


def test_expired_token_is_refreshed_before_request(monkeypatch):
    token_calls = []
    request_headers = []
    clock = iter([100.0, 4000.0, 4000.0])

    monkeypatch.setattr("avito_bot.avito_client.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(
        "avito_bot.avito_client.requests.post",
        lambda *args, **kwargs: token_calls.append((args, kwargs)) or TokenResponse(),
    )

    def fake_request(method, url, **kwargs):
        request_headers.append(kwargs["headers"]["Authorization"])
        return FakeResponse()

    monkeypatch.setattr("avito_bot.avito_client.requests.request", fake_request)
    client = AvitoClient("client", "secret", "123")

    client.get_chats()
    client.get_chats()

    assert len(token_calls) == 2
    assert request_headers == ["Bearer fresh-token", "Bearer fresh-token"]


def test_transient_api_error_is_retried(monkeypatch):
    class UnavailableResponse(FakeResponse):
        status_code = 503

    responses = iter([UnavailableResponse(), FakeResponse()])
    sleeps = []
    client = AvitoClient("client", "secret", "123")
    client._access_token = "token"
    monkeypatch.setenv("AVITO_HTTP_RETRY_BASE_SECONDS", "0.25")
    monkeypatch.setattr(
        "avito_bot.avito_client.requests.request", lambda *args, **kwargs: next(responses)
    )
    monkeypatch.setattr("avito_bot.avito_client.time.sleep", sleeps.append)

    client.get_chats()

    assert sleeps == [0.25]
