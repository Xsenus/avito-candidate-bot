from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from avito_bot.avito_client import AvitoClient
from avito_bot.conversation import ConversationState, handle_webhook_event

load_dotenv()

app = FastAPI(title="Avito bot")
client = AvitoClient(
    client_id=os.getenv("AVITO_CLIENT_ID", ""),
    client_secret=os.getenv("AVITO_CLIENT_SECRET", ""),
    user_id=os.getenv("AVITO_USER_ID", ""),
    base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
)
state_store: dict[str, ConversationState] = {}


def should_use_webhook() -> bool:
    value = os.getenv("USE_WEBHOOK", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _is_public_webhook_url(url: str) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.hostname or ""
    if not host:
        return False

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() not in {"localhost", "localhost.localdomain"}

    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


@app.on_event("startup")
async def register_webhook_on_startup() -> None:
    if not should_use_webhook():
        print("[startup] webhook registration disabled; running in poll-only mode")
        return

    webhook_url = os.getenv("AVITO_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[startup] AVITO_WEBHOOK_URL is not configured; webhook registration skipped")
        return

    if not _is_public_webhook_url(webhook_url):
        print("[startup] AVITO_WEBHOOK_URL points to localhost or a private address; Avito cannot reach it")
        return

    try:
        result = client.register_webhook(webhook_url)
        print(f"[startup] webhook registered: {result}")
    except Exception as exc:
        print(f"[startup] failed to register webhook: {exc}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/register_webhook")
def register_webhook() -> dict[str, Any]:
    if not should_use_webhook():
        raise RuntimeError("Webhook registration is disabled in poll-only mode")

    webhook_url = os.getenv("AVITO_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("AVITO_WEBHOOK_URL is not configured")
    if not _is_public_webhook_url(webhook_url):
        raise RuntimeError("AVITO_WEBHOOK_URL must be a public http/https URL reachable by Avito")
    return client.register_webhook(webhook_url)


@app.post("/webhook/avito")
async def webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    chat_id = payload.get("chat_id") or payload.get("chat", {}).get("id") or "unknown"
    state = state_store.setdefault(chat_id, ConversationState())

    result = handle_webhook_event(client, state, payload)
    return {"ok": True, "result": result}
