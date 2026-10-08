"""Durable work plans and atomic snapshot/progress commits.

M0's schema remains version 1. M1 adds independently versioned tables;
opening an M0 database requires no rewriting of existing snapshots.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from inpa_bot.source.models import NoticeSnapshot
from inpa_bot.source.snapshot import NoticeDiff, SnapshotStore, _utc_iso

from .types import SyncReport, SyncStatus


class SyncBusyError(RuntimeError):
    """Another synchronizer owns this archive."""


class SyncStore(SnapshotStore):
    """One archive for snapshots, pending jobs, checkpoints and change events."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(path)
        self._sync_lock = threading.Lock()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='m1_schema_version'"
                ).fetchone()
                if row is not None and row[0] != "1":
                    raise RuntimeError(f"Unsupported M1 schema version: {row[0]}")
                for statement in (
                    """CREATE TABLE IF NOT EXISTS sync_batches (
                        id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                        started_at TEXT NOT NULL, finished_at TEXT,
                        phase TEXT NOT NULL CHECK(phase IN ('discovering','fetching','complete')),
                        baseline INTEGER NOT NULL, discovered INTEGER NOT NULL DEFAULT 0,
                        pages INTEGER NOT NULL DEFAULT 0)""",
                    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch
                        ON sync_batches((1)) WHERE phase != 'complete'""",
                    """CREATE TABLE IF NOT EXISTS sync_jobs (
                        batch_id INTEGER NOT NULL REFERENCES sync_batches(id),
                        notice_id TEXT NOT NULL, lastmod_key TEXT,
                        in_catalogue INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                        error TEXT, PRIMARY KEY(batch_id, notice_id))""",
                    """CREATE TABLE IF NOT EXISTS sync_tracking (
                        notice_id TEXT PRIMARY KEY, lastmod_key TEXT,
                        fetched_at TEXT, missing_since TEXT)""",
                    """CREATE TABLE IF NOT EXISTS sync_events (
                        id INTEGER PRIMARY KEY, notice_id TEXT NOT NULL,
                        version_no INTEGER NOT NULL, batch_id INTEGER NOT NULL
                            REFERENCES sync_batches(id),
                        kind TEXT NOT NULL CHECK(kind IN ('baseline','new','changed')),
                        observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                        UNIQUE(notice_id, version_no))""",
                    """CREATE TABLE IF NOT EXISTS sync_attempts (
                        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL
                            REFERENCES sync_batches(id),
                        finished_at TEXT NOT NULL, payload TEXT NOT NULL)""",
                ):
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('m1_schema_version','1')"
                )
        except BaseException:
            self.close()
            raise

    @contextmanager
    def synchronizing(self) -> Iterator[None]:
        """OS advisory lock, released even after process termination.

        The lock file must remain on disk: deleting it can split the lock
        between two inodes. Network filesystems are outside the M1 target.
        """
        if not self._sync_lock.acquire(blocking=False):
            raise SyncBusyError("Synchronization already running")
        handle = None
        acquired = False
        try:
            if self.path != ":memory:":
                lock_path = str(Path(self.path).resolve()) + ".sync.lock"
                handle = open(lock_path, "a+b")
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise SyncBusyError(
                        "Another process is synchronizing this archive"
                    ) from exc
                acquired = True
            yield
        finally:
            if handle is not None:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self._sync_lock.release()

    def begin(self, source: str, now: datetime) -> sqlite3.Row:
        with self._write() as conn:
            active = conn.execute(
                "SELECT * FROM sync_batches WHERE phase != 'complete'"
            ).fetchone()
            stored_source = conn.execute(
                "SELECT source FROM sync_batches LIMIT 1"
            ).fetchone()
            if stored_source is not None and stored_source[0] != source:
                raise ValueError(
                    "Use a separate archive for a different source adapter"
                )
            if active is not None:
                return active
            baseline = not conn.execute(
                "SELECT 1 FROM sync_batches WHERE phase='complete' LIMIT 1"
            ).fetchone()
            cursor = conn.execute(
                "INSERT INTO sync_batches(source,started_at,phase,baseline) VALUES(?,?,'discovering',?)",
                (source, _utc_iso(now), int(baseline)),
            )
            return conn.execute(
                "SELECT * FROM sync_batches WHERE id=?", (cursor.lastrowid,)
            ).fetchone()

    def tracking(self) -> dict[str, sqlite3.Row]:
        with self._read() as conn:
            return {
                row["notice_id"]: row
                for row in conn.execute("SELECT * FROM sync_tracking")
            }

    def import_existing(self) -> None:
        """M0 snapshots enter tracking and get a fresh read on the next plan."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sync_tracking(notice_id) SELECT notice_id FROM notices"
            )

    def plan(
        self,
        batch_id: int,
        catalogue: dict[str, str | None],
        jobs: Sequence[tuple[str, str | None, bool]],
        pages: int,
        now: datetime,
    ) -> None:
        with self._write() as conn:
            # Only a fully validated catalogue can alter absence tracking.
            conn.execute(
                "UPDATE sync_tracking SET missing_since=COALESCE(missing_since,?)",
                (_utc_iso(now),),
            )
            conn.executemany(
                "INSERT INTO sync_tracking(notice_id,missing_since) VALUES(?,NULL) "
                "ON CONFLICT(notice_id) DO UPDATE SET missing_since=NULL",
                [(notice_id,) for notice_id in catalogue],
            )
            conn.executemany(
                "UPDATE notices SET last_seen_at=MAX(last_seen_at,?) WHERE notice_id=?",
                [(_utc_iso(now), notice_id) for notice_id in catalogue],
            )
            conn.executemany(
                "INSERT INTO sync_jobs(batch_id,notice_id,lastmod_key,in_catalogue) VALUES(?,?,?,?)",
                [
                    (batch_id, notice_id, key, int(present))
                    for notice_id, key, present in jobs
                ],
            )
            conn.execute(
                "UPDATE sync_batches SET phase='fetching',discovered=?,pages=? WHERE id=?",
                (len(catalogue), pages, batch_id),
            )

    def pending(self, batch_id: int) -> list[sqlite3.Row]:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM sync_jobs WHERE batch_id=? AND done=0 ORDER BY notice_id",
                (batch_id,),
            ).fetchall()

    def commit_notice(
        self,
        job: sqlite3.Row,
        snapshot: NoticeSnapshot,
        baseline: bool,
        fetched_at: datetime,
    ) -> NoticeDiff:
        """Snapshot, event and completed job either all commit or all roll back."""
        with self._write() as conn:
            current = conn.execute(
                "SELECT done FROM sync_jobs WHERE batch_id=? AND notice_id=?",
                (job["batch_id"], job["notice_id"]),
            ).fetchone()
            if current is None or current[0]:
                raise RuntimeError("Job missing or already committed")
            if snapshot.notice_id != job["notice_id"] or snapshot.has_blocking_anomaly:
                raise ValueError("Invalid snapshot for synchronization job")
            diff = self._record(conn, snapshot)
            if diff.significant:
                version = conn.execute(
                    "SELECT MAX(version_no) FROM notice_versions WHERE notice_id=?",
                    (snapshot.notice_id,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO sync_events(notice_id,version_no,batch_id,kind,observed_at,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        snapshot.notice_id,
                        version,
                        job["batch_id"],
                        "baseline" if baseline else "new" if diff.is_new else "changed",
                        _utc_iso(snapshot.observed_at),
                        json.dumps(asdict(diff), ensure_ascii=False),
                    ),
                )
            conn.execute(
                "UPDATE sync_tracking SET fetched_at=?,lastmod_key=? WHERE notice_id=?",
                (_utc_iso(fetched_at), job["lastmod_key"], snapshot.notice_id),
            )
            conn.execute(
                "UPDATE sync_jobs SET done=1,error=NULL WHERE batch_id=? AND notice_id=?",
                (job["batch_id"], snapshot.notice_id),
            )
            return diff

    def job_error(self, job: sqlite3.Row, message: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE sync_jobs SET error=? WHERE batch_id=? AND notice_id=?",
                (message, job["batch_id"], job["notice_id"]),
            )

    def finish_attempt(self, batch_id: int, report: SyncReport, now: datetime) -> None:
        with self._write() as conn:
            if report["complete"]:
                if conn.execute(
                    "SELECT 1 FROM sync_jobs WHERE batch_id=? AND done=0", (batch_id,)
                ).fetchone():
                    raise RuntimeError("Cannot complete a batch with pending jobs")
                conn.execute(
                    "UPDATE sync_batches SET phase='complete',finished_at=? "
                    "WHERE id=? AND phase='fetching'",
                    (_utc_iso(now), batch_id),
                )
            conn.execute(
                "INSERT INTO sync_attempts(batch_id,finished_at,payload) VALUES(?,?,?)",
                (batch_id, _utc_iso(now), json.dumps(report, ensure_ascii=False)),
            )

    def status(self) -> SyncStatus:
        with self._read() as conn:
            last = conn.execute(
                "SELECT finished_at FROM sync_batches WHERE phase='complete' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            active = conn.execute(
                "SELECT id,phase,started_at FROM sync_batches WHERE phase!='complete'"
            ).fetchone()
            attempt = conn.execute(
                "SELECT payload FROM sync_attempts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "last_success_at": last[0] if last else None,
                "active_batch": dict(active) if active else None,
                "pending": conn.execute(
                    "SELECT COUNT(*) FROM sync_jobs WHERE done=0"
                ).fetchone()[0],
                "notices": conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0],
                "versions": conn.execute(
                    "SELECT COUNT(*) FROM notice_versions"
                ).fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()[
                    0
                ],
                "last_attempt": json.loads(attempt[0]) if attempt else None,
            }

    def snapshots(self) -> tuple[NoticeSnapshot, ...]:
        with self._read() as conn:
            return tuple(
                snapshot
                for row in conn.execute(
                    "SELECT notice_id FROM notices ORDER BY notice_id"
                )
                if (snapshot := self._latest(conn, row[0])) is not None
            )

    def events(self) -> list[dict[str, object]]:
        with self._read() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM sync_events ORDER BY id")
            ]
