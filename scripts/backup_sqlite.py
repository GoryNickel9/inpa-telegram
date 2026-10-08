"""Create and verify a self-contained SQLite backup, including committed WAL data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BACKUP_NAME = re.compile(r"inpa-\d{8}T\d{12}Z\.sqlite3\Z")


def backup_database(source: Path, directory: Path, *, keep: int = 7) -> Path:
    if keep < 1:
        raise ValueError("keep must be positive")
    source = source.resolve(strict=True)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".inpa-backup-", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"inpa-{timestamp}.sqlite3"
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(temporary)) as destination:
                origin.backup(destination, pages=256, sleep=0.1)
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise sqlite3.DatabaseError("Backup integrity check failed")
                if (
                    destination.execute("PRAGMA foreign_key_check").fetchone()
                    is not None
                ):
                    raise sqlite3.IntegrityError("Backup foreign key check failed")
        # Windows requires a writable handle for FlushFileBuffers via fsync.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    # Remove only this tool's dated regular files, never the live source or symlinks.
    backups = sorted(
        entry
        for entry in directory.iterdir()
        if BACKUP_NAME.fullmatch(entry.name)
        and entry.is_file()
        and not entry.is_symlink()
        and entry.resolve() != source
    )
    for old in backups[:-keep]:
        old.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    try:
        target = backup_database(args.source, args.directory, keep=args.keep)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(
            json.dumps({"event": "backup_failed", "type": type(exc).__name__}),
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {
                "event": "backup_created",
                "path": str(target),
                "bytes": target.stat().st_size,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
