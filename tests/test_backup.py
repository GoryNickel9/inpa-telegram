from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from scripts.backup_sqlite import backup_database


def test_live_wal_backup_restores_committed_data_and_keeps_original(tmp_path):
    source = tmp_path / "live.sqlite3"
    with closing(sqlite3.connect(source)) as origin:
        origin.execute("PRAGMA journal_mode=WAL")
        origin.execute("CREATE TABLE values_table (value TEXT)")
        origin.execute("INSERT INTO values_table VALUES ('committed')")
        origin.commit()
        origin.execute("INSERT INTO values_table VALUES ('uncommitted')")
        path = backup_database(source, tmp_path / "backups")
        with closing(sqlite3.connect(path)) as restored:
            assert restored.execute("SELECT * FROM values_table").fetchall() == [
                ("committed",)
            ]
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert origin.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 2
    assert not path.with_name(path.name + "-wal").exists()


def test_retention_preserves_unrelated_files(tmp_path):
    source = tmp_path / "live.sqlite3"
    with closing(sqlite3.connect(source)) as origin:
        origin.execute("CREATE TABLE data (n INTEGER)")
    directory = tmp_path / "backups"
    first = backup_database(source, directory, keep=1)
    unrelated = directory / "important.sqlite3"
    unrelated.write_bytes(b"keep")
    second = backup_database(source, directory, keep=1)
    assert second.exists() and not first.exists()
    assert unrelated.read_bytes() == b"keep"


def test_corrupt_source_does_not_prune_existing_backup(tmp_path):
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"not a database")
    directory = tmp_path / "backups"
    directory.mkdir()
    prior = directory / "inpa-20261008T000000000000Z.sqlite3"
    prior.write_bytes(b"keep")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, directory, keep=1)
    assert list(directory.iterdir()) == [prior]


def test_missing_source_is_not_created(tmp_path):
    source = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_database(source, tmp_path / "backups")
    assert not source.exists()


def test_invalid_retention_rejected(tmp_path):
    with pytest.raises(ValueError):
        backup_database(tmp_path / "missing", tmp_path, keep=0)
