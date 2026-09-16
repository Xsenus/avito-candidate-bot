from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from .conversation import ConversationState


class SQLiteStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection_finalizer = weakref.finalize(self, self._connection.close)
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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_message_cursors (
                chat_id TEXT PRIMARY KEY,
                last_created REAL NOT NULL,
                last_message_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_outgoing_messages (
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_fingerprint TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                original_updated_at TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        return ConversationState(
            **{key: value for key, value in payload.items() if key in allowed}
        )

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

    def mark_bot_outgoing(self, chat_id: str, message_id: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO bot_outgoing_messages(chat_id, message_id)
                VALUES (?, ?)
                """,
                (chat_id, message_id),
            )
            self._connection.commit()

    def is_bot_outgoing(self, chat_id: str, message_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM bot_outgoing_messages
                WHERE chat_id = ? AND message_id = ?
                """,
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

    def get_message_cursor(self, chat_id: str) -> tuple[float, str] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_created, last_message_id
                FROM chat_message_cursors
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return (float(row[0]), str(row[1])) if row else None

    def mark_message_seen(
        self, chat_id: str, message_id: str, created: float | None
    ) -> None:
        """Atomically mark a message and move the per-chat high-water mark."""
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO processed_messages(chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
            if created is not None:
                current = self._connection.execute(
                    """
                    SELECT last_created, last_message_id
                    FROM chat_message_cursors
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
                candidate = (float(created), message_id)
                if current is None or candidate > (float(current[0]), str(current[1])):
                    self._connection.execute(
                        """
                        INSERT INTO chat_message_cursors(
                            chat_id, last_created, last_message_id, updated_at
                        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            last_created = excluded.last_created,
                            last_message_id = excluded.last_message_id,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (chat_id, float(created), message_id),
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
            retry_due = not retry_at or datetime.fromisoformat(
                retry_at
            ) <= datetime.now(timezone.utc)
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

    def bind_account(self, account_fingerprint: str) -> tuple[bool, int]:
        """Bind state to one Avito account and archive data after a switch.

        The fingerprint must be a one-way digest rather than a credential or
        raw account ID. The first binding preserves existing state for safe
        upgrades. A later mismatch archives all conversations and resets every
        account-local cursor so the new account starts without touching chats
        from the previous profile.
        """
        fingerprint = str(account_fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("Account fingerprint is required")

        binding_key = "avito_account_fingerprint_v1"
        account_metadata = (
            "message_history_cursor_initialized_v2",
            "legacy_completed_chats_migrated_v1",
            "unanswered_recovery_started_at_v1",
            "manual_takeover_started_at_v1",
            "reminder_manual_stop_started_at_v1",
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (binding_key,)
            ).fetchone()
            previous = str(row[0]).strip() if row else None
            if previous is None:
                self._connection.execute(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    (binding_key, fingerprint),
                )
                self._connection.commit()
                return False, 0
            if previous == fingerprint:
                return False, 0

            archived = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0]
            )
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO archived_conversations(
                        account_fingerprint,
                        chat_id,
                        state_json,
                        original_updated_at
                    )
                    SELECT ?, chat_id, state_json, updated_at
                    FROM conversations
                    """,
                    (previous,),
                )
                self._connection.execute("DELETE FROM conversations")
                self._connection.execute("DELETE FROM processed_messages")
                self._connection.execute("DELETE FROM chat_message_cursors")
                self._connection.execute("DELETE FROM bot_outgoing_messages")
                placeholders = ",".join("?" for _ in account_metadata)
                self._connection.execute(
                    f"DELETE FROM metadata WHERE key IN ({placeholders})",
                    account_metadata,
                )
                self._connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = ?",
                    (fingerprint, binding_key),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return True, archived

    def close(self) -> None:
        with self._lock:
            self._connection_finalizer()
