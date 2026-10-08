from __future__ import annotations

from dataclasses import replace

import pytest

from inpa_bot.source.models import (
    NoticeStatus,
    Provenance,
    TerritoryScope,
    TerritoryValue,
)
from inpa_bot.sync.service import Synchronizer
from inpa_bot.telegram.commands import Commands, Incoming
from inpa_bot.telegram.store import BotStore
from tests.sync.test_service import FakeSource, NOW

A, B, C = "a" * 32, "b" * 32, "c" * 32
ADMIN, USER = 11, 22


class Source(FakeSource):
    def __init__(self, ids=(A, B)):
        super().__init__(ids)
        self.region = "IT-88"

    def fetch_notice(self, notice_id):
        snapshot = super().fetch_notice(notice_id)
        return replace(
            snapshot,
            status_normalized=NoticeStatus.OPEN,
            territories=(
                TerritoryValue(
                    "Sardegna" if self.region == "IT-88" else "Lazio",
                    TerritoryScope.UNSPECIFIED,
                    Provenance("https://www.inpa.gov.it", self.now),
                    region_code=self.region,
                ),
            ),
        )


@pytest.fixture
def archive(tmp_path):
    with BotStore(tmp_path / "bot.sqlite3") as store:
        store.configure_admins(frozenset({ADMIN}), 4, NOW)
        source = Source()
        Synchronizer(source, store, clock=lambda: source.now).run_once()
        yield store, source


def command(
    store,
    update_id,
    text,
    uid=ADMIN,
    *,
    callback=False,
    chat_id=None,
    private=True,
    now=NOW,
):
    return Commands(store).handle(
        Incoming(
            update_id, uid, uid if chat_id is None else chat_id, private, text, callback
        ),
        now,
    )


def rows(store, sql, params=()):
    with store._read() as conn:
        return conn.execute(sql, params).fetchall()
