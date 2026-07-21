from __future__ import annotations

import os
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
        if self._access_token:
            return self._access_token

        response = requests.post(
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
        return self._access_token

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("AVITO_CLIENT_ID and AVITO_CLIENT_SECRET must be configured")

        token = self.get_access_token()
        url = f"{self.base_url}/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "text", "message": {"text": text}},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def register_webhook(self, url: str) -> dict[str, Any]:
        token = self.get_access_token()
        response = requests.post(
            f"{self.base_url}/messenger/v3/webhook",
            headers={"Authorization": f"Bearer {token}"},
            json={"url": url},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_chats(self, unread_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        token = self.get_access_token()
        response = requests.get(
            f"{self.base_url}/messenger/v2/accounts/{self.user_id}/chats",
            headers={"Authorization": f"Bearer {token}"},
            params={"unread_only": str(unread_only).lower(), "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("chats", [])

    def submit_to_yandex_form(self, form_url: str, data: dict[str, str]) -> dict[str, Any]:
        response = requests.post(form_url, data=data, timeout=30)
        response.raise_for_status()
        return {"status": response.status_code, "url": form_url}
