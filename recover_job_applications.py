from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from avito_bot.avito_client import AvitoClient
from avito_bot.storage import SQLiteStateStore
from avito_bot.workflow import CandidateWorkflow
from avito_bot.yandex_form import YandexFormSubmitter
from poller import iter_unanswered_job_applications, process_chat_message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover recent Avito vacancy applications with no outgoing reply."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Send replies. Without this flag the command is read-only.",
    )
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--delay", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(Path(__file__).resolve().parent / ".env")
    client = AvitoClient(
        client_id=os.environ["AVITO_CLIENT_ID"],
        client_secret=os.environ["AVITO_CLIENT_SECRET"],
        user_id=os.environ["AVITO_USER_ID"],
        base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
    )
    state_path = os.getenv(
        "STATE_DB_PATH", str(Path(__file__).resolve().parent / "data" / "bot.sqlite3")
    )
    store = SQLiteStateStore(state_path)
    targets = list(
        iter_unanswered_job_applications(
            client,
            client.get_chats(unread_only=False, limit=100),
            store,
            max_age_hours=args.hours,
        )
    )
    print(f"Recovery candidates: {len(targets)}")
    for chat_id, _, _, message_id, city, item_id in targets:
        print(
            f"candidate chat_id={chat_id} message_id={message_id} "
            f"city={city!r} item_id={item_id!r}"
        )
    if not args.apply:
        print("Dry run only; use --apply to send replies.")
        store.close()
        return 0

    workflow = CandidateWorkflow.from_env(YandexFormSubmitter.from_env())
    failures: list[tuple[str, str]] = []
    for values in targets:
        chat_id = values[0]
        try:
            process_chat_message(client, workflow, store, *values)
            print(f"recovered chat_id={chat_id}")
        except Exception as exc:
            failures.append((chat_id, str(exc)))
            print(f"recovery error chat_id={chat_id}: {exc}")
        time.sleep(max(0.0, args.delay))

    # Initial replies schedule a second message five seconds later. Keep this
    # one-shot process alive long enough for the final scheduled send.
    time.sleep(6)
    store.close()
    print(f"Recovery complete: ok={len(targets) - len(failures)} failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
