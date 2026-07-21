from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from dataclasses import asdict, fields
from pathlib import Path

from .conversation import ConversationState


class SQLiteStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        self._connection.commit()

    def load(self, chat_id: str) -> ConversationState:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM conversations WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if not row:
            return ConversationState()
        payload = json.loads(row[0])
        allowed = {item.name for item in fields(ConversationState)}
        return ConversationState(**{key: value for key, value in payload.items() if key in allowed})

    def save(self, chat_id: str, state: ConversationState) -> None:
        payload = json.dumps(asdict(state), ensure_ascii=False)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO conversations(chat_id, state_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, payload),
            )
            self._connection.commit()

    def is_processed(self, chat_id: str, message_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return row is not None

    def has_seen_chat(self, chat_id: str) -> bool:
        """Return whether state or a message cursor already exists for this chat."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM conversations WHERE chat_id = ?
                UNION ALL
                SELECT 1 FROM processed_messages WHERE chat_id = ?
                LIMIT 1
                """,
                (chat_id, chat_id),
            ).fetchone()
        return row is not None

    def mark_processed(self, chat_id: str, message_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO processed_messages(chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
            self._connection.commit()

    def pending(self) -> list[tuple[str, ConversationState]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT chat_id, state_json FROM conversations"
            ).fetchall()
        pending_statuses = {"pending", "submitted"}
        result = []
        for chat_id, raw in rows:
            payload = json.loads(raw)
            retry_at = payload.get("next_retry_at")
            retry_due = not retry_at or datetime.fromisoformat(retry_at) <= datetime.now(timezone.utc)
            if payload.get("application_status") in pending_statuses and retry_due:
                allowed = {item.name for item in fields(ConversationState)}
                state = ConversationState(
                    **{key: value for key, value in payload.items() if key in allowed}
                )
                result.append((chat_id, state))
        return result

    def all_conversations(self) -> list[tuple[str, ConversationState]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT chat_id, state_json FROM conversations"
            ).fetchall()
        allowed = {item.name for item in fields(ConversationState)}
        return [
            (
                chat_id,
                ConversationState(
                    **{
                        key: value
                        for key, value in json.loads(raw).items()
                        if key in allowed
                    }
                ),
            )
            for chat_id, raw in rows
        ]

    def quarantine_interrupted_submissions(self) -> int:
        """Prevent an automatic duplicate after a crash during form submission."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT chat_id, state_json FROM conversations"
            ).fetchall()
            changed = 0
            for chat_id, raw in rows:
                payload = json.loads(raw)
                if payload.get("application_status") != "submitting":
                    continue
                payload["application_status"] = "uncertain"
                payload["last_error"] = (
                    "Процесс был остановлен во время отправки формы; "
                    "перед повтором проверьте заявку вручную"
                )
                payload["next_retry_at"] = None
                self._connection.execute(
                    """
                    UPDATE conversations
                    SET state_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                    """,
                    (json.dumps(payload, ensure_ascii=False), chat_id),
                )
                changed += 1
            if changed:
                self._connection.commit()
        return changed

    def get_metadata(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
