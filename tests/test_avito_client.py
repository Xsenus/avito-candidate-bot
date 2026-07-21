from avito_bot.avito_client import AvitoClient


class FakeResponse:
    status_code = 200

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
