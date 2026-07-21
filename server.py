import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from avito_bot.avito_client import AvitoClient
from avito_bot.conversation import ConversationState, handle_webhook_event


class BotHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"status": "not_found"})

    def do_POST(self):
        if self.path == "/webhook":
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length).decode("utf-8", errors="ignore")
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                payload = {}

            status_code, body = handle_request("POST", self.path, dict(self.headers), raw_body)
            self._send_json(status_code, body)
            return

        self._send_json(404, {"status": "not_found"})

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def create_state() -> ConversationState:
    return ConversationState()


def handle_request(method: str, path: str, headers: dict, body: str):
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    if method == "POST" and path == "/webhook":
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        client = AvitoClient(
            client_id=os.getenv("AVITO_CLIENT_ID", ""),
            client_secret=os.getenv("AVITO_CLIENT_SECRET", ""),
            user_id=os.getenv("AVITO_USER_ID", ""),
            base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
        )
        state = create_state()
        result = handle_webhook_event(client, state, payload)
        return 200, result

    return 404, {"status": "not_found"}


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = HTTPServer((host, port), BotHandler)
    print(f"Server started on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
