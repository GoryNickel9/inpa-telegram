"""Offline M2 benchmark: archive browsing and persistent delivery bookkeeping."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from inpa_bot.source.detail import build_detail_url  # noqa: E402
from inpa_bot.source.models import NoticeSnapshot, NoticeStatus  # noqa: E402
from inpa_bot.telegram.commands import Commands, Incoming  # noqa: E402
from inpa_bot.telegram.store import BotStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not 1 <= args.records <= 10000:
        parser.error("--records must be 1..10000")
    root = Path(__file__).resolve().parents[1]
    code_hash = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        code_hash.update(path.relative_to(root).as_posix().encode())
        code_hash.update(path.read_bytes())
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="inpa-m2-bench-") as directory:
        path = Path(directory) / "benchmark.sqlite3"
        with BotStore(path) as store:
            for i in range(args.records):
                notice_id = f"{i:032x}"
                store.record(
                    NoticeSnapshot(
                        notice_id,
                        build_detail_url(notice_id),
                        now,
                        status_normalized=NoticeStatus.OPEN,
                        title=f"Synthetic bando {i}",
                    )
                )
            store.configure_admins(frozenset({11}), 4, now)
            t0 = perf_counter()
            Commands(store).handle(Incoming(1, 11, 11, True, "/start"), now)
            Commands(store).handle(Incoming(2, 11, 11, True, "/concorsi"), now)
            page_seconds = perf_counter() - t0
            t0 = perf_counter()
            deliveries = 0
            while (delivery := store.claim(now)) is not None:
                store.finish_delivery(
                    delivery["id"], "sent", now, message_id=deliveries + 1
                )
                deliveries += 1
            delivery_seconds = perf_counter() - t0
            assert deliveries <= 8 and store.offset() == 2
        disk_bytes = sum(p.stat().st_size for p in Path(directory).iterdir())
    output = json.dumps(
        {
            "dataset": "synthetic open notices, no network",
            "records": args.records,
            "threads": 1,
            "python": sys.version,
            "os": platform.platform(),
            "cpu": platform.processor(),
            "source_sha256": code_hash.hexdigest(),
            "first_page_and_outbox_seconds": page_seconds,
            "queued_replies": deliveries,
            "delivery_bookkeeping_seconds": delivery_seconds,
            "disk_bytes_after_close": disk_bytes,
            "limitations": "Single run, warm OS cache, no Telegram or inPA requests, no RAM measurement",
        },
        indent=2,
    )
    if args.out:
        args.out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
