from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests


class HttpClient(Protocol):
    def post(self, url: str, **kwargs: Any): ...


class TelegramDeliveryError(RuntimeError):
    pass


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


@dataclass(frozen=True)
class TelegramAlertNotifier:
    bot_token: str
    chat_id: str
    proxy_url: str | None = None
    timeout_seconds: int = 10
    http: HttpClient = requests

    @classmethod
    def from_env(cls) -> "TelegramAlertNotifier":
        proxy = os.getenv("TELEGRAM_ALERT_PROXY_URL", "").strip() or None
        if proxy and urlsplit(proxy).scheme.casefold() not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(
                "TELEGRAM_ALERT_PROXY_URL должен использовать http, https, "
                "socks4, socks5 или socks5h"
            )
        return cls(
            bot_token=os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip(),
            proxy_url=proxy,
            timeout_seconds=max(
                1, int(os.getenv("TELEGRAM_ALERT_TIMEOUT_SECONDS", "10"))
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    @property
    def partially_configured(self) -> bool:
        return bool(self.bot_token) != bool(self.chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        text = (message or "").strip()
        if not text:
            raise ValueError("Telegram alert message cannot be empty")

        kwargs: dict[str, Any] = {
            "json": {
                "chat_id": self.chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            "timeout": self.timeout_seconds,
        }
        if self.proxy_url:
            kwargs["proxies"] = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }

        try:
            response = self.http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                **kwargs,
            )
        except Exception as exc:
            # Do not include the original exception: request errors may contain
            # the URL and therefore the Telegram bot token.
            raise TelegramDeliveryError(
                f"сетевая ошибка {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise TelegramDeliveryError(
                f"Telegram API вернул HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise TelegramDeliveryError("Telegram API вернул некорректный ответ") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramDeliveryError("Telegram API отклонил уведомление")


def emit_alert(message: str) -> None:
    """Always log an alert and additionally deliver it to Telegram when configured."""
    print(f"ALERT {message}")
    try:
        notifier = TelegramAlertNotifier.from_env()
    except (TypeError, ValueError):
        print("ALERT telegram delivery skipped: invalid notifier configuration")
        return
    if notifier.partially_configured:
        print(
            "ALERT telegram delivery skipped: set both "
            "TELEGRAM_ALERT_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID"
        )
        return
    if not notifier.enabled:
        return
    try:
        notifier.send(message)
    except TelegramDeliveryError as exc:
        print(f"ALERT telegram delivery failed: {exc}")
