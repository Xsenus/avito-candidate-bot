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
from .warehouses import warehouse_group_for_city, warehouse_prompt_for_city

REMINDER_STEPS = frozenset({"awaiting_warehouse", "awaiting_datetime"})
AVITO_TEXT_LIMIT = 1000


def _enabled(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ReminderConfig:
    enabled: bool = False
    # Absolute offsets from the unanswered bot question, not from each reminder.
    delays_seconds: tuple[int, int, int] = (5 * 60, 12 * 60 * 60, 24 * 60 * 60)
    inflight_grace_seconds: int = 60
    fourth_delay_seconds: int = 48 * 60 * 60

    def __post_init__(self) -> None:
        if len(self.delays_seconds) != 3 or not (
            0 < self.delays_seconds[0] < self.delays_seconds[1] < self.delays_seconds[2]
        ):
            raise ValueError(
                "Reminder offsets must be three strictly increasing positive values"
            )
        if self.fourth_delay_seconds <= self.delays_seconds[2]:
            raise ValueError("The fourth reminder offset must be later than the third")

    @property
    def all_offsets(self) -> tuple[int, ...]:
        return (*self.delays_seconds, self.fourth_delay_seconds)

    @classmethod
    def from_env(cls) -> ReminderConfig:
        return cls(
            enabled=_enabled(os.getenv("FOLLOW_UP_REMINDERS_ENABLED", "false")),
            delays_seconds=(
                int(os.getenv("FOLLOW_UP_FIRST_DELAY_SECONDS", "300")),
                int(os.getenv("FOLLOW_UP_SECOND_DELAY_SECONDS", "43200")),
                max(1, int(os.getenv("FOLLOW_UP_THIRD_DELAY_SECONDS", "86400"))),
            ),
            inflight_grace_seconds=max(
                5, int(os.getenv("FOLLOW_UP_INFLIGHT_GRACE_SECONDS", "60"))
            ),
            fourth_delay_seconds=int(os.getenv("FOLLOW_UP_FOURTH_DELAY_SECONDS", "172800")),
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
    state.reminder_policy_version = 2
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
    waiting_for_final_offer = (
        state.step == "awaiting_reactivation"
        and state.reactivation_sent
        and state.reminder_count == 3
    )
    if (
        state.reminders_stopped
        or state.application_status != "collecting"
        or (state.step not in REMINDER_STEPS and not waiting_for_final_offer)
        or state.reminder_step != state.step
        or not state.reminder_due_at
        # A skipped 24h milestone may leave count=3 on the original question
        # while the 48h send is being retried. It is exhausted only at four.
        or (state.reminder_count >= (2 if state.reactivation_sent else 4) and not waiting_for_final_offer)
    ):
        return False
    due_at = _parse_timestamp(state.reminder_due_at)
    if due_at is None:
        return False
    return due_at <= (now or utc_now())


def reminder_message(state: ConversationState) -> str:
    if state.step == "awaiting_reactivation" or (state.reminder_count >= 2 and not state.reactivation_sent):
        return reactivation_message(state)
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


def reactivation_message(state: ConversationState) -> str:
    if not state.city:
        raise ValueError("Неизвестен город для повторного предложения вакансии")
    income = "6000" if warehouse_group_for_city(state.city) else "4500"
    return (
        "Здравствуйте.\n"
        "Хотели уточнить, актуальна ли для вас еще вакансия водителя Яндекс Маркета.\n"
        "Напомню условия:\n"
        "✅ Выдаем авто для работы - Форд транзит\n"
        f"✅ Доход от {income} за смену, до 240 000 ₽ в месяц;\n"
        "✅ Бензин, парковки и обслуживание — за счет компании;\n"
        "✅ График можно подобрать индивидуально;\n"
        "Если вакансия интересна — ответьте «Да», и я помогу подобрать ближайший "
        "склад и записаться на стажировку."
    )


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
    was_reactivation = bool(
        state.reminder_inflight_text
        and state.reminder_inflight_text.startswith("Здравствуйте.\nХотели уточнить,")
    )
    state.reminder_count = max(state.reminder_count, number)
    state.reminder_inflight_number = None
    state.reminder_inflight_started_at = None
    state.reminder_inflight_text = None
    if was_reactivation:
        state.reactivation_sent = True
        state.step = "awaiting_reactivation"
        if state.reminder_count < 4:
            anchor = _parse_timestamp(state.reminder_armed_at or "")
            if anchor is None:
                stop_reminders(state, permanently=True)
                return
            state.reminder_step = state.step
            state.reminder_due_at = max(
                anchor + timedelta(seconds=config.fourth_delay_seconds),
                current + timedelta(seconds=1),
            ).isoformat()
            return
    limit = 2 if state.reactivation_sent else len(config.delays_seconds)
    if state.reminder_count >= limit:
        state.reminder_step = None
        state.reminder_due_at = None
        state.reminder_armed_at = None
        return
    anchor = _parse_timestamp(state.reminder_armed_at or "")
    if anchor is None:
        stop_reminders(state, permanently=True)
        return
    next_at = anchor + timedelta(seconds=config.delays_seconds[state.reminder_count])
    # Do not burst overdue reminders after a long outage. If the final offset
    # has passed, the next poll sends only the final offer, never a backlog.
    if next_at <= current and not state.reactivation_sent:
        state.reminder_count = 2
        next_at = anchor + timedelta(seconds=config.delays_seconds[2])
    state.reminder_due_at = max(next_at, current + timedelta(seconds=1)).isoformat()


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
