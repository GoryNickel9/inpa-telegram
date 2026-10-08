"""JSON-compatible status and attempt contracts shared by CLI and persistence."""

from typing import NotRequired, TypedDict


class SyncError(TypedDict):
    type: str
    message: str
    notice_id: NotRequired[str]


class SyncReport(TypedDict):
    batch_id: int
    source: str
    baseline: bool
    complete: bool
    fetched: int
    new: int
    changed: int
    errors: list[SyncError]
    warnings: list[dict[str, object]]
    started_at: str
    config: dict[str, float | int]
    finished_at: NotRequired[str]
    pending: NotRequired[int]
    interrupted: NotRequired[bool]
    source_stopped: NotRequired[bool]
    retry_not_before: NotRequired[str]
    deferred: NotRequired[bool]


class SyncStatus(TypedDict):
    last_success_at: str | None
    active_batch: dict[str, object] | None
    pending: int
    notices: int
    versions: int
    events: int
    last_attempt: SyncReport | None
