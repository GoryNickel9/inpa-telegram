"""Offline M1 benchmark on synthetic notices; never connects to inPA."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from inpa_bot.source.models import (  # noqa: E402
    NoticeSnapshot,
    NoticeStub,
    Provenance,
    SearchFilters,
    SearchPage,
    SourceDateTime,
)
from inpa_bot.sync.service import Synchronizer  # noqa: E402
from inpa_bot.sync.store import SyncStore  # noqa: E402


class SyntheticSource:
    name = "offline-benchmark"
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)

    def __init__(self, count: int) -> None:
        self.count = count
        self.fetched = 0

    def refresh(self) -> None:
        pass

    def search(
        self, filters: SearchFilters, page_cursor: str | None = None
    ) -> SearchPage:
        return SearchPage(
            tuple(
                NoticeStub(
                    str(i),
                    f"https://example.invalid/{i}",
                    Provenance("synthetic", self.now),
                    SourceDateTime("2026-10-08", date_only=self.now.date()),
                )
                for i in range(self.count)
            ),
            True,
            declared_total=self.count,
        )

    def fetch_notice(self, notice_id: str) -> NoticeSnapshot:
        self.fetched += 1
        return NoticeSnapshot(
            notice_id,
            f"https://example.invalid/{notice_id}",
            self.now,
            title=f"Synthetic notice {notice_id}",
            description="x" * 1024,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--out", type=Path, help="Save the measured JSON report")
    args = parser.parse_args()
    if not 1 <= args.records <= 10000:
        parser.error("--records must be between 1 and 10000")
    root = Path(__file__).resolve().parents[1]
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    code_hash = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        code_hash.update(path.relative_to(root).as_posix().encode())
        code_hash.update(path.read_bytes())
    start = perf_counter()
    with tempfile.TemporaryDirectory(prefix="inpa-m1-bench-") as directory:
        path = Path(directory) / "bench.sqlite3"
        source = SyntheticSource(args.records)
        with SyncStore(path) as store:
            sync = Synchronizer(source, store, clock=lambda: source.now)
            t0 = perf_counter()
            initial = sync.run_once()
            initial_seconds = perf_counter() - t0
            t0 = perf_counter()
            unchanged = sync.run_once()
            unchanged_seconds = perf_counter() - t0
            assert initial["complete"] and unchanged["complete"]
            assert initial["fetched"] == args.records and unchanged["fetched"] == 0
            assert store.status()["events"] == args.records
        t0 = perf_counter()
        with SyncStore(path) as store:
            reopened = store.status()
        reopen_seconds = perf_counter() - t0
        assert reopened["versions"] == args.records
        disk_bytes = sum(p.stat().st_size for p in Path(directory).iterdir())
    output = json.dumps(
        {
            "dataset": "synthetic, 1024 description bytes per notice; no network",
            "records": args.records,
            "threads": 1,
            "seed": None,
            "sqlite": sqlite3.sqlite_version,
            "os": platform.platform(),
            "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "python": sys.version,
            "commit": git.stdout.strip(),
            "source_sha256": code_hash.hexdigest(),
            "initial_seconds": initial_seconds,
            "unchanged_seconds": unchanged_seconds,
            "reopen_status_seconds": reopen_seconds,
            "disk_bytes_after_close": disk_bytes,
            "total_seconds": perf_counter() - start,
            "limitations": "Single run, warm OS cache, no network latency; no RAM measurement",
        },
        indent=2,
    )
    if args.out:
        args.out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
