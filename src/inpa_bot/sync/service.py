"""Resumable catalogue synchronization, with conservative daily reconciliation."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from inpa_bot.source.errors import (
    HttpStatusError,
    IncompleteAcquisitionError,
    RobotsDisallowedError,
    SourceError,
    UnsafeUrlError,
)
from inpa_bot.source.models import (
    NoticeSnapshot,
    SearchFilters,
    SearchPage,
    SourceDateTime,
)

from .store import SyncStore
from .types import SyncError, SyncReport


class CatalogueSource(Protocol):
    """M1 requires explicit refresh to avoid reusing yesterday's catalogue."""

    name: str

    def refresh(self) -> None: ...
    def search(
        self, filters: SearchFilters, page_cursor: str | None = None
    ) -> SearchPage: ...
    def fetch_notice(self, notice_id: str) -> NoticeSnapshot: ...


@dataclass(frozen=True, slots=True)
class SyncConfig:
    interval_seconds: float = 3600
    reconcile_seconds: float = 86400
    missing_retention_seconds: float = 7 * 86400
    max_records: int = 100_000
    max_pages: int = 10_000

    def __post_init__(self) -> None:
        for name in (
            "interval_seconds",
            "reconcile_seconds",
            "missing_retention_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("max_records", "max_pages"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def lastmod_key(value: SourceDateTime | None) -> str | None:
    if value is None or not value.resolved:
        return None
    return json.dumps(
        {
            "raw": value.raw,
            "date": str(value.date_only),
            "instant": value.instant.isoformat() if value.instant else None,
        },
        sort_keys=True,
    )


def _stop_source(exc: SourceError) -> bool:
    return (
        isinstance(exc, (RobotsDisallowedError, UnsafeUrlError))
        or isinstance(exc, HttpStatusError)
        and (
            exc.status in {401, 403, 429, 503}
            or exc.retry_after is not None
            and exc.retry_after > 0
        )
    )


class Synchronizer:
    def __init__(
        self,
        source: CatalogueSource,
        store: SyncStore,
        config: SyncConfig | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.source = source
        self.store = store
        self.config = config or SyncConfig()
        self.clock = clock

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Synchronization clock must include a timezone")
        return now.astimezone(timezone.utc)

    def _discover(
        self, should_stop: Callable[[], bool]
    ) -> tuple[dict[str, str | None], int, list[dict[str, object]]]:
        self.source.refresh()
        catalogue: dict[str, str | None] = {}
        cursors: set[str | None] = set()
        cursor = None
        total = None
        warnings: list[dict[str, object]] = []
        for page_no in range(1, self.config.max_pages + 1):
            if should_stop():
                raise IncompleteAcquisitionError(
                    "Synchronization stopped during discovery"
                )
            if cursor in cursors:
                raise IncompleteAcquisitionError("Repeated catalogue cursor")
            cursors.add(cursor)
            page = self.source.search(SearchFilters(), cursor)
            if not page.complete or any(a.blocking for a in page.anomalies):
                raise IncompleteAcquisitionError("Incomplete catalogue page")
            if page.unapplied_filters:
                raise IncompleteAcquisitionError(
                    "Unexpected unapplied filters on unfiltered catalogue"
                )
            for anomaly in page.anomalies:
                item = asdict(anomaly)
                if item not in warnings:
                    warnings.append(item)
            if page.declared_total is not None:
                if page.declared_total < 0 or (
                    total is not None and total != page.declared_total
                ):
                    raise IncompleteAcquisitionError(
                        "Catalogue total changed during pagination"
                    )
                total = page.declared_total
            for stub in page.records:
                if not stub.notice_id or stub.notice_id in catalogue:
                    raise IncompleteAcquisitionError("Empty or duplicate catalogue ID")
                catalogue[stub.notice_id] = lastmod_key(stub.source_last_modified)
                if len(catalogue) > self.config.max_records:
                    raise IncompleteAcquisitionError("Catalogue exceeds max_records")
            if page.next_cursor is None:
                if total is not None and total != len(catalogue):
                    raise IncompleteAcquisitionError(
                        "Catalogue total differs from unique IDs"
                    )
                return catalogue, page_no, warnings
            if not page.records:
                raise IncompleteAcquisitionError("Empty non-final catalogue page")
            cursor = page.next_cursor
        raise IncompleteAcquisitionError("Catalogue exceeds max_pages")

    def run_once(
        self, *, should_stop: Callable[[], bool] = lambda: False
    ) -> SyncReport:
        with self.store.synchronizing():
            now = self._now()
            previous = self.store.status()["last_attempt"]
            if previous and previous.get("retry_not_before"):
                if now < datetime.fromisoformat(previous["retry_not_before"]):
                    return {**previous, "deferred": True}
            batch = self.store.begin(self.source.name, now)
            batch_id = batch["id"]
            report: SyncReport = {
                "batch_id": batch_id,
                "source": self.source.name,
                "baseline": bool(batch["baseline"]),
                "complete": False,
                "fetched": 0,
                "new": 0,
                "changed": 0,
                "errors": [],
                "warnings": [],
                "started_at": now.isoformat(),
                "config": asdict(self.config),
            }
            errors: list[SyncError] = []
            if batch["phase"] == "discovering":
                try:
                    catalogue, pages, warnings = self._discover(should_stop)
                    report["warnings"] = warnings
                except SourceError as exc:
                    errors.append({"type": type(exc).__name__, "message": str(exc)})
                    self._retry_after(report, exc)
                    report["errors"] = errors
                    if should_stop():
                        report["interrupted"] = True
                    report["finished_at"] = self._now().isoformat()
                    self.store.finish_attempt(batch_id, report, self._now())
                    return report
                self.store.import_existing()
                tracking = self.store.tracking()
                jobs: list[tuple[str, str | None, bool]] = []
                for notice_id, key in catalogue.items():
                    old = tracking.get(notice_id)
                    due = (
                        old is None
                        or old["fetched_at"] is None
                        or key is None
                        or key != old["lastmod_key"]
                        or now - datetime.fromisoformat(old["fetched_at"])
                        >= timedelta(seconds=self.config.reconcile_seconds)
                    )
                    if due:
                        jobs.append((notice_id, key, True))
                for notice_id, old in tracking.items():
                    if notice_id in catalogue:
                        continue
                    missing_since = (
                        datetime.fromisoformat(old["missing_since"])
                        if old["missing_since"]
                        else now
                    )
                    if now - missing_since <= timedelta(
                        seconds=self.config.missing_retention_seconds
                    ):
                        if old["fetched_at"] is None or now - datetime.fromisoformat(
                            old["fetched_at"]
                        ) >= timedelta(seconds=self.config.reconcile_seconds):
                            jobs.append((notice_id, old["lastmod_key"], False))
                self.store.plan(batch_id, catalogue, jobs, pages, now)
            for job in self.store.pending(batch_id):
                if should_stop():
                    report["interrupted"] = True
                    break
                try:
                    snapshot = self.source.fetch_notice(job["notice_id"])
                    if (
                        snapshot.notice_id != job["notice_id"]
                        or snapshot.has_blocking_anomaly
                    ):
                        raise IncompleteAcquisitionError(
                            "Snapshot ID mismatch or blocking anomaly"
                        )
                    if snapshot.observed_at.tzinfo is None:
                        raise IncompleteAcquisitionError(
                            "Snapshot observation has no timezone"
                        )
                    diff = self.store.commit_notice(
                        job, snapshot, bool(batch["baseline"]), self._now()
                    )
                    report["fetched"] += 1
                    report["new"] += int(diff.is_new)
                    report["changed"] += int(diff.significant and not diff.is_new)
                except SourceError as exc:
                    self._retry_after(report, exc)
                    message = f"{type(exc).__name__}: {exc}"
                    self.store.job_error(job, message)
                    errors.append(
                        {
                            "notice_id": job["notice_id"],
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    if _stop_source(exc):
                        report["source_stopped"] = True
                        break
            report["errors"] = errors
            report["pending"] = len(self.store.pending(batch_id))
            report["complete"] = (
                not errors
                and not report["pending"]
                and not report.get("interrupted", False)
            )
            report["finished_at"] = self._now().isoformat()
            self.store.finish_attempt(batch_id, report, self._now())
            return report

    def _retry_after(self, report: SyncReport, exc: SourceError) -> None:
        if isinstance(exc, HttpStatusError) and exc.retry_after is not None:
            seconds = exc.retry_after
            if math.isfinite(seconds) and seconds > 0:
                # Saturate unrepresentable dates conservatively instead of retrying early.
                try:
                    deadline = self._now() + timedelta(seconds=seconds)
                except OverflowError:
                    deadline = datetime.max.replace(tzinfo=timezone.utc)
                report["retry_not_before"] = deadline.isoformat()
