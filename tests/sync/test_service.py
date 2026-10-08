"""M1 acceptance tests: recovery, atomicity, reconciliation and source failures."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from inpa_bot.source.errors import HttpStatusError, TransportError
from inpa_bot.source.models import (
    Anomaly,
    AnomalySeverity,
    NoticeSnapshot,
    NoticeStub,
    Provenance,
    SearchFilters,
    SearchPage,
    SourceDateTime,
)
from inpa_bot.source.snapshot import SnapshotStore
from inpa_bot.sync.service import SyncConfig, Synchronizer
from inpa_bot.sync.store import SyncBusyError, SyncStore

NOW = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)


class FakeSource:
    name = "test-source"

    def __init__(self, ids: tuple[str, ...] = ("a", "b")) -> None:
        self.ids = ids
        self.calls: list[str] = []
        self.refreshes = 0
        self.now = NOW
        self.key = "v1"
        self.fail: dict[str, BaseException] = {}
        self.title = "Original"
        self.raw_hash = "raw"

    def refresh(self) -> None:
        self.refreshes += 1

    def search(
        self, filters: SearchFilters, page_cursor: str | None = None
    ) -> SearchPage:
        assert filters == SearchFilters()
        return SearchPage(
            tuple(
                NoticeStub(
                    i,
                    f"https://example.test/{i}",
                    Provenance("test", self.now),
                    SourceDateTime(self.key, date_only=self.now.date()),
                )
                for i in self.ids
            ),
            True,
            declared_total=len(self.ids),
        )

    def fetch_notice(self, notice_id: str) -> NoticeSnapshot:
        self.calls.append(notice_id)
        if notice_id in self.fail:
            raise self.fail[notice_id]
        return NoticeSnapshot(
            notice_id,
            f"https://example.test/{notice_id}",
            self.now,
            title=self.title,
            raw_document_hash=self.raw_hash,
        )


def sync(source: FakeSource, store: SyncStore, **kwargs) -> Synchronizer:
    return Synchronizer(source, store, clock=lambda: source.now, **kwargs)


def test_baseline_then_unchanged_restart_has_no_duplicate(tmp_path):
    path = tmp_path / "archive.sqlite3"
    first = FakeSource()
    with SyncStore(path) as store:
        assert sync(first, store).run_once()["complete"]
        assert [e["kind"] for e in store.events()] == ["baseline", "baseline"]
    second = FakeSource()
    second.now += timedelta(hours=1)
    with SyncStore(path) as store:
        report = sync(second, store).run_once()
        assert report["complete"] and report["fetched"] == 0
        assert second.refreshes == 1
        assert store.status()["versions"] == 2
        assert store.status()["events"] == 2


def test_new_and_changed_after_baseline_and_old_id_not_discarded(tmp_path):
    source = FakeSource(("a",))
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        source.ids = ("a", "b")
        source.key = "v2"
        source.title = "Rettifica"
        result = sync(source, store).run_once()
        assert (result["new"], result["changed"]) == (1, 1)
        assert [e["kind"] for e in store.events()] == ["baseline", "changed", "new"]
        assert store.latest("a").title == "Rettifica"


def test_failed_job_resumes_only_pending_after_reopen(tmp_path):
    path = tmp_path / "db"
    source = FakeSource()
    source.fail["b"] = TransportError("timeout")
    with SyncStore(path) as store:
        result = sync(source, store).run_once()
        assert not result["complete"] and result["pending"] == 1
        assert store.status()["last_success_at"] is None
    replacement = FakeSource()
    with SyncStore(path) as store:
        result2 = sync(replacement, store).run_once()
        assert result2["batch_id"] == result["batch_id"]
        assert result2["complete"]
        assert replacement.calls == ["b"]
        assert replacement.refreshes == 0
        assert store.status()["events"] == 2


def test_daily_reconciliation_ignores_unchanged_lastmod(tmp_path):
    source = FakeSource(("a",))
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        source.title = "Proroga senza aggiornamento sitemap"
        source.now += timedelta(days=1)
        # Keep lastmod exactly unchanged while moving the local clock.
        original_search = source.search
        source.search = lambda *args: replace(
            original_search(*args),
            records=(
                replace(
                    original_search(*args).records[0],
                    source_last_modified=SourceDateTime("v1", date_only=NOW.date()),
                ),
            ),
        )
        result = sync(source, store).run_once()
        assert result["changed"] == 1 and source.calls == ["a", "a"]


def test_raw_html_change_does_not_create_change_event(tmp_path):
    source = FakeSource(("a",))
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        source.raw_hash = "random-token"
        source.key = "v2"
        assert sync(source, store).run_once()["changed"] == 0
        assert store.status()["versions"] == 2
        assert store.status()["events"] == 1


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "duplicate",
        "total",
        "cursor",
        "blocking",
        "filters",
        "pages",
        "records",
    ],
)
def test_incomplete_discovery_does_not_save_or_advance(tmp_path, mode):
    source = FakeSource()
    page = source.search(SearchFilters())
    config = SyncConfig()
    if mode == "partial":
        page = replace(page, complete=False)
    elif mode == "duplicate":
        page = replace(page, records=(page.records[0], page.records[0]))
    elif mode == "total":
        page = replace(page, declared_total=3)
    elif mode == "cursor":
        page = replace(page, next_cursor="repeat", declared_total=None)
    elif mode == "blocking":
        page = replace(
            page, anomalies=(Anomaly("bad", "bad", AnomalySeverity.BLOCKING),)
        )
    elif mode == "filters":
        page = replace(page, unapplied_filters=("region",))
    elif mode == "pages":
        config = SyncConfig(max_pages=1)
        page = replace(page, next_cursor="next", declared_total=None)
    else:
        config = SyncConfig(max_records=1)
    source.search = lambda *args: page
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store, config=config).run_once()
        assert not result["complete"] and result["errors"]
        assert not source.calls and store.status()["notices"] == 0
        assert store.status()["last_success_at"] is None


def test_failed_discovery_is_refreshed_on_resume(tmp_path):
    source = FakeSource()
    search = source.search
    source.search = lambda *args: replace(search(*args), complete=False)
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once()
        source.search = search
        retry = sync(source, store).run_once()
        assert retry["complete"] and retry["batch_id"] == result["batch_id"]
        assert source.refreshes == 2


def test_missing_notice_is_rechecked_then_retained_without_deletion(tmp_path):
    source = FakeSource(("a",))
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        source.ids = ()
        source.now += timedelta(days=1)
        assert sync(source, store).run_once()["fetched"] == 1
        assert store.latest("a") is not None
        source.now += timedelta(days=8)
        assert sync(source, store).run_once()["fetched"] == 0
        assert store.status()["notices"] == 1


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_source_block_stops_remaining_requests(tmp_path, status):
    source = FakeSource()
    source.fail["a"] = HttpStatusError(status, "test")
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once()
        assert result["source_stopped"] and not result["complete"]
        assert source.calls == ["a"]


def test_retry_after_survives_restart_and_blocks_manual_sync(tmp_path):
    path = tmp_path / "db"
    source = FakeSource()
    source.fail["a"] = HttpStatusError(429, "test", retry_after=7200)
    with SyncStore(path) as store:
        sync(source, store).run_once()
    source2 = FakeSource()
    source2.now += timedelta(hours=1)
    with SyncStore(path) as store:
        assert sync(source2, store).run_once()["deferred"]
        assert source2.calls == [] and source2.refreshes == 0
        source2.now += timedelta(hours=1)
        assert sync(source2, store).run_once()["complete"]


def test_atomic_rollback_snapshot_event_and_job(tmp_path):
    source = FakeSource(("a",))
    path = tmp_path / "db"
    with SyncStore(path) as store:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TRIGGER fail_event BEFORE INSERT ON sync_events BEGIN SELECT RAISE(ABORT,'disk failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
            sync(source, store).run_once()
        assert store.latest("a") is None
        assert store.status()["pending"] == 1
        assert not store.events()
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TRIGGER fail_event")
        assert sync(source, store).run_once()["complete"]
        assert store.status()["versions"] == 1


def test_cancellation_saves_pending_manifest(tmp_path):
    source = FakeSource()
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once(
            should_stop=lambda: len(source.calls) == 1
        )
        assert result["interrupted"] and result["pending"] == 1
        assert sync(source, store).run_once()["complete"]
        assert source.calls == ["a", "b"]


def test_two_stores_cannot_synchronize_same_file(tmp_path):
    path = tmp_path / "db"
    with SyncStore(path) as first, SyncStore(path) as second:
        with first.synchronizing():
            with pytest.raises(SyncBusyError):
                with second.synchronizing():
                    pytest.fail("Lock was not exclusive")
        with second.synchronizing():
            pass


def test_upgrade_m0_archive_preserves_data(tmp_path):
    path = tmp_path / "db"
    source = FakeSource(("a",))
    with SnapshotStore(path) as old:
        old.record(source.fetch_notice("a"))
    source.calls.clear()
    with SyncStore(path) as store:
        assert sync(source, store).run_once()["complete"]
        assert store.status()["versions"] == 1
        assert not store.events()
    with SnapshotStore(path) as old:
        assert old.latest("a").title == "Original"


def test_process_crash_releases_lock_and_resumes_committed_work(tmp_path):
    path = tmp_path / "crash.sqlite3"
    # A real process dies without context-manager cleanup after committing a.
    script = """
import os, sys
from tests.sync.test_service import FakeSource, sync
from inpa_bot.sync.store import SyncStore
class CrashingSource(FakeSource):
    def fetch_notice(self, notice_id):
        if notice_id == 'b':
            os._exit(9)
        return super().fetch_notice(notice_id)
with SyncStore(sys.argv[1]) as store:
    sync(CrashingSource(), store).run_once()
"""
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(Path.cwd() / "src"), str(Path.cwd())]),
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 9, child.stderr
    source = FakeSource()
    with SyncStore(path) as store:
        assert store.status()["versions"] == 1
        assert sync(source, store).run_once()["complete"]
        assert source.calls == ["b"]
        assert store.status()["events"] == 2


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_reject_bad_intervals(value):
    with pytest.raises(ValueError):
        SyncConfig(interval_seconds=value)


def test_reject_other_source_and_future_schema(tmp_path):
    path = tmp_path / "db"
    with SyncStore(path) as store:
        sync(FakeSource(), store).run_once()
        other = FakeSource()
        other.name = "other-source"
        with pytest.raises(ValueError, match="separate archive"):
            sync(other, store).run_once()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_meta SET value='2' WHERE key='m1_schema_version'")
    with pytest.raises(RuntimeError, match="Unsupported M1"):
        SyncStore(path)


def test_report_is_json_serializable(tmp_path):
    with SyncStore(tmp_path / "db") as store:
        assert json.loads(json.dumps(sync(FakeSource(), store).run_once()))["complete"]


def test_success_timestamp_does_not_advance_after_later_failure(tmp_path):
    source = FakeSource(("a",))
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        previous = store.status()["last_success_at"]
        source.now += timedelta(hours=1)
        source.key = "changed"
        source.fail["a"] = TransportError("offline")
        assert not sync(source, store).run_once()["complete"]
        assert store.status()["last_success_at"] == previous


def test_retry_after_on_500_stops_other_requests(tmp_path):
    source = FakeSource()
    source.fail["a"] = HttpStatusError(500, "test", retry_after=600)
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once()
        assert result["source_stopped"] and result["retry_not_before"]
        assert source.calls == ["a"]


def test_cancelled_discovery_makes_no_search_requests(tmp_path):
    source = FakeSource()
    source.search = lambda *args: pytest.fail("Search after cancellation")
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once(should_stop=lambda: True)
        assert result["interrupted"] and not result["complete"]


def test_missing_lastmod_is_reread_every_cycle(tmp_path):
    source = FakeSource(("a",))
    search = source.search
    source.search = lambda *args: replace(
        search(*args),
        records=(replace(search(*args).records[0], source_last_modified=None),),
    )
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        assert sync(source, store).run_once()["fetched"] == 1


@pytest.mark.parametrize("changed_total", [False, True])
def test_multi_page_catalogue_and_changing_total(tmp_path, changed_total):
    source = FakeSource()
    page = source.search(SearchFilters())

    def search(filters, cursor=None):
        if cursor is None:
            return replace(page, records=page.records[:1], next_cursor="second")
        return replace(
            page, records=page.records[1:], declared_total=3 if changed_total else 2
        )

    source.search = search
    with SyncStore(tmp_path / "db") as store:
        result = sync(source, store).run_once()
        assert result["complete"] != changed_total
        assert len(source.calls) == (0 if changed_total else 2)
