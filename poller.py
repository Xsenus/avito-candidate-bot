from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from avito_bot.avito_client import AvitoClient
from avito_bot.conversation import ConversationState, handle_webhook_event

load_dotenv()


def iter_new_chat_messages(chats: list[dict[str, Any]], states: dict[str, ConversationState], processed_message_ids: dict[str, set[str]]):
    for chat in chats:
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            continue

        state = states.setdefault(chat_id, ConversationState())
        last_message = chat.get("last_message") or {}
        message_id = str(last_message.get("id") or "")
        if not message_id:
            continue

        processed_ids = processed_message_ids.setdefault(chat_id, set())
        if message_id in processed_ids:
            continue

        yield chat_id, state, last_message, message_id


def main() -> None:
    if os.getenv("USE_WEBHOOK", "false").strip().lower() in {"1", "true", "yes", "on"}:
        print("Webhook mode enabled; poller will run in polling mode anyway.")

    missing = [name for name in ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET", "AVITO_USER_ID") if not os.getenv(name, "").strip()]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}")
        print("Please set them in .env before starting the bot")
        return

    client = AvitoClient(
        client_id=os.getenv("AVITO_CLIENT_ID", ""),
        client_secret=os.getenv("AVITO_CLIENT_SECRET", ""),
        user_id=os.getenv("AVITO_USER_ID", ""),
        base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
    )
    states: dict[str, ConversationState] = {}
    processed_message_ids: dict[str, set[str]] = {}
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    print(f"Starting poller with interval={interval}s")

    try:
        token = client.get_access_token()
        print("Avito auth OK")
    except Exception as exc:
        print(f"Avito auth failed: {exc}")
        return

    while True:
        try:
            chats = client.get_chats(unread_only=True)
            for chat_id, state, last_message, message_id in iter_new_chat_messages(chats, states, processed_message_ids):
                text = last_message.get("content", {}).get("text") if isinstance(last_message.get("content"), dict) else None
                if not text:
                    continue

                result = handle_webhook_event(client, state, {"chat_id": chat_id, "message": {"text": text}})
                processed_message_ids.setdefault(chat_id, set()).add(message_id)
                print(result)

            time.sleep(interval)
        except Exception as exc:
            print(f"poller error: {exc}")
            time.sleep(interval)


if __name__ == "__main__":
    main()
