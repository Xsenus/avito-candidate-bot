from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from avito_bot.avito_client import AvitoClient
from avito_bot.conversation import (
    FOLLOW_UP_MESSAGE,
    INITIAL_MESSAGE,
    ConversationState,
    handle_user_message,
    schedule_delayed_message,
)
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow, mark_invitation_sent
from avito_bot.yandex_form import YandexFormSubmitter

load_dotenv()


def iter_new_chat_messages(
    chats: list[dict[str, Any]], store: SQLiteStateStore
) -> Iterator[tuple[str, ConversationState, dict[str, Any], str, str | None, str | None]]:
    for chat in chats:
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            continue

        last_message = chat.get("last_message") or {}
        message_id = str(last_message.get("id") or "").strip()
        if not message_id or store.is_processed(chat_id, message_id):
            continue
        if last_message.get("direction") != "in" or last_message.get("type") != "text":
            store.mark_processed(chat_id, message_id)
            continue

        content = last_message.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            store.mark_processed(chat_id, message_id)
            continue

        city, item_id = extract_chat_context(chat)
        yield chat_id, store.load(chat_id), last_message, message_id, city, item_id


def extract_chat_context(chat: dict[str, Any]) -> tuple[str | None, str | None]:
    context = chat.get("context") or {}
    value = context.get("value") or {}
    if not isinstance(value, dict):
        return None, None
    location = value.get("location") or {}
    city = location.get("title") if isinstance(location, dict) else None
    item_id = value.get("id")
    return (
        city.strip() if isinstance(city, str) and city.strip() else None,
        str(item_id).strip() if item_id is not None and str(item_id).strip() else None,
    )


def complete_pending_application(
    client: AvitoClient,
    workflow: CandidateWorkflow,
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
) -> bool:
    if not state.processing_notice_sent:
        client.send_message(
            chat_id,
            "Спасибо, данные получили. Завершаю запись — это может занять до минуты.",
        )
        state.processing_notice_sent = True
        store.save(chat_id, state)

    try:
        invitation = workflow.complete(
            state, persist=lambda current: store.save(chat_id, current)
        )
    except Exception as exc:
        schedule_retry(store, chat_id, state, exc)
        print(f"application error chat_id={chat_id}: {exc}")
        return False

    try:
        client.send_message(chat_id, invitation)
    except Exception as exc:
        schedule_retry(store, chat_id, state, exc)
        print(f"invitation error chat_id={chat_id}: {exc}")
        return False
    mark_invitation_sent(state)
    store.save(chat_id, state)
    print(
        f"application completed chat_id={chat_id} "
        f"city={state.city!r} service_center={state.service_center!r}"
    )
    return True


def schedule_retry(
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    error: Exception,
) -> None:
    state.last_error = str(error)
    state.submission_attempts += 1
    retry_seconds = max(30, int(os.getenv("APPLICATION_RETRY_SECONDS", "300")))
    state.next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
    ).isoformat()
    store.save(chat_id, state)


def initialize_message_cursor(
    client: AvitoClient, store: SQLiteStateStore, chats: list[dict[str, Any]]
) -> None:
    if store.get_metadata("message_cursor_initialized") == "true":
        return
    skip_existing = os.getenv("BOOTSTRAP_SKIP_EXISTING_MESSAGES", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if skip_existing:
        count = 0
        for chat in chats:
            chat_id = str(chat.get("id") or "").strip()
            message_id = str((chat.get("last_message") or {}).get("id") or "").strip()
            if chat_id and message_id:
                store.mark_processed(chat_id, message_id)
                count += 1
        print(f"Bootstrap: skipped {count} existing last messages")
    store.set_metadata("message_cursor_initialized", "true")


def process_chat_message(
    client: AvitoClient,
    workflow: CandidateWorkflow,
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    message: dict[str, Any],
    message_id: str,
    city: str | None,
    item_id: str | None,
) -> None:
    content = message.get("content") or {}
    text = content.get("text", "")
    state.city = city or state.city
    state.item_id = item_id or state.item_id

    reply = handle_user_message(state, text, city_hint=state.city)
    if reply:
        client.send_message(chat_id, reply)
        if reply == INITIAL_MESSAGE:
            delay = int(os.getenv("FOLLOW_UP_DELAY_SECONDS", "5"))
            schedule_delayed_message(client, chat_id, FOLLOW_UP_MESSAGE, delay=delay)

    store.save(chat_id, state)
    store.mark_processed(chat_id, message_id)

    if state.application_status in {"pending", "submitted"}:
        complete_pending_application(client, workflow, store, chat_id, state)


def main() -> None:
    missing = [
        name
        for name in ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET", "AVITO_USER_ID")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}")
        return

    client = AvitoClient(
        client_id=os.getenv("AVITO_CLIENT_ID", ""),
        client_secret=os.getenv("AVITO_CLIENT_SECRET", ""),
        user_id=os.getenv("AVITO_USER_ID", ""),
        base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
    )
    state_path = os.getenv("STATE_DB_PATH", str(Path(PROJECT_ROOT) / "data" / "bot.sqlite3"))
    store = SQLiteStateStore(state_path)
    workflow = CandidateWorkflow.from_env(YandexFormSubmitter.from_env())
    interval = max(5, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
    print(f"Starting poller with interval={interval}s state_db={state_path}")

    try:
        client.get_access_token()
        print("Avito auth OK")
    except Exception as exc:
        print(f"Avito auth failed: {exc}")
        return


    try:
        initial_chats = client.get_chats(unread_only=False)
        initialize_message_cursor(client, store, initial_chats)
    except Exception as exc:
        print(f"Failed to initialize message cursor: {exc}")
        return

    while True:
        try:
            chats = client.get_chats(unread_only=False)
            for values in iter_new_chat_messages(chats, store):
                try:
                    process_chat_message(client, workflow, store, *values)
                except Exception as exc:
                    print(f"message error chat_id={values[0]}: {exc}")

            for chat_id, state in store.pending():
                complete_pending_application(client, workflow, store, chat_id, state)
        except Exception as exc:
            print(f"poller error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
