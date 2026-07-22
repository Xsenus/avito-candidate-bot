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
from avito_bot.alerts import emit_alert
from avito_bot.conversation import (
    ADDRESS_MESSAGE,
    CONFIRMATION_MESSAGE,
    FOLLOW_UP_MESSAGE,
    INITIAL_MESSAGE,
    INTERNSHIP_MESSAGE,
    STORE_SELECTION_MESSAGE,
    ConversationState,
    handle_user_message,
    schedule_delayed_message,
)
from avito_bot.candidate import normalize_phone, resolve_internship_date, split_full_name
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow, mark_invitation_sent
from avito_bot.yandex_form import FormConfigurationError, YandexFormSubmitter

load_dotenv()

CHAT_PAGE_LIMIT = 100


def iter_new_chat_messages(
    client: AvitoClient,
    chats: list[dict[str, Any]],
    store: SQLiteStateStore,
) -> Iterator[tuple[str, ConversationState, dict[str, Any], str, str | None, str | None]]:
    for chat in chats:
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            continue

        last_message = chat.get("last_message") or {}
        last_message_id = str(last_message.get("id") or "").strip()
        if not last_message_id or store.is_processed(chat_id, last_message_id):
            continue

        city, item_id = extract_chat_context(chat)
        state = store.load(chat_id)
        messages = oldest_first(client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT))
        cursor = store.get_message_cursor(chat_id)
        only_last_message = False

        # Migrate databases created before per-chat cursors existed. A fetched
        # processed message is a reliable watermark; unprocessed messages older
        # than it are history. With no matching processed message, fail closed
        # and only consider Avito's current last message.
        if cursor is None:
            processed_keys = [
                key
                for message in messages
                if (key := message_key(message)) is not None
                and store.is_processed(chat_id, str(message.get("id") or "").strip())
            ]
            if processed_keys:
                cursor = max(processed_keys)
            else:
                only_last_message = True

        terminal = state.application_status == "completed" or state.step == "done"
        for message in messages:
            message_id = str(message.get("id") or "").strip()
            if not message_id or store.is_processed(chat_id, message_id):
                continue
            key = message_key(message)
            if only_last_message and message_id != last_message_id:
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if key is None and message_id != last_message_id:
                store.mark_message_seen(chat_id, message_id, None)
                continue
            if key is not None and cursor is not None and key <= cursor:
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if terminal:
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if message.get("direction") != "in" or message.get("type") != "text":
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue

            content = message.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if not isinstance(text, str) or not text.strip():
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            yield chat_id, state, message, message_id, city, item_id


def normalized_created(message: dict[str, Any]) -> float | None:
    created = message.get("created")
    if not isinstance(created, (int, float)):
        return None
    value = float(created)
    if value > 10_000_000_000:
        value /= 1000
    return value


def message_key(message: dict[str, Any]) -> tuple[float, str] | None:
    created = normalized_created(message)
    message_id = str(message.get("id") or "").strip()
    return (created, message_id) if created is not None and message_id else None


def oldest_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avito returns newest first; use timestamps when available for stable ordering."""
    if messages and all(isinstance(message.get("created"), (int, float)) for message in messages):
        return sorted(messages, key=lambda message: (message["created"], str(message.get("id", ""))))
    return list(reversed(messages))


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
    missing = missing_application_fields(state)
    if missing and state.application_status != "submitted":
        return_to_collection(store, chat_id, state, missing)
        print(
            f"application deferred chat_id={chat_id}: missing {', '.join(missing)}"
        )
        return False

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
    except (FormConfigurationError, ValueError, LookupError) as exc:
        pause_application(store, chat_id, state, exc)
        return False
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
    max_retries = max(1, int(os.getenv("APPLICATION_MAX_RETRIES", "5")))
    alert_after = max(1, int(os.getenv("APPLICATION_ALERT_AFTER_ATTEMPTS", "3")))
    if state.submission_attempts >= max_retries:
        state.application_status = (
            "invitation_retry_exhausted"
            if state.application_status == "submitted"
            else "submission_retry_exhausted"
        )
        state.next_retry_at = None
        state.alert_sent = True
        store.save(chat_id, state)
        emit_alert(
            "application retries exhausted "
            f"chat_id={chat_id} attempts={state.submission_attempts} error={error}"
        )
        return

    retry_seconds = max(30, int(os.getenv("APPLICATION_RETRY_SECONDS", "300")))
    state.next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
    ).isoformat()
    if state.submission_attempts >= alert_after and not state.alert_sent:
        state.alert_sent = True
        emit_alert(
            "repeated application failure "
            f"chat_id={chat_id} attempts={state.submission_attempts} error={error}"
        )
    store.save(chat_id, state)


def pause_application(
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    error: Exception,
) -> None:
    """Stop automatic retries for deterministic configuration or data errors."""
    state.application_status = (
        "invitation_configuration_error"
        if state.application_status == "submitted"
        else "configuration_error"
    )
    state.last_error = str(error)
    state.submission_attempts += 1
    state.next_retry_at = None
    state.alert_sent = True
    store.save(chat_id, state)
    emit_alert(
        "application paused due to configuration error "
        f"chat_id={chat_id} attempts={state.submission_attempts} error={error}"
    )


def initialize_message_cursor(
    client: AvitoClient, store: SQLiteStateStore, chats: list[dict[str, Any]]
) -> None:
    cursor_key = "message_history_cursor_initialized_v2"
    if store.get_metadata(cursor_key) == "true":
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
            if not chat_id:
                continue
            messages = oldest_first(client.get_messages(chat_id, limit=100))
            state = store.load(chat_id)
            prompt_index = None
            inferred_step = None
            if state.application_status == "collecting":
                for index, message in enumerate(messages):
                    if message.get("direction") != "out" or message.get("type") != "text":
                        continue
                    content = message.get("content") or {}
                    text = content.get("text") if isinstance(content, dict) else None
                    step = infer_step_from_bot_message(text)
                    if step:
                        prompt_index = index
                        inferred_step = step

            if inferred_step:
                city, item_id = extract_chat_context(chat)
                restore_collected_fields(
                    state,
                    messages[: (prompt_index or 0) + 1],
                )
                state.step = inferred_step
                state.city = city or state.city
                state.item_id = item_id or state.item_id
                store.save(chat_id, state)

            for message in messages:
                message_id = str(message.get("id") or "").strip()
                if not message_id:
                    continue
                store.mark_message_seen(
                    chat_id, message_id, normalized_created(message)
                )
                count += 1
        print(f"Bootstrap: skipped {count} existing messages")
    store.set_metadata(cursor_key, "true")


def infer_step_from_bot_message(text: str | None) -> str | None:
    normalized = (text or "").strip()
    if normalized in {INITIAL_MESSAGE.strip(), FOLLOW_UP_MESSAGE.strip()}:
        return "awaiting_interest"
    if normalized == INTERNSHIP_MESSAGE.strip():
        return "awaiting_staj"
    if normalized in {ADDRESS_MESSAGE.strip(), STORE_SELECTION_MESSAGE.strip()}:
        return "awaiting_datetime"
    if normalized == CONFIRMATION_MESSAGE.strip():
        return "awaiting_full_name"
    if normalized == "И номер":
        return "awaiting_phone"
    return None


def missing_application_fields(state: ConversationState) -> list[str]:
    return [
        name
        for name, value in (
            ("internship_date", state.internship_date),
            ("last_name", state.last_name),
            ("first_name", state.first_name),
            ("phone", state.phone),
        )
        if not value
    ]


def return_to_collection(
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    missing: list[str] | None = None,
) -> None:
    absent = missing or missing_application_fields(state)
    state.application_status = "collecting"
    if "internship_date" in absent:
        state.step = "awaiting_datetime"
    elif "last_name" in absent or "first_name" in absent:
        state.step = "awaiting_full_name"
    elif "phone" in absent:
        state.step = "awaiting_phone"
    state.last_error = None
    state.next_retry_at = None
    state.submission_attempts = 0
    state.alert_sent = False
    store.save(chat_id, state)


def reconcile_incomplete_applications(
    client: AvitoClient, store: SQLiteStateStore
) -> tuple[int, int]:
    repaired = 0
    returned_to_collection = 0
    for chat_id, state in store.all_conversations():
        if state.application_status not in {"pending", "submitted"}:
            continue
        missing = missing_application_fields(state)
        if not missing:
            continue
        restore_collected_fields(state, client.get_messages(chat_id, limit=100))
        missing = missing_application_fields(state)
        if not missing:
            state.last_error = None
            state.next_retry_at = None
            state.submission_attempts = 0
            store.save(chat_id, state)
            repaired += 1
            continue
        return_to_collection(store, chat_id, state, missing)
        returned_to_collection += 1
    return repaired, returned_to_collection


def restore_collected_fields(
    state: ConversationState, messages: list[dict[str, Any]]
) -> None:
    """Recover date, name and phone that the legacy in-memory bot already collected."""
    expected_step = None
    for message in oldest_first(messages):
        content = message.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        if message.get("direction") == "out":
            expected_step = infer_step_from_bot_message(text)
            continue
        if message.get("direction") != "in" or message.get("type") != "text":
            continue

        if expected_step == "awaiting_datetime":
            try:
                created = message.get("created")
                timestamp = float(created) if isinstance(created, (int, float)) else None
                if timestamp and timestamp > 10_000_000_000:
                    timestamp /= 1000
                message_date = (
                    datetime.fromtimestamp(timestamp, timezone.utc).date()
                    if timestamp
                    else None
                )
                internship_date = resolve_internship_date(text, today=message_date)
                state.date_time = text.strip()
                state.internship_date = internship_date.strftime("%d.%m.%Y")
            except (ValueError, OSError, OverflowError):
                pass
        elif expected_step == "awaiting_full_name":
            try:
                state.last_name, state.first_name = split_full_name(text)
                state.full_name = text.strip()
            except ValueError:
                pass
            try:
                state.phone = normalize_phone(text)
            except ValueError:
                pass
        elif expected_step == "awaiting_phone":
            try:
                state.phone = normalize_phone(text)
            except ValueError:
                pass


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
    store.mark_message_seen(chat_id, message_id, normalized_created(message))

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
    interrupted = store.quarantine_interrupted_submissions()
    if interrupted:
        print(
            f"WARNING: quarantined {interrupted} interrupted form submission(s); "
            "manual verification is required"
        )
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
        initial_chats = client.get_chats(
            unread_only=False, limit=CHAT_PAGE_LIMIT
        )
        initialize_message_cursor(client, store, initial_chats)
        repaired, returned = reconcile_incomplete_applications(client, store)
        if repaired or returned:
            print(
                f"Reconciled legacy applications: repaired={repaired}, "
                f"returned_to_collection={returned}"
            )
    except Exception as exc:
        print(f"Failed to initialize message cursor: {exc}")
        return

    while True:
        try:
            chats = client.get_chats(
                unread_only=True, limit=CHAT_PAGE_LIMIT
            )
            failed_chats: set[str] = set()
            for values in iter_new_chat_messages(client, chats, store):
                if values[0] in failed_chats:
                    continue
                try:
                    process_chat_message(client, workflow, store, *values)
                except Exception as exc:
                    failed_chats.add(values[0])
                    print(f"message error chat_id={values[0]}: {exc}")

            for chat_id, state in store.pending():
                complete_pending_application(client, workflow, store, chat_id, state)
        except Exception as exc:
            print(f"poller error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
