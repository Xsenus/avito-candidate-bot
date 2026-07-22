from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

import requests


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, user_id: str, base_url: str = "https://api.avito.ru") -> None:
        self.client_id = self._normalize_value(client_id)
        self.client_secret = self._normalize_value(client_secret)
        self.user_id = self._normalize_user_id(user_id)
        self.base_url = self._normalize_base_url(base_url)
        self._access_token: str | None = None
        self._access_token_expires_at: float | None = None

    @staticmethod
    def _normalize_value(value: str) -> str:
        return (value or "").strip()

    @staticmethod
    def _normalize_user_id(value: str) -> str:
        return (value or "").replace(" ", "").replace("\u00a0", "").strip()

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        value = (base_url or "").strip()
        if not value:
            return "https://api.avito.ru"

        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

        return "https://api.avito.ru"

    def get_access_token(self) -> str:
        if (
            self._access_token
            and (
                self._access_token_expires_at is None
                or time.monotonic() < self._access_token_expires_at
            )
        ):
            return self._access_token

        response = self._request_with_retries(
            requests.post,
            f"{self.base_url}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": os.getenv("AVITO_SCOPES", "messenger:read messenger:write"),
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        error_code = payload.get("error")
        if error_code:
            error_description = payload.get("error_description") or "unknown Avito error"
            raise RuntimeError(f"Avito auth failed: {error_code} ({error_description})")

        self._access_token = payload.get("access_token")
        if not self._access_token:
            raise RuntimeError("Avito token was not returned")
        try:
            expires_in = float(payload.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            margin = max(
                0, int(os.getenv("AVITO_TOKEN_REFRESH_MARGIN_SECONDS", "60"))
            )
            self._access_token_expires_at = time.monotonic() + max(
                1, expires_in - margin
            )
        else:
            self._access_token_expires_at = None
        return self._access_token

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("AVITO_CLIENT_ID and AVITO_CLIENT_SECRET must be configured")

        if not self.user_id:
            raise RuntimeError("AVITO_USER_ID must be configured")
        if not (text or "").strip():
            raise ValueError("Avito message cannot be empty")
        if len(text) > 1000:
            raise ValueError("Avito message exceeds 1000 characters")
        url = f"{self.base_url}/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages"
        response = self._authorized_request(
            "POST",
            url,
            json={"type": "text", "message": {"text": text}},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def register_webhook(self, url: str) -> dict[str, Any]:
        response = self._authorized_request(
            "POST",
            f"{self.base_url}/messenger/v3/webhook",
            json={"url": url},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_chats(self, unread_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        response = self._authorized_request(
            "GET",
            f"{self.base_url}/messenger/v2/accounts/{self.user_id}/chats",
            params={"unread_only": str(unread_only).lower(), "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("chats", [])

    def get_messages(
        self, chat_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return recent chat messages in the order provided by Avito (newest first)."""
        if not self.user_id:
            raise RuntimeError("AVITO_USER_ID must be configured")
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            raise ValueError("Avito chat_id cannot be empty")

        response = self._authorized_request(
            "GET",
            (
                f"{self.base_url}/messenger/v3/accounts/{self.user_id}"
                f"/chats/{normalized_chat_id}/messages/"
            ),
            params={"limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset))},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        messages = payload.get("messages", [])
        return messages if isinstance(messages, list) else []

    def _authorized_request(self, method: str, url: str, **kwargs):
        base_headers = dict(kwargs.pop("headers", {}))
        max_attempts = max(2, int(os.getenv("AVITO_HTTP_MAX_ATTEMPTS", "3")))
        for attempt in range(max_attempts):
            token = self.get_access_token()
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = requests.request(method, url, headers=headers, **kwargs)
            except requests.RequestException:
                if attempt + 1 >= max_attempts:
                    raise
                self._sleep_before_retry(attempt)
                continue
            if response.status_code == 401:
                self._access_token = None
                self._access_token_expires_at = None
                if attempt + 1 >= max_attempts:
                    return response
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            if attempt + 1 >= max_attempts:
                return response
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("Retry-After")
            self._sleep_before_retry(attempt, retry_after)
        raise RuntimeError("Avito request retry failed")

    def _request_with_retries(self, request, *args, **kwargs):
        max_attempts = max(1, int(os.getenv("AVITO_HTTP_MAX_ATTEMPTS", "3")))
        for attempt in range(max_attempts):
            try:
                response = request(*args, **kwargs)
            except requests.RequestException:
                if attempt + 1 >= max_attempts:
                    raise
                self._sleep_before_retry(attempt)
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            if attempt + 1 >= max_attempts:
                return response
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("Retry-After")
            self._sleep_before_retry(attempt, retry_after)
        raise RuntimeError("Avito request retry failed")

    @staticmethod
    def _sleep_before_retry(attempt: int, retry_after: str | None = None) -> None:
        try:
            delay = float(retry_after) if retry_after is not None else None
        except ValueError:
            delay = None
        if delay is None:
            base = max(
                0.0, float(os.getenv("AVITO_HTTP_RETRY_BASE_SECONDS", "1"))
            )
            delay = base * (2**attempt)
        time.sleep(max(0.0, delay))
