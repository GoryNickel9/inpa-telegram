"""Local acquisition and optional private Telegram CLI."""

from __future__ import annotations

import argparse
import json
import math
import signal
import sqlite3
import sys
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from inpa_bot.source.adapter import InpaSitemapAdapter
from inpa_bot.source.http import HttpClient, HttpConfig
from inpa_bot.source.territory import PROVINCES, REGIONS, matches_filter
from inpa_bot.sync.service import SyncConfig, Synchronizer
from inpa_bot.sync.store import SyncBusyError, SyncStore


def emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _positive(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="inPA persistent metadata monitor (M1)"
    )
    result.add_argument("--db", default="var/inpa.sqlite3", help="Local SQLite archive")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("sync", "watch"):
        command = commands.add_parser(name)
        command.add_argument("--interval-seconds", type=_positive, default=3600)
        command.add_argument("--reconcile-seconds", type=_positive, default=86400)
        command.add_argument("--missing-retention-days", type=_positive, default=7)
        command.add_argument(
            "--user-agent",
            required=True,
            help="Descriptive user agent with an operator contact",
        )
    commands.add_parser("status")
    listing = commands.add_parser("list")
    listing.add_argument(
        "--region", action="append", choices=[r.code for r in REGIONS], default=[]
    )
    listing.add_argument(
        "--province", action="append", choices=[p.code for p in PROVINCES], default=[]
    )
    listing.add_argument(
        "--exclude-national",
        action="store_true",
        help="Exclude national notices when a geographic filter is set",
    )
    listing.add_argument(
        "--exclude-unresolved",
        action="store_true",
        help="Exclude unresolved territories when a geographic filter is set",
    )
    listing.add_argument("--limit", type=int, default=50)
    telegram = commands.add_parser("telegram", help="Private bot setup and runtime")
    telegram.add_argument("--env-file", default=".env", help="Local secrets file")
    telegram.add_argument("telegram_action", choices=["configure", "identify", "run"])
    return result


def _watch(sync: Synchronizer, stopped: threading.Event) -> int:
    """Schedule from the last persisted attempt, including across restarts."""
    while not stopped.is_set():
        last = sync.store.status()["last_attempt"]
        if last is not None:
            finished = datetime.fromisoformat(last["finished_at"])
            delay = (
                sync.config.interval_seconds
                - (datetime.now(timezone.utc) - finished).total_seconds()
            )
            if last.get("retry_not_before"):
                retry_delay = (
                    datetime.fromisoformat(last["retry_not_before"])
                    - datetime.now(timezone.utc)
                ).total_seconds()
                delay = max(delay, retry_delay)
            if delay > 0:
                stopped.wait(min(delay, 60))
                continue
        report = sync.run_once(should_stop=stopped.is_set)
        emit(
            {
                "level": "info" if report["complete"] else "warning",
                "event": "sync_attempt",
                **report,
            }
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "list" and args.limit < 1:
        parser().error("--limit must be positive")
    try:
        path = Path(args.db).resolve()
        if args.command in {"status", "list"} and not path.is_file():
            raise FileNotFoundError(f"Archive does not exist: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.command == "telegram":
            from inpa_bot.telegram.cli import telegram_command

            return telegram_command(args, path, emit)
        with SyncStore(path) as store:
            if args.command == "status":
                emit(store.status())
                return 0
            if args.command == "list":
                count = 0
                for snapshot in store.snapshots():
                    if not matches_filter(
                        snapshot.territories,
                        region_codes=args.region,
                        province_codes=args.province,
                        include_national=not args.exclude_national,
                        include_unresolved=not args.exclude_unresolved,
                    ):
                        continue
                    emit(
                        {
                            "notice_id": snapshot.notice_id,
                            "title": snapshot.title,
                            "status": snapshot.status_normalized.value,
                            "closing": snapshot.closing_at.raw
                            if snapshot.closing_at
                            else None,
                            "source_url": snapshot.source_url,
                            "territories": [t.raw for t in snapshot.territories],
                            "territory_note": "Verificare la sede di lavoro nel bando; il filtro usa l'area dichiarata",
                        }
                    )
                    count += 1
                    if count >= args.limit:
                        break
                return 0
            if not args.user_agent.strip():
                raise ValueError("--user-agent cannot be empty")
            config = SyncConfig(
                interval_seconds=args.interval_seconds,
                reconcile_seconds=args.reconcile_seconds,
                missing_retention_seconds=args.missing_retention_days * 86400,
            )
            source = InpaSitemapAdapter(
                HttpClient(HttpConfig(user_agent=args.user_agent))
            )
            sync = Synchronizer(source, store, config)
            stopped = threading.Event()
            previous = {
                sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                for sig in previous:
                    signal.signal(sig, lambda signum, frame: stopped.set())
                if args.command == "watch":
                    return _watch(sync, stopped)
                report = sync.run_once(should_stop=stopped.is_set)
                emit(
                    {
                        "level": "info" if report["complete"] else "warning",
                        "event": "sync_attempt",
                        **report,
                    }
                )
                return 130 if stopped.is_set() else 0 if report["complete"] else 1
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except SyncBusyError as exc:
        emit({"level": "error", "type": type(exc).__name__, "message": str(exc)})
        return 3
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        emit({"level": "error", "type": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
