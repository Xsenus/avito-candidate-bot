from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .conversation import (
    DATE_REMINDER_MESSAGE,
    REMINDER_LEAD,
    ConversationState,
    stop_reminders,
)
from .warehouses import warehouse_prompt_for_city

REMINDER_STEPS = frozenset({"awaiting_warehouse", "awaiting_datetime"})
AVITO_TEXT_LIMIT = 1000


def _enabled(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ReminderConfig:
    enabled: bool = False
    delays_seconds: tuple[int, int, int] = (15 * 60, 60 * 60, 24 * 60 * 60)
    inflight_grace_seconds: int = 60

    @classmethod
    def from_env(cls) -> ReminderConfig:
        return cls(
            enabled=_enabled(os.getenv("FOLLOW_UP_REMINDERS_ENABLED", "false")),
            delays_seconds=(
                max(1, int(os.getenv("FOLLOW_UP_FIRST_DELAY_SECONDS", "900"))),
                max(1, int(os.getenv("FOLLOW_UP_SECOND_DELAY_SECONDS", "3600"))),
                max(1, int(os.getenv("FOLLOW_UP_THIRD_DELAY_SECONDS", "86400"))),
            ),
            inflight_grace_seconds=max(
                5, int(os.getenv("FOLLOW_UP_INFLIGHT_GRACE_SECONDS", "60"))
            ),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def arm_reminders(
    state: ConversationState,
    config: ReminderConfig,
    *,
    now: datetime | None = None,
) -> None:
    stop_reminders(state)
    state.reminder_count = 0
    if (
        not config.enabled
        or state.reminders_stopped
        or state.application_status != "collecting"
        or state.step not in REMINDER_STEPS
    ):
        return
    current = now or utc_now()
    state.reminder_step = state.step
    state.reminder_armed_at = current.isoformat()
    state.reminder_due_at = (
        current + timedelta(seconds=config.delays_seconds[0])
    ).isoformat()


def reminder_is_due(state: ConversationState, *, now: datetime | None = None) -> bool:
    if (
        state.reminders_stopped
        or state.application_status != "collecting"
        or state.step not in REMINDER_STEPS
        or state.reminder_step != state.step
        or not state.reminder_due_at
        or state.reminder_count >= 3
    ):
        return False
    due_at = _parse_timestamp(state.reminder_due_at)
    if due_at is None:
        return False
    return due_at <= (now or utc_now())


def reminder_message(state: ConversationState) -> str:
    if state.step == "awaiting_datetime":
        return DATE_REMINDER_MESSAGE
    if state.step == "awaiting_warehouse":
        prompt = warehouse_prompt_for_city(state.city)
        if not prompt:
            raise ValueError("Для напоминания не найден список складов")
        message = f'{REMINDER_LEAD}\n\n❗️{prompt}\nили "0" если не актуально.'
        if len(message) > AVITO_TEXT_LIMIT:
            raise ValueError("Напоминание со списком складов превышает лимит Avito")
        return message
    raise ValueError(f"Для этапа {state.step!r} напоминание не предусмотрено")


def mark_reminder_inflight(
    state: ConversationState,
    text: str,
    *,
    now: datetime | None = None,
) -> None:
    current = now or utc_now()
    state.reminder_inflight_number = state.reminder_count + 1
    state.reminder_inflight_started_at = current.isoformat()
    state.reminder_inflight_text = text


def finish_reminder(
    state: ConversationState,
    config: ReminderConfig,
    *,
    sent_at: datetime | None = None,
) -> None:
    current = sent_at or utc_now()
    number = state.reminder_inflight_number or (state.reminder_count + 1)
    state.reminder_count = max(state.reminder_count, number)
    state.reminder_inflight_number = None
    state.reminder_inflight_started_at = None
    state.reminder_inflight_text = None
    if state.reminder_count >= len(config.delays_seconds):
        state.reminder_step = None
        state.reminder_due_at = None
        state.reminder_armed_at = None
        return
    state.reminder_due_at = (
        current + timedelta(seconds=config.delays_seconds[state.reminder_count])
    ).isoformat()


def inflight_retry_is_due(
    state: ConversationState,
    config: ReminderConfig,
    *,
    now: datetime | None = None,
) -> bool:
    if not state.reminder_inflight_started_at:
        return False
    started_at = _parse_timestamp(state.reminder_inflight_started_at)
    if started_at is None:
        return True
    return started_at + timedelta(seconds=config.inflight_grace_seconds) <= (
        now or utc_now()
    )


def clear_inflight_for_retry(state: ConversationState) -> None:
    state.reminder_inflight_number = None
    state.reminder_inflight_started_at = None
    state.reminder_inflight_text = None
