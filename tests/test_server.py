from server import handle_request


def test_health_endpoint():
    status_code, payload = handle_request("GET", "/health", {}, "")
    assert status_code == 200
    assert payload["status"] == "ok"


def test_webhook_endpoint_returns_ignored_for_empty_payload():
    status_code, payload = handle_request("POST", "/webhook", {"Content-Type": "application/json"}, "{}")
    assert status_code == 200
    assert payload["status"] == "ignored"
