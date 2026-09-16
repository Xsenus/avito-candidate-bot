from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from avito_bot.alerts import emit_alert
from avito_bot.avito_client import AvitoClient
from avito_bot.candidate import (
    normalize_phone,
    resolve_internship_date,
    split_full_name,
)
from avito_bot.conversation import (
    CALL_HANDOFF_MESSAGE,
    CONFIRMATION_MESSAGE,
    DATE_REMINDER_MESSAGE,
    FOLLOW_UP_MESSAGE,
    INITIAL_MESSAGE,
    LEGACY_CALL_HANDOFF_MESSAGE,
    LEGACY_DATE_REMINDER_MESSAGE,
    LEGACY_MORE_INFO_MESSAGE,
    MORE_INFO_MESSAGE,
    ConversationState,
    handle_user_message,
    initial_messages_for_city,
    is_call_request,
    is_reactivation_acceptance,
    stop_reminders,
)
from avito_bot.regional_locations import (
    DEFAULT_SHEET_GID as DEFAULT_REGIONAL_LOCATIONS_GID,
)
from avito_bot.regional_locations import (
    DEFAULT_SHEET_ID as DEFAULT_REGIONAL_LOCATIONS_SHEET_ID,
)
from avito_bot.regional_locations import (
    GoogleSheetRegionalLocationSource,
    RefreshingRegionalLocationProvider,
    RegionalLocationCatalog,
    regional_initial_messages,
)
from avito_bot.reminders import (
    ReminderConfig,
    arm_reminders,
    clear_inflight_for_retry,
    finish_reminder,
    inflight_retry_is_due,
    mark_reminder_inflight,
    reminder_is_due,
    reminder_message,
)
from avito_bot.service_centers import parse_service_center_overrides
from avito_bot.storage import SQLiteStateStore
from avito_bot.warehouse_sheet import (
    DEFAULT_SHEET_GID as DEFAULT_WAREHOUSE_LOCATIONS_SHEET_GID,
)
from avito_bot.warehouse_sheet import (
    DEFAULT_SHEET_ID as DEFAULT_WAREHOUSE_LOCATIONS_SHEET_ID,
)
from avito_bot.warehouse_sheet import (
    GoogleSheetWarehouseSource,
    RefreshingWarehouseProvider,
)
from avito_bot.warehouses import (
    WAREHOUSE_GROUPS,
    replace_warehouse_groups,
    warehouse_group_for_city,
    warehouse_prompt_for_city,
)
from avito_bot.workflow import CandidateWorkflow, mark_invitation_sent
from avito_bot.yandex_form import FormConfigurationError, YandexFormSubmitter

load_dotenv()

CHAT_PAGE_LIMIT = 100
JOB_APPLICATION_FLOW_IDS = frozenset({"job"})


def env_file_signature(path: str | Path) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def account_fingerprint(user_id: str) -> str:
    normalized = str(user_id or "").strip()
    if not normalized:
        raise ValueError("AVITO_USER_ID is required")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def http_error_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    match = re.search(r"\b([1-5]\d{2})\s+Client Error\b", str(exc))
    return int(match.group(1)) if match else None


def is_job_application_system_message(message: dict[str, Any]) -> bool:
    """Return whether Avito is announcing a new vacancy application."""
    if message.get("direction") != "in" or message.get("type") != "system":
        return False
    content = message.get("content") or {}
    return (
        isinstance(content, dict)
        and str(content.get("flow_id") or "").strip() in JOB_APPLICATION_FLOW_IDS
    )


def get_chat_messages_if_available(
    client: AvitoClient,
    chat_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]] | None:
    """Return history while isolating Avito's per-chat HTTP 402 response.

    Some accounts expose a chat in the chat list but return Payment Required
    for that individual history. One such chat must not abort polling for the
    rest of the account. Other failures still reach the normal retry loop.
    """
    try:
        return client.get_messages(chat_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 - inspect HTTP status generically
        if http_error_status(exc) != 402:
            raise
        print(
            "WARNING: skipped chat history unavailable through Avito API "
            "status=402"
        )
        return None


def iter_new_chat_messages(
    client: AvitoClient,
    chats: list[dict[str, Any]],
    store: SQLiteStateStore,
    *,
    not_before_timestamp: float | None = None,
    manual_takeover_after: float | None = None,
    reminder_manual_stop_after: float | None = None,
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
        history = get_chat_messages_if_available(
            client, chat_id, limit=CHAT_PAGE_LIMIT
        )
        if history is None:
            store.mark_message_seen(
                chat_id,
                last_message_id,
                normalized_created(last_message),
            )
            continue
        messages = oldest_first(history)
        latest_job_application = next(
            (
                message
                for message in reversed(messages)
                if is_job_application_system_message(message)
                and str(message.get("id") or "").strip()
            ),
            None,
        )
        latest_job_application_id = (
            str(latest_job_application.get("id") or "").strip()
            if latest_job_application is not None
            else None
        )
        cursor = store.get_message_cursor(chat_id)
        only_last_message = False

        # The Avito chat list is limited and can expose older pages only after
        # other chats move in the ordering. Fail closed for a chat that was not
        # present during installation: it is eligible for automation only when
        # its latest vacancy response was created after the installation
        # boundary. This prevents historical conversations from being restarted
        # merely because they appear in a later polling cycle.
        if cursor is None and not_before_timestamp is not None:
            latest_application = next(
                (
                    message
                    for message in reversed(messages)
                    if is_job_application_system_message(message)
                ),
                None,
            )
            application_created = (
                normalized_created(latest_application)
                if latest_application is not None
                else None
            )
            if (
                application_created is None
                or application_created < not_before_timestamp
            ):
                state.step = "done"
                state.application_status = "manual"
                state.last_error = None
                state.next_retry_at = None
                state.notes["preinstallation_chat_quarantined"] = "true"
                store.save(chat_id, state)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id:
                        continue
                    key = message_key(message)
                    store.mark_message_seen(
                        chat_id,
                        message_id,
                        key[0] if key else None,
                    )
                continue

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

        terminal = state.application_status in {
            "completed",
            "manual",
            "cancelled",
        } or state.step in {
            "done",
            "manual_takeover",
        }

        # A manager and a candidate can both write between two polling cycles.
        # Avito IDs are opaque and same-second ordering is ambiguous, so looking
        # at messages one by one can yield the candidate reply before noticing
        # that a human already took over.  Pre-scan the whole eligible history:
        # a manual outgoing must always win and silence this chat first.
        if not terminal and manual_takeover_after is not None:
            for outgoing in messages:
                outgoing_id = str(outgoing.get("id") or "").strip()
                if (
                    not outgoing_id
                    or store.is_processed(chat_id, outgoing_id)
                    or outgoing.get("direction") == "in"
                    or outgoing.get("type") != "text"
                ):
                    continue
                outgoing_key = message_key(outgoing)
                created = normalized_created(outgoing)
                if (
                    created is None
                    or created < manual_takeover_after
                    or (
                        outgoing_key is not None
                        and cursor is not None
                        and outgoing_key[0] < cursor[0]
                    )
                ):
                    continue
                probe = state
                if (
                    state.reactivation_reply_inflight_at
                    and state.reactivation_reply_index < len(state.reactivation_reply_messages)
                ):
                    probe = ConversationState(
                        reminder_inflight_started_at=state.reactivation_reply_inflight_at,
                        reminder_inflight_text=state.reactivation_reply_messages[
                            state.reactivation_reply_index
                        ],
                    )
                if _find_delivered_inflight_reminder([outgoing], probe):
                    store.mark_bot_outgoing(chat_id, outgoing_id)
                    continue
                if store.is_bot_outgoing(chat_id, outgoing_id):
                    continue
                stop_reminders(state, permanently=True)
                state.step = "manual_takeover"
                state.application_status = "manual"
                state.last_error = None
                state.manual_takeover_at = datetime.now(timezone.utc).isoformat()
                state.manual_takeover_message_id = outgoing_id
                store.save(chat_id, state)
                store.mark_message_seen(
                    chat_id,
                    outgoing_id,
                    outgoing_key[0] if outgoing_key else None,
                )
                terminal = True
                print(
                    f"manual takeover chat_id={chat_id} "
                    f"message_id={outgoing_id}; bot paused"
                )
                break
        for message in messages:
            message_id = str(message.get("id") or "").strip()
            if not message_id or store.is_processed(chat_id, message_id):
                continue
            key = message_key(message)
            if (
                only_last_message
                and message_id != last_message_id
                and message_id != latest_job_application_id
            ):
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if key is None and message_id != last_message_id:
                store.mark_message_seen(chat_id, message_id, None)
                continue
            # IDs are opaque, not a chronological sequence. A newly received
            # message can sort below the cursor ID within the same second.
            # Exact replay protection is provided by is_processed above;
            # only strictly older timestamps belong behind the watermark.
            if key is not None and cursor is not None and key[0] < cursor[0]:
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if terminal:
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            if message.get("direction") != "in":
                created = normalized_created(message)
                # Reconcile a POST with a lost response before mistaking its
                # outgoing message for a human operator taking over the chat.
                probe = state
                if state.reactivation_reply_inflight_at and state.reactivation_reply_index < len(state.reactivation_reply_messages):
                    probe = ConversationState(
                        reminder_inflight_started_at=state.reactivation_reply_inflight_at,
                        reminder_inflight_text=state.reactivation_reply_messages[state.reactivation_reply_index],
                    )
                if _find_delivered_inflight_reminder([message], probe):
                    store.mark_bot_outgoing(chat_id, message_id)
                is_new_manual_outgoing = (
                    message.get("type") == "text"
                    and created is not None
                    and not store.is_bot_outgoing(chat_id, message_id)
                )
                if (
                    is_new_manual_outgoing
                    and manual_takeover_after is not None
                    and created >= manual_takeover_after
                ):
                    stop_reminders(state, permanently=True)
                    state.step = "manual_takeover"
                    state.application_status = "manual"
                    state.last_error = None
                    state.manual_takeover_at = datetime.now(timezone.utc).isoformat()
                    state.manual_takeover_message_id = message_id
                    store.save(chat_id, state)
                    terminal = True
                    print(
                        f"manual takeover chat_id={chat_id} "
                        f"message_id={message_id}; bot paused"
                    )
                elif (
                    is_new_manual_outgoing
                    and reminder_manual_stop_after is not None
                    and created >= reminder_manual_stop_after
                ):
                    stop_reminders(state, permanently=True)
                    store.save(chat_id, state)
                    print(
                        f"manual outgoing stopped reminders chat_id={chat_id} "
                        f"message_id={message_id}"
                    )
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue

            if message.get("type") == "system":
                # Only the actual vacancy response starts a conversation.
                # Avito's job_apply_enrichment event is emitted when candidate
                # details are saved/revealed and must never restart the bot.
                if (
                    message_id != latest_job_application_id
                    or state.step != "idle"
                    or not is_job_application_system_message(message)
                ):
                    store.mark_message_seen(
                        chat_id, message_id, key[0] if key else None
                    )
                    continue
                yield chat_id, state, message, message_id, city, item_id
                continue

            if message.get("type") != "text":
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue

            content = message.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if not isinstance(text, str) or not text.strip():
                store.mark_message_seen(chat_id, message_id, key[0] if key else None)
                continue
            yield chat_id, state, message, message_id, city, item_id


def iter_unanswered_job_applications(
    client: AvitoClient,
    chats: list[dict[str, Any]],
    store: SQLiteStateStore,
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
    not_before_timestamp: float | None = None,
) -> Iterator[tuple[str, ConversationState, dict[str, Any], str, str | None, str | None]]:
    """Find recent application chats that have never received an outgoing text."""
    current = now or datetime.now(timezone.utc)
    cutoff = current.timestamp() - max(1, max_age_hours) * 3600
    if not_before_timestamp is not None:
        cutoff = max(cutoff, not_before_timestamp)
    for chat in chats:
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            continue
        state = store.load(chat_id)
        if state.step != "idle" or state.application_status != "collecting":
            continue

        history = get_chat_messages_if_available(
            client, chat_id, limit=CHAT_PAGE_LIMIT
        )
        if history is None:
            last_message = chat.get("last_message") or {}
            last_message_id = str(last_message.get("id") or "").strip()
            if last_message_id:
                store.mark_message_seen(
                    chat_id,
                    last_message_id,
                    normalized_created(last_message),
                )
            continue
        messages = oldest_first(history)
        triggers = [
            message
            for message in messages
            if is_job_application_system_message(message)
            and (normalized_created(message) or 0) >= cutoff
        ]
        if not triggers:
            continue
        if any(
            message.get("direction") == "out" and message.get("type") == "text"
            for message in messages
        ):
            continue

        later_texts = [
            message
            for message in messages
            if message.get("direction") == "in"
            and message.get("type") == "text"
            and (normalized_created(message) or 0)
            >= (normalized_created(triggers[-1]) or 0)
        ]
        message = later_texts[-1] if later_texts else triggers[-1]
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            continue
        city, item_id = extract_chat_context(chat)
        yield chat_id, state, message, message_id, city, item_id


def restore_terminal_reapplications(store: SQLiteStateStore) -> int:
    """Silence chats that were incorrectly reopened by the old poller.

    The previous implementation recorded the terminal status and step before
    resetting a chat.  Use those markers once, then remove them so this repair
    is idempotent and cannot affect ordinary active conversations.
    """
    restored = 0
    terminal_statuses = {"completed", "manual", "cancelled"}
    terminal_steps = {"done", "manual_takeover"}
    for chat_id, state in store.all_conversations():
        previous_status = str(
            state.notes.get("reapplication_previous_status") or ""
        ).strip()
        if previous_status not in terminal_statuses:
            continue
        previous_step = str(
            state.notes.get("reapplication_previous_step") or ""
        ).strip()
        if previous_step not in terminal_steps:
            previous_step = (
                "manual_takeover" if previous_status == "manual" else "done"
            )

        state.application_status = previous_status
        state.step = previous_step
        state.last_error = None
        state.next_retry_at = None
        state.reactivation_sent = False
        state.reactivation_reply_messages = []
        state.reactivation_reply_index = 0
        state.reactivation_reply_trigger_id = None
        state.reactivation_reply_inflight_at = None
        state.reactivation_reply_started_at = None
        stop_reminders(state, permanently=True)
        state.notes.pop("reapplication_previous_status", None)
        state.notes.pop("reapplication_previous_step", None)
        state.notes["terminal_reapplication_restored"] = "true"
        store.save(chat_id, state)
        restored += 1
    return restored


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


def sent_message_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    direct = str(response.get("id") or "").strip()
    if direct:
        return direct
    nested = response.get("message")
    if isinstance(nested, dict):
        nested_id = str(nested.get("id") or "").strip()
        if nested_id:
            return nested_id
    return None


def send_bot_message(
    client: AvitoClient,
    store: SQLiteStateStore,
    chat_id: str,
    text: str,
) -> dict[str, Any] | None:
    response = client.send_message(chat_id, text)
    message_id = sent_message_id(response)
    if message_id:
        store.mark_bot_outgoing(chat_id, message_id)
    return response


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    value = content.get("text") if isinstance(content, dict) else None
    return value if isinstance(value, str) else ""


def _message_datetime(message: dict[str, Any]) -> datetime | None:
    created = normalized_created(message)
    if created is None:
        return None
    try:
        return datetime.fromtimestamp(created, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _find_delivered_inflight_reminder(
    messages: list[dict[str, Any]], state: ConversationState
) -> tuple[str | None, datetime] | None:
    if not state.reminder_inflight_text or not state.reminder_inflight_started_at:
        return None
    try:
        started_at = datetime.fromisoformat(state.reminder_inflight_started_at)
        if started_at.tzinfo is None:
            return None
    except ValueError:
        return None
    earliest = started_at - timedelta(seconds=5)
    for message in reversed(oldest_first(messages)):
        if message.get("direction") != "out" or message.get("type") != "text":
            continue
        created_at = _message_datetime(message)
        if created_at is None or created_at < earliest:
            continue
        if _message_text(message) != state.reminder_inflight_text:
            continue
        message_id = str(message.get("id") or "").strip() or None
        return message_id, created_at
    return None


def _activity_after_reminder_arm(
    messages: list[dict[str, Any]],
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
) -> str | None:
    if not state.reminder_armed_at:
        return None
    try:
        armed_at = datetime.fromisoformat(state.reminder_armed_at)
        if armed_at.tzinfo is None:
            return "unknown"
    except ValueError:
        return "unknown"
    # Avito timestamps have second precision. Include the whole boundary
    # second; processed incoming and registered bot outgoing IDs below exclude
    # activity already handled before this question was armed.
    armed_at = armed_at.replace(microsecond=0)
    candidate_activity = False
    for message in oldest_first(messages):
        created_at = _message_datetime(message)
        if created_at is None or created_at < armed_at:
            continue
        message_id = str(message.get("id") or "").strip()
        direction = message.get("direction")
        if direction == "in" and message.get("type") != "system":
            # A response that advanced the conversation before this reminder
            # series was armed is not new activity, even when the Avito and VPS
            # clocks differ by a few seconds.
            if message_id and store.is_processed(chat_id, message_id):
                continue
            candidate_activity = True
            continue
        if direction != "out" or message.get("type") == "system":
            continue
        if message_id and store.is_bot_outgoing(chat_id, message_id):
            continue
        if (
            state.reminder_inflight_text
            and _message_text(message) == state.reminder_inflight_text
        ):
            continue
        return "manual"
    return "candidate" if candidate_activity else None


def process_due_reminders(
    client: AvitoClient,
    store: SQLiteStateStore,
    config: ReminderConfig,
    *,
    now: datetime | None = None,
) -> int:
    if not config.enabled:
        return 0
    current = now or datetime.now(timezone.utc)
    migrate_waiting_reminders(client, store, config, now=current)
    sent = 0
    reminder_steps = {"awaiting_warehouse", "awaiting_datetime"}
    if not config.single_24h_only:
        reminder_steps.add("awaiting_reactivation")
    for chat_id, state in store.all_conversations():
        if (
            state.reminders_stopped
            or state.application_status != "collecting"
            or state.step not in reminder_steps
            or state.reminder_policy_version < config.policy_version
        ):
            continue
        if not state.reminder_inflight_number and not reminder_is_due(
            state, now=current
        ):
            continue
        try:
            messages = client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT)
        except Exception as exc:  # noqa: BLE001 - isolate one chat/API failure
            status_code = http_error_status(exc)
            if status_code in {402, 404}:
                stop_reminders(state, permanently=True)
                state.notes["reminder_history_unavailable"] = str(status_code)
                store.save(chat_id, state)
                print(
                    "reminders stopped because chat history is unavailable "
                    f"chat_id={chat_id} status={status_code}"
                )
                continue
            print(f"reminder verification error chat_id={chat_id}: {exc}")
            continue

        if state.reminder_inflight_number:
            delivered = _find_delivered_inflight_reminder(messages, state)
            if delivered is not None:
                message_id, delivered_at = delivered
                if message_id:
                    store.mark_bot_outgoing(chat_id, message_id)
                finish_reminder(state, config, sent_at=delivered_at)
                store.save(chat_id, state)
                print(
                    f"reminder delivery reconciled chat_id={chat_id} "
                    f"sent_count={state.reminder_count}"
                )
                continue

        activity = _activity_after_reminder_arm(messages, store, chat_id, state)
        if activity == "candidate":
            stop_reminders(state)
            store.save(chat_id, state)
            print(f"reminders cancelled after candidate activity chat_id={chat_id}")
            continue
        if activity in {"manual", "unknown"}:
            stop_reminders(state, permanently=True)
            store.save(chat_id, state)
            print(f"reminders stopped after manual activity chat_id={chat_id}")
            continue

        if state.reminder_inflight_number:
            if not inflight_retry_is_due(state, config, now=current):
                continue
            clear_inflight_for_retry(state)
            store.save(chat_id, state)

        if not reminder_is_due(state, now=current):
            continue
        # Missed deadlines do not result in a burst after downtime.
        if state.reminder_armed_at:
            anchor = datetime.fromisoformat(state.reminder_armed_at)
            offsets = config.all_offsets
            for index, offset in enumerate(offsets):
                if current >= anchor + timedelta(seconds=offset):
                    state.reminder_count = max(state.reminder_count, index)
        try:
            text = reminder_message(state)
        except ValueError as exc:
            stop_reminders(state, permanently=True)
            store.save(chat_id, state)
            print(f"reminder configuration error chat_id={chat_id}: {exc}")
            continue
        try:
            mark_reminder_inflight(state, text, now=current)
            store.save(chat_id, state)
            response = send_bot_message(client, store, chat_id, text)
            if not sent_message_id(response):
                # A success without an id still requires history confirmation.
                # Keep the outbox intact so the outgoing cannot look manual.
                continue
        except Exception as exc:  # noqa: BLE001 - preserve inflight state for retry
            print(f"reminder send error chat_id={chat_id}: {exc}")
            continue
        finish_reminder(state, config, sent_at=current)
        store.save(chat_id, state)
        sent += 1
        print(
            f"reminder sent chat_id={chat_id} sent_count={state.reminder_count} "
            f"step={state.step}"
        )
    return sent


def migrate_waiting_reminders(
    client: AvitoClient, store: SQLiteStateStore, config: ReminderConfig,
    *, now: datetime, batch_size: int = 20,
) -> int:
    """Migrate verified unanswered bot questions; never revive manual/history chats.

    A bounded batch protects normal polling. Errors are retried later and do not
    turn uncertain history into permission to send a message.
    """
    if not config.enabled:
        return 0
    if config.single_24h_only:
        return _migrate_single_reminder_policy(
            client, store, config, now=now, batch_size=500
        )
    migrated = checked = 0
    for chat_id, state in store.all_conversations():
        if state.reminder_policy_version >= 2 or state.reminder_inflight_number:
            continue
        if state.reminders_stopped or state.application_status != "collecting" or state.step not in {
            "awaiting_warehouse", "awaiting_datetime"
        } or state.last_error or state.manual_takeover_at or state.notes.get("preinstallation_chat_quarantined"):
            continue
        retry_at = state.notes.get("reminder_migration_retry_at")
        if retry_at and retry_at > now.isoformat():
            continue
        if checked >= batch_size:
            break
        checked += 1
        try:
            messages = oldest_first(client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT))
        except Exception:  # noqa: BLE001 - retry history lookup without authorizing sends
            state.notes["reminder_migration_retry_at"] = (now + timedelta(minutes=5)).isoformat()
            store.save(chat_id, state)
            continue
        prompt = next((m for m in reversed(messages) if (
            m.get("direction") == "out"
            and not _message_text(m).startswith("Напоминаю")
            and infer_step_from_bot_message(_message_text(m)) == state.step
            and store.is_bot_outgoing(chat_id, str(m.get("id") or ""))
        )), None)
        anchor = _message_datetime(prompt) if prompt else None
        state.reminder_policy_version = 2
        state.notes.pop("reminder_migration_retry_at", None)
        if anchor is None:
            stop_reminders(state)
            state.notes["reminder_migration"] = "no_verified_question"
            store.save(chat_id, state)
            continue
        # Include processed replies too. IDs cannot establish chronology within
        # one second: ambiguous same-second activity must prevent reactivation.
        prompt_index = messages.index(prompt)
        later = messages[prompt_index + 1:]
        later.extend(
            message for message in messages[:prompt_index]
            if (created := _message_datetime(message)) is not None
            and created >= anchor.replace(microsecond=0)
        )
        manual = any(m.get("direction") == "out" and m.get("type") != "system"
                     and not store.is_bot_outgoing(chat_id, str(m.get("id") or "")) for m in later)
        answered = any(m.get("direction") == "in" and m.get("type") != "system" for m in later)
        if manual or answered:
            stop_reminders(state, permanently=manual)
            state.notes["reminder_migration"] = "manual" if manual else "answered"
            store.save(chat_id, state)
            continue
        old_count = state.reminder_count
        arm_reminders(state, config, now=anchor)
        # Keep sent milestones and skip missed ones, rather than replaying them.
        elapsed = (now - anchor).total_seconds()
        milestone = max((i for i, value in enumerate(config.delays_seconds) if elapsed >= value), default=0)
        state.reminder_count = min(2, max(old_count, milestone))
        state.reminder_due_at = max(now, anchor + timedelta(seconds=config.delays_seconds[state.reminder_count])).isoformat()
        state.notes["reminder_migration"] = "verified_question"
        store.save(chat_id, state)
        migrated += 1
    return migrated


def _migrate_single_reminder_policy(
    client: AvitoClient,
    store: SQLiteStateStore,
    config: ReminderConfig,
    *,
    now: datetime,
    batch_size: int,
) -> int:
    """Retire old multi-step campaigns without replaying an overdue backlog."""
    migrated = 0
    for chat_id, state in store.all_conversations():
        if migrated >= batch_size:
            break
        if (
            state.reminder_policy_version >= config.policy_version
            or state.application_status != "collecting"
        ):
            continue
        if state.reminder_inflight_number:
            try:
                messages = client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT)
            except Exception:  # noqa: BLE001 - fail closed until history returns
                continue
            delivered = _find_delivered_inflight_reminder(messages, state)
            if delivered:
                message_id, _ = delivered
                if message_id:
                    store.mark_bot_outgoing(chat_id, message_id)
                state.reminder_count = max(
                    state.reminder_count, state.reminder_inflight_number
                )
                clear_inflight_for_retry(state)
                stop_reminders(state, permanently=True)
                state.reminder_policy_version = config.policy_version
                state.notes["reminder_migration"] = "legacy_inflight_delivered"
                store.save(chat_id, state)
                migrated += 1
                continue
            if not inflight_retry_is_due(state, config, now=now):
                continue
            clear_inflight_for_retry(state)
        if state.step in {"awaiting_reactivation", "sending_reactivation_intro"}:
            state.step = (
                "awaiting_warehouse"
                if warehouse_group_for_city(state.city)
                and state.warehouse_choice is None
                else "awaiting_datetime"
            )
            state.reactivation_reply_messages = []
            state.reactivation_reply_trigger_id = None
            state.reactivation_reply_inflight_at = None
            stop_reminders(state, permanently=True)
            state.reminder_policy_version = config.policy_version
            state.notes["reminder_migration"] = "legacy_campaign_retired"
            store.save(chat_id, state)
            migrated += 1
            continue
        if state.step not in {"awaiting_warehouse", "awaiting_datetime"}:
            continue
        state.reminder_policy_version = config.policy_version
        if (
            state.reminders_stopped
            or state.reminder_count > 0
            or state.reactivation_sent
        ):
            stop_reminders(state, permanently=state.reminders_stopped)
            state.notes["reminder_migration"] = "already_reminded"
            store.save(chat_id, state)
            migrated += 1
            continue
        try:
            anchor = datetime.fromisoformat(state.reminder_armed_at or "")
        except ValueError:
            anchor = None
        if anchor is None or anchor.tzinfo is None:
            stop_reminders(state)
            state.notes["reminder_migration"] = "missing_anchor"
        else:
            due_at = anchor + timedelta(seconds=config.single_delay_seconds)
            if due_at <= now:
                # The new policy must not suddenly message thousands of old
                # chats when it is enabled. Only still-future 24h reminders
                # are preserved; missed historical windows are retired.
                stop_reminders(state)
                state.notes["reminder_migration"] = "missed_single_window"
            else:
                state.reminder_step = state.step
                state.reminder_due_at = due_at.isoformat()
                state.notes["reminder_migration"] = "single_24h_scheduled"
        store.save(chat_id, state)
        migrated += 1
    return migrated


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
    regional_locations: RegionalLocationCatalog | None = None,
) -> bool:
    missing = missing_application_fields(state)
    if missing and state.application_status != "submitted":
        return_to_collection(store, chat_id, state, missing)
        print(
            f"application deferred chat_id={chat_id}: missing {', '.join(missing)}"
        )
        return False

    if not state.processing_notice_sent:
        send_bot_message(
            client,
            store,
            chat_id,
            "Спасибо, данные получили. Завершаю запись — это может занять до минуты.",
        )
        state.processing_notice_sent = True
        store.save(chat_id, state)

    if (
        regional_locations is not None
        and state.warehouse_selection_source == "regional_catalog"
        and state.service_center
    ):
        current_location = regional_locations.resolve(
            state.service_center,
            None,
        )
        state.address = current_location.address
        state.internship_time = current_location.internship_time
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
        send_bot_message(client, store, chat_id, invitation)
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
            history = get_chat_messages_if_available(client, chat_id, limit=100)
            if history is None:
                last_message = chat.get("last_message") or {}
                last_message_id = str(last_message.get("id") or "").strip()
                if last_message_id:
                    store.mark_message_seen(
                        chat_id,
                        last_message_id,
                        normalized_created(last_message),
                    )
                    count += 1
                continue
            messages = oldest_first(history)
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
                if inferred_step == "done":
                    state.application_status = "completed"
                elif inferred_step == "manual_takeover":
                    stop_reminders(state, permanently=True)
                    state.application_status = "manual"
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
        return "sending_intro"
    if normalized.startswith("Подобрали для вас склады"):
        return "awaiting_warehouse"
    if "❗️Подобрали для вас склады" in normalized:
        return "awaiting_warehouse"
    if (
        normalized.startswith("❗️Напоминаю — вакансия ещё актуальна.\n")
        and "Подобрали для вас склады" in normalized
    ):
        return "awaiting_warehouse"
    if (
        normalized.startswith("Стажировка каждый день в ")
        and "на какой день вас записать?" in normalized
    ):
        return "awaiting_datetime"
    if normalized == CONFIRMATION_MESSAGE.strip():
        return "awaiting_full_name"
    if normalized in {DATE_REMINDER_MESSAGE.strip(), LEGACY_DATE_REMINDER_MESSAGE.strip()}:
        return "awaiting_datetime"
    if normalized in {MORE_INFO_MESSAGE.strip(), LEGACY_MORE_INFO_MESSAGE.strip()}:
        return "awaiting_call"
    if normalized in {CALL_HANDOFF_MESSAGE.strip(), LEGACY_CALL_HANDOFF_MESSAGE.strip()}:
        return "manual_takeover"
    if normalized.startswith("Здравствуйте.\nХотели уточнить, актуальна ли для вас еще вакансия"):
        return "awaiting_reactivation"
    if normalized == "И номер":
        return "awaiting_phone"
    if normalized.startswith("Готово, вы записаны!"):
        return "done"
    return None


def migrate_legacy_completed_chats(
    client: AvitoClient, store: SQLiteStateStore, chats: list[dict[str, Any]]
) -> int:
    """Close chats whose latest recognized legacy bot message is its final reply."""
    metadata_key = "legacy_completed_chats_migrated_v1"
    if store.get_metadata(metadata_key) == "true":
        return 0
    changed = 0
    for chat in chats:
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            continue
        state = store.load(chat_id)
        if state.application_status == "completed" or state.step == "done":
            continue
        latest_step = None
        history = get_chat_messages_if_available(client, chat_id, limit=100)
        if history is None:
            continue
        for message in oldest_first(history):
            if message.get("direction") != "out" or message.get("type") != "text":
                continue
            content = message.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            inferred = infer_step_from_bot_message(text)
            if inferred:
                latest_step = inferred
        if latest_step == "done":
            state.step = "done"
            state.application_status = "completed"
            store.save(chat_id, state)
            changed += 1
    store.set_metadata(metadata_key, "true")
    return changed


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
        history = get_chat_messages_if_available(client, chat_id, limit=100)
        if history is None:
            continue
        restore_collected_fields(state, history)
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


def send_initial_sequence(
    client: AvitoClient,
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    message_id: str,
    reminder_config: ReminderConfig | None = None,
) -> bool:
    messages = initial_messages_for_city(state.city)
    if not messages:
        state.step = "unsupported"
        state.intro_messages_sent = 0
        state.intro_trigger_message_id = None
        store.save(chat_id, state)
        return False

    if state.step == "idle":
        state.step = "sending_intro"
        state.intro_messages_sent = 0
        state.intro_trigger_message_id = message_id
        store.save(chat_id, state)

    for outgoing_text in messages[state.intro_messages_sent :]:
        send_bot_message(client, store, chat_id, outgoing_text)
        state.intro_messages_sent += 1
        store.save(chat_id, state)

    state.step = "awaiting_warehouse"
    arm_reminders(state, reminder_config or ReminderConfig())
    store.save(chat_id, state)
    return True


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
    *,
    regional_locations: RegionalLocationCatalog | None = None,
    regional_overrides: dict[str, str] | None = None,
    reminder_config: ReminderConfig | None = None,
) -> None:
    configured_reminders = reminder_config or ReminderConfig()
    content = message.get("content") or {}
    text = content.get("text", "")
    state.city = city or state.city
    state.item_id = item_id or state.item_id

    if configured_reminders.single_24h_only and state.step in {
        "awaiting_call",
        "awaiting_reactivation",
        "sending_reactivation_intro",
    }:
        state.step = (
            "awaiting_warehouse"
            if warehouse_group_for_city(state.city)
            and state.warehouse_choice is None
            else "awaiting_datetime"
        )
        state.reactivation_reply_messages = []
        state.reactivation_reply_trigger_id = None
        state.reactivation_reply_inflight_at = None
        stop_reminders(state, permanently=True)
        state.reminder_policy_version = configured_reminders.policy_version
        store.save(chat_id, state)

    if state.application_status in {"completed", "manual", "cancelled"} or state.step in {"done", "manual_takeover"}:
        store.mark_message_seen(chat_id, message_id, normalized_created(message))
        return
    if state.reminder_inflight_number:
        delivered = _find_delivered_inflight_reminder(
            client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT), state
        )
        if delivered:
            if delivered[0]:
                store.mark_bot_outgoing(chat_id, delivered[0])
            finish_reminder(state, configured_reminders, sent_at=delivered[1])
            store.save(chat_id, state)

    if (
        not configured_reminders.single_24h_only
        and state.step == "awaiting_reactivation"
        and state.reminders_stopped
    ):
        store.mark_message_seen(chat_id, message_id, normalized_created(message))
        return

    if (
        not configured_reminders.single_24h_only
        and
        state.step == "sending_reactivation_intro"
        and message_id != state.reactivation_reply_trigger_id
        and state.reactivation_reply_inflight_at
        and state.reactivation_reply_index < len(state.reactivation_reply_messages)
    ):
        # A new reply supersedes the pending sequence, but must not erase our
        # ability to identify an already delivered POST whose response was lost.
        probe = ConversationState(
            reminder_inflight_text=state.reactivation_reply_messages[state.reactivation_reply_index],
            reminder_inflight_started_at=state.reactivation_reply_inflight_at,
        )
        delivered = _find_delivered_inflight_reminder(
            client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT), probe
        )
        if delivered and delivered[0]:
            store.mark_bot_outgoing(chat_id, delivered[0])

    if (
        not configured_reminders.single_24h_only
        and state.step == "sending_reactivation_intro"
        and is_call_request(text)
    ):
        state.reactivation_reply_messages = []
        state.reactivation_reply_trigger_id = None
        state.reactivation_reply_inflight_at = None
    elif (
        not configured_reminders.single_24h_only
        and state.step == "sending_reactivation_intro"
    ):
        trigger = state.reactivation_reply_trigger_id
        if message_id == trigger:
            resume_reactivation_sequence(client, store, chat_id, state, configured_reminders)
            return
        # A new candidate response supersedes unsent questions; do not deadlock
        # waiting for a retry while that response is already available.
        state.reactivation_reply_messages = []
        state.reactivation_reply_inflight_at = None
        state.reactivation_reply_trigger_id = None
        state.step = "awaiting_warehouse" if warehouse_group_for_city(state.city) else "awaiting_datetime"
        if trigger:
            store.mark_message_seen(chat_id, trigger, None)

    if (
        not configured_reminders.single_24h_only
        and state.step == "awaiting_reactivation"
        and is_reactivation_acceptance(text)
    ):
        prepare_reactivation_sequence(
            store, chat_id, state, message_id, regional_locations, regional_overrides
        )
        resume_reactivation_sequence(client, store, chat_id, state, configured_reminders)
        return

    if (
        regional_locations is not None
        and state.step in {"idle", "sending_regional_intro"}
        and warehouse_group_for_city(state.city) is None
    ):
        send_regional_initial_sequence(
            client,
            store,
            chat_id,
            state,
            message_id,
            regional_locations,
            regional_overrides,
            configured_reminders,
        )
        store.mark_message_seen(chat_id, message_id, normalized_created(message))
        print(
            f"regional application started chat_id={chat_id} "
            f"message_id={message_id} city={state.city!r} "
            f"service_center={state.service_center!r}"
        )
        return

    if (
        state.step in {"idle", "sending_intro"}
        and warehouse_group_for_city(state.city) is not None
    ):
        reply_sent = send_initial_sequence(
            client,
            store,
            chat_id,
            state,
            message_id,
            configured_reminders,
        )
        store.mark_message_seen(chat_id, message_id, normalized_created(message))
        state.intro_trigger_message_id = None
        store.save(chat_id, state)
        print(
            f"message processed chat_id={chat_id} message_id={message_id} "
            f"reply_sent={reply_sent} step={state.step} "
            f"application_status={state.application_status}"
        )
        return

    if (
        state.step == "awaiting_warehouse"
        and state.intro_trigger_message_id == message_id
    ):
        store.mark_message_seen(chat_id, message_id, normalized_created(message))
        state.intro_trigger_message_id = None
        store.save(chat_id, state)
        return
    stop_reminders(state)
    # Persist cancellation before parsing or replying. Even if processing the
    # candidate response fails, an already-due reminder must not race it.
    store.save(chat_id, state)
    reply = handle_user_message(
        state,
        text,
        city_hint=state.city,
        operator_handoff_enabled=not configured_reminders.single_24h_only,
    )
    if reply:
        send_bot_message(client, store, chat_id, reply)
        arm_reminders(state, configured_reminders)

    store.save(chat_id, state)
    store.mark_message_seen(chat_id, message_id, normalized_created(message))

    if state.application_status in {"pending", "submitted"}:
        complete_pending_application(
            client,
            workflow,
            store,
            chat_id,
            state,
            regional_locations,
        )
    print(
        f"message processed chat_id={chat_id} message_id={message_id} "
        f"reply_sent={bool(reply)} step={state.step} "
        f"application_status={state.application_status}"
    )


def send_regional_initial_sequence(
    client: AvitoClient,
    store: SQLiteStateStore,
    chat_id: str,
    state: ConversationState,
    message_id: str,
    catalog: RegionalLocationCatalog,
    overrides: dict[str, str] | None = None,
    reminder_config: ReminderConfig | None = None,
) -> None:
    location = catalog.resolve(state.city, state.item_id, overrides)
    if state.regional_intro_trigger_message_id != message_id:
        state.regional_intro_trigger_message_id = message_id
        state.regional_intro_messages_sent = 0

    state.step = "sending_regional_intro"
    state.service_center = location.service_center
    state.warehouse_selection_source = "regional_catalog"
    state.address = location.address
    state.internship_time = location.internship_time
    store.save(chat_id, state)

    messages = regional_initial_messages(location)
    for outgoing_text in messages[state.regional_intro_messages_sent :]:
        send_bot_message(client, store, chat_id, outgoing_text)
        state.regional_intro_messages_sent += 1
        store.save(chat_id, state)

    state.step = "awaiting_datetime"
    state.regional_intro_trigger_message_id = None
    arm_reminders(state, reminder_config or ReminderConfig())
    store.save(chat_id, state)


def prepare_reactivation_sequence(
    store: SQLiteStateStore, chat_id: str, state: ConversationState,
    message_id: str, catalog: RegionalLocationCatalog | None,
    overrides: dict[str, str] | None = None,
) -> None:
    """Persist the complete reply before the first send; keep candidate data."""
    if warehouse_group_for_city(state.city):
        prompt = warehouse_prompt_for_city(state.city)
        if not prompt:
            raise ValueError("Не найден каталог складов для повторной записи")
        texts = [f'❗️{prompt}\nили "0" если не актуально.']
    else:
        if catalog is None:
            raise ValueError("Каталог регионов недоступен для повторной записи")
        location = catalog.resolve(state.city, state.item_id, overrides)
        state.service_center = location.service_center
        state.address = location.address
        state.internship_time = location.internship_time
        state.warehouse_selection_source = "regional_catalog"
        texts = list(regional_initial_messages(location)[-2:])
    if any(not text or len(text) > 1000 for text in texts):
        raise ValueError("Сообщение повторной записи не укладывается в лимит Avito")
    stop_reminders(state)
    state.reactivation_sent = True
    state.step = "sending_reactivation_intro"
    state.reactivation_reply_messages = texts
    state.reactivation_reply_index = 0
    state.reactivation_reply_trigger_id = message_id
    state.reactivation_reply_inflight_at = None
    state.reactivation_reply_started_at = datetime.now(timezone.utc).isoformat()
    store.save(chat_id, state)


def resume_reactivation_sequence(
    client: AvitoClient, store: SQLiteStateStore, chat_id: str,
    state: ConversationState, config: ReminderConfig,
) -> None:
    if state.step != "sending_reactivation_intro" or state.application_status != "collecting" or state.reminders_stopped:
        return
    texts = state.reactivation_reply_messages
    if not texts or not state.reactivation_reply_trigger_id:
        raise ValueError("Повреждено состояние повторного приглашения")
    # Also verify history once every reply has been delivered. Otherwise a
    # recovered last POST could re-arm timers over an intervening human reply.
    while True:
        text = texts[state.reactivation_reply_index] if state.reactivation_reply_index < len(texts) else None
        now = datetime.now(timezone.utc)
        messages = client.get_messages(chat_id, limit=CHAT_PAGE_LIMIT)
        if state.reactivation_reply_inflight_at and text is not None:
            # A timed-out POST may already have been delivered. Reconcile before retry.
            probe = ConversationState(
                reminder_inflight_text=text,
                reminder_inflight_started_at=state.reactivation_reply_inflight_at,
            )
            delivered = _find_delivered_inflight_reminder(messages, probe)
            if delivered:
                if delivered[0]:
                    store.mark_bot_outgoing(chat_id, delivered[0])
                state.reactivation_reply_index += 1
                state.reactivation_reply_inflight_at = None
                store.save(chat_id, state)
                continue
            started = datetime.fromisoformat(state.reactivation_reply_inflight_at)
            if now < started + timedelta(seconds=config.inflight_grace_seconds):
                return
        if state.reactivation_reply_started_at:
            # Match Avito's second precision so a simultaneous human reply is
            # not discarded as older than our local sub-second timestamp.
            started = datetime.fromisoformat(state.reactivation_reply_started_at).replace(microsecond=0)
            pending_candidate = False
            for message in messages:
                created = _message_datetime(message)
                if created is None or created < started or message.get("type") == "system":
                    continue
                mid = str(message.get("id") or "")
                if message.get("direction") == "out" and not store.is_bot_outgoing(chat_id, mid):
                    stop_reminders(state, permanently=True)
                    state.application_status = "manual"
                    state.step = "manual_takeover"
                    state.reactivation_reply_messages = []
                    store.save(chat_id, state)
                    return
                if message.get("direction") == "in" and mid != state.reactivation_reply_trigger_id and not store.is_processed(chat_id, mid):
                    pending_candidate = True
            if pending_candidate:
                return
        if text is None:
            break
        state.reactivation_reply_inflight_at = now.isoformat()
        store.save(chat_id, state)
        response = send_bot_message(client, store, chat_id, text)
        if not sent_message_id(response):
            return
        state.reactivation_reply_index += 1
        state.reactivation_reply_inflight_at = None
        store.save(chat_id, state)
    state.step = "awaiting_warehouse" if warehouse_group_for_city(state.city) else "awaiting_datetime"
    store.mark_message_seen(chat_id, state.reactivation_reply_trigger_id, None)
    state.reactivation_reply_trigger_id = None
    state.reactivation_reply_messages = []
    arm_reminders(state, config)
    store.save(chat_id, state)


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
    switched_account, archived_conversations = store.bind_account(
        account_fingerprint(os.getenv("AVITO_USER_ID", ""))
    )
    if switched_account:
        print(
            "Avito account change detected; archived previous account state "
            f"conversations={archived_conversations}"
        )
    reminder_config = ReminderConfig.from_env()
    restored_reapplications = restore_terminal_reapplications(store)
    if restored_reapplications:
        print(
            "Restored terminal state for "
            f"{restored_reapplications} incorrectly reopened chat(s)"
        )
    manual_takeover_after = None
    if os.getenv("PAUSE_ON_MANUAL_OUTGOING", "false").strip().lower() == "true":
        manual_takeover_key = "manual_takeover_started_at_v1"
        configured_boundary = store.get_metadata(manual_takeover_key)
        if configured_boundary is None:
            manual_takeover_after = datetime.now(timezone.utc).timestamp()
            store.set_metadata(manual_takeover_key, str(manual_takeover_after))
            print(
                "Manual takeover baseline initialized; "
                "pre-existing outgoing messages were skipped"
            )
        else:
            try:
                manual_takeover_after = float(configured_boundary)
            except ValueError:
                manual_takeover_after = datetime.now(timezone.utc).timestamp()
                store.set_metadata(
                    manual_takeover_key,
                    str(manual_takeover_after),
                )
        print("Manual takeover detection enabled")
    reminder_manual_stop_after = None
    if reminder_config.enabled:
        reminder_boundary_key = "reminder_manual_stop_started_at_v1"
        configured_boundary = store.get_metadata(reminder_boundary_key)
        if configured_boundary is None:
            reminder_manual_stop_after = datetime.now(timezone.utc).timestamp()
            store.set_metadata(
                reminder_boundary_key, str(reminder_manual_stop_after)
            )
        else:
            try:
                reminder_manual_stop_after = float(configured_boundary)
            except ValueError:
                reminder_manual_stop_after = datetime.now(timezone.utc).timestamp()
                store.set_metadata(
                    reminder_boundary_key, str(reminder_manual_stop_after)
                )
        print(
            "Follow-up reminders enabled with question-relative offsets="
            f"{reminder_config.all_offsets}s"
        )
    interrupted = store.quarantine_interrupted_submissions()
    if interrupted:
        print(
            f"WARNING: quarantined {interrupted} interrupted form submission(s); "
            "manual verification is required"
        )
    workflow = CandidateWorkflow.from_env(YandexFormSubmitter.from_env())
    regional_source = GoogleSheetRegionalLocationSource(
        os.getenv(
            "REGIONAL_LOCATIONS_SHEET_ID",
            DEFAULT_REGIONAL_LOCATIONS_SHEET_ID,
        ),
        os.getenv(
            "REGIONAL_LOCATIONS_SHEET_GID",
            DEFAULT_REGIONAL_LOCATIONS_GID,
        ),
        timeout=max(
            5,
            int(os.getenv("REGIONAL_LOCATIONS_TIMEOUT_SECONDS", "30")),
        ),
    )
    regional_cache_path = os.getenv(
        "REGIONAL_LOCATIONS_CACHE_PATH",
        str(Path(PROJECT_ROOT) / "data" / "regional_locations.csv"),
    )
    regional_refresh_seconds = max(
        60,
        int(os.getenv("REGIONAL_LOCATIONS_REFRESH_SECONDS", "300")),
    )
    try:
        regional_provider = RefreshingRegionalLocationProvider(
            regional_source,
            regional_cache_path,
            regional_refresh_seconds,
        )
    except Exception as exc:
        print(f"Failed to load regional locations: {exc}")
        return
    regional_locations = regional_provider.catalog
    regional_overrides = parse_service_center_overrides(
        os.getenv("SERVICE_CENTER_OVERRIDES_JSON", "")
    )


    interval = max(5, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
    warehouse_source = GoogleSheetWarehouseSource(
        sheet_id=os.getenv(
            "WAREHOUSE_LOCATIONS_SHEET_ID",
            DEFAULT_WAREHOUSE_LOCATIONS_SHEET_ID,
        ),
        gid=os.getenv(
            "WAREHOUSE_LOCATIONS_SHEET_GID",
            DEFAULT_WAREHOUSE_LOCATIONS_SHEET_GID,
        ),
        timeout=max(
            1,
            int(os.getenv("WAREHOUSE_LOCATIONS_TIMEOUT_SECONDS", "30")),
        ),
    )
    warehouse_cache_path = os.getenv(
        "WAREHOUSE_LOCATIONS_CACHE_PATH",
        str(Path(PROJECT_ROOT) / "data" / "warehouse_locations.csv"),
    )
    warehouse_refresh_seconds = max(
        60,
        int(os.getenv("WAREHOUSE_LOCATIONS_REFRESH_SECONDS", "300")),
    )
    try:
        warehouse_provider = RefreshingWarehouseProvider(
            warehouse_source,
            warehouse_cache_path,
            warehouse_refresh_seconds,
            WAREHOUSE_GROUPS,
        )
    except Exception as exc:
        print(f"Failed to load warehouse locations: {exc}")
        return
    replace_warehouse_groups(warehouse_provider.groups)
    print(
        f"Starting poller with interval={interval}s state_db={state_path} "
        f"regional_locations={len(regional_locations)} "
        f"regional_locations_cache={regional_source.last_load_used_cache} "
        f"regional_locations_refresh={regional_refresh_seconds}s "
        f"warehouse_locations_cache={warehouse_source.last_load_used_cache} "
        f"warehouse_locations_refresh={warehouse_refresh_seconds}s"
    )

    recovery_key = "unanswered_recovery_started_at_v1"
    recovery_started_at = store.get_metadata(recovery_key)
    if recovery_started_at is None:
        recovery_cutoff = datetime.now(timezone.utc).timestamp()
        store.set_metadata(recovery_key, str(recovery_cutoff))
        print(
            "Unanswered application recovery baseline initialized; "
            "pre-existing applications were skipped"
        )
    else:
        try:
            recovery_cutoff = float(recovery_started_at)
        except ValueError:
            recovery_cutoff = datetime.now(timezone.utc).timestamp()
            store.set_metadata(recovery_key, str(recovery_cutoff))

    try:
        client.get_access_token()
        print("Avito auth OK")
    except Exception as exc:
        print(f"WARNING: initial Avito auth failed; polling will retry: {exc}")

    env_path = Path(PROJECT_ROOT) / ".env"
    startup_env_signature = env_file_signature(env_path)
    startup_initialization_pending = True
    consecutive_poll_errors = 0
    processed_since_health = 0
    health_interval = max(
        60, int(os.getenv("HEALTH_LOG_INTERVAL_SECONDS", "300"))
    )
    next_health_log = time.monotonic() + health_interval
    while True:
        if env_file_signature(env_path) != startup_env_signature:
            print("Environment file changed; restarting to apply new settings")
            return
        if time.monotonic() >= regional_provider.next_refresh_at:
            if regional_provider.refresh_if_due():
                regional_locations = regional_provider.catalog
                if regional_source.last_load_used_cache:
                    print(
                        "WARNING: regional locations refresh used cached data; "
                        "Google Sheet is temporarily unavailable"
                    )
                else:
                    print(
                        "Regional locations refreshed from Google Sheet: "
                        f"{len(regional_locations)}"
                    )
            elif regional_provider.last_error is not None:
                print(
                    "WARNING: regional locations refresh failed; "
                    f"keeping previous catalog: {regional_provider.last_error}"
                )
        if time.monotonic() >= warehouse_provider.next_refresh_at:
            if warehouse_provider.refresh_if_due():
                replace_warehouse_groups(warehouse_provider.groups)
                if warehouse_source.last_load_used_cache:
                    print(
                        "WARNING: warehouse locations refresh used cached data; "
                        "Google Sheet is temporarily unavailable"
                    )
                else:
                    print("Warehouse locations refreshed from Google Sheet")
            elif warehouse_provider.last_error is not None:
                print(
                    "WARNING: warehouse locations refresh failed; "
                    f"keeping previous catalog: {warehouse_provider.last_error}"
                )
        try:
            # A recruiter can open a chat before the next polling cycle. Avito
            # then removes it from the unread list even though the bot has not
            # processed its last message. Local message IDs and cursors already
            # provide the required deduplication, so inspect all recent chats.
            chats = client.get_chats(
                unread_only=False, limit=CHAT_PAGE_LIMIT
            )
            if startup_initialization_pending:
                initialize_message_cursor(client, store, chats)
                migrated_completed = migrate_legacy_completed_chats(
                    client, store, chats
                )
                repaired, returned = reconcile_incomplete_applications(
                    client, store
                )
                recovered_unanswered = 0
                for values in iter_unanswered_job_applications(
                    client,
                    chats,
                    store,
                    not_before_timestamp=recovery_cutoff,
                ):
                    try:
                        process_chat_message(
                            client,
                            workflow,
                            store,
                            *values,
                            regional_locations=regional_locations,
                            regional_overrides=regional_overrides,
                            reminder_config=reminder_config,
                        )
                        recovered_unanswered += 1
                    except Exception as exc:
                        print(
                            "unanswered application recovery error "
                            f"chat_id={values[0]}: {exc}"
                        )
                if migrated_completed:
                    print(
                        f"Migrated legacy completed chats: {migrated_completed}"
                    )
                if repaired or returned:
                    print(
                        "Reconciled legacy applications: "
                        f"repaired={repaired}, "
                        f"returned_to_collection={returned}"
                    )
                if recovered_unanswered:
                    print(
                        "Recovered unanswered applications: "
                        f"{recovered_unanswered}"
                    )
                startup_initialization_pending = False
                print("Poller startup initialization completed")
            failed_chats: set[str] = set()
            for values in iter_new_chat_messages(
                client,
                chats,
                store,
                not_before_timestamp=recovery_cutoff,
                manual_takeover_after=manual_takeover_after,
                reminder_manual_stop_after=reminder_manual_stop_after,
            ):
                if values[0] in failed_chats:
                    continue
                try:
                    process_chat_message(
                        client,
                        workflow,
                        store,
                        *values,
                        regional_locations=regional_locations,
                        regional_overrides=regional_overrides,
                        reminder_config=reminder_config,
                    )
                    processed_since_health += 1
                except Exception as exc:
                    failed_chats.add(values[0])
                    print(f"message error chat_id={values[0]}: {exc}")

            for chat_id, state in store.all_conversations():
                if (
                    not reminder_config.single_24h_only
                    and state.step == "sending_reactivation_intro"
                    and chat_id not in failed_chats
                ):
                    try:
                        resume_reactivation_sequence(client, store, chat_id, state, reminder_config)
                    except Exception as exc:
                        print(f"reactivation reply error chat_id={chat_id}: {exc}")

            processed_since_health += process_due_reminders(
                client,
                store,
                reminder_config,
            )

            for chat_id, state in store.pending():
                complete_pending_application(
                    client,
                    workflow,
                    store,
                    chat_id,
                    state,
                    regional_locations,
                )
            consecutive_poll_errors = 0
        except Exception as exc:
            consecutive_poll_errors += 1
            print(f"poller error: {exc}")
            alert_after = max(
                1, int(os.getenv("POLL_ERROR_ALERT_AFTER_ATTEMPTS", "3"))
            )
            if consecutive_poll_errors == alert_after:
                emit_alert(
                    "poller repeatedly failed "
                    f"attempts={consecutive_poll_errors} error={exc}"
                )
        if time.monotonic() >= next_health_log:
            print(
                "poller health OK "
                f"processed_since_last={processed_since_health} "
                f"consecutive_errors={consecutive_poll_errors}"
            )
            processed_since_health = 0
            next_health_log = time.monotonic() + health_interval
        time.sleep(interval)


if __name__ == "__main__":
    main()
