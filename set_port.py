from __future__ import annotations

import re
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def find_free_port(start: int = 8000) -> int:
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


def update_env(port: int) -> None:
    if not ENV_PATH.exists():
        return

    text = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"^AVITO_WEBHOOK_URL=.*$", text, flags=re.MULTILINE):
        return

    updated = text.rstrip() + "\nAVITO_WEBHOOK_URL=https://your-domain.example/webhook/avito\n"
    ENV_PATH.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    port = find_free_port()
    update_env(port)
    print(port)
