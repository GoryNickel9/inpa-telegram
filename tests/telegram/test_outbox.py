from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from inpa_bot.sync.service import Synchronizer
from tests.sync.test_service import NOW
from .conftest import A, C, ADMIN, USER, command, rows


def new_cycle(store, source):
    source.key += "-updated"
    return Synchronizer(source, store, clock=lambda: source.now).run_once()


def subscribe(store):
    command(store, 1, "/start")
    command(store, 2, "/notifiche on")


def test_new_notices_deduplicate_across_collector_restarts(archive):
    store, source = archive
    subscribe(store)
    source.ids = (A, C)
    new_cycle(store, source)
    assert store.collect_events(NOW) == 1
    assert store.collect_events(NOW) == 0
    notices = rows(store, "SELECT * FROM telegram_outbox WHERE category='notice'")
    assert len(notices) == 1 and notices[0]["notice_id"] == C


def test_changes_are_sent_as_updates_not_new_notices(archive):
    store, source = archive
    subscribe(store)
    source.title = "Proroga"
    new_cycle(store, source)
    assert store.collect_events(NOW) == 2
    assert all(
        r[0].startswith("Bando aggiornato")
        for r in rows(store, "SELECT text FROM telegram_outbox WHERE category='notice'")
    )


def test_event_cursor_and_enqueue_roll_back_together(archive):
    store, source = archive
    subscribe(store)
    old = rows(store, "SELECT event_cursor FROM telegram_users")[0][0]
    source.ids += (C,)
    new_cycle(store, source)
    with store._write() as conn:
        conn.execute(
            "CREATE TRIGGER fail_notification BEFORE INSERT ON telegram_outbox WHEN NEW.category='notice' BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.collect_events(NOW)
    assert rows(store, "SELECT event_cursor FROM telegram_users")[0][0] == old


def test_changing_filters_cancels_stale_deliveries(archive):
    store, source = archive
    subscribe(store)
    source.ids += (C,)
    new_cycle(store, source)
    store.collect_events(NOW)
    command(store, 3, "/filtri regione Lazio")
    assert (
        rows(store, "SELECT state FROM telegram_outbox WHERE category='notice'")[0][0]
        == "cancelled"
    )


def test_repeated_enable_does_not_drop_queued_notifications(archive):
    store, source = archive
    subscribe(store)
    source.ids += (C,)
    new_cycle(store, source)
    store.collect_events(NOW)
    command(store, 3, "/notifiche on")
    assert (
        rows(store, "SELECT state FROM telegram_outbox WHERE category='notice'")[0][0]
        == "queued"
    )


def test_hiding_notice_cancels_queued_notification(archive):
    store, source = archive
    subscribe(store)
    source.ids += (C,)
    new_cycle(store, source)
    store.collect_events(NOW)
    command(store, 3, f"/nascondi {C}")
    assert (
        rows(store, "SELECT state FROM telegram_outbox WHERE category='notice'")[0][0]
        == "cancelled"
    )


def test_user_specific_matching(archive):
    store, source = archive
    subscribe(store)
    command(store, 3, f"/approva {USER}")
    command(store, 4, "/filtri regione Lazio", uid=USER)
    command(store, 5, "/notifiche on", uid=USER)
    source.ids += (C,)
    new_cycle(store, source)
    store.collect_events(NOW)
    assert {
        r[0]
        for r in rows(
            store, "SELECT user_id FROM telegram_outbox WHERE category='notice'"
        )
    } == {ADMIN}


def test_claim_prevents_duplicate_senders_and_crash_becomes_ambiguous(archive):
    store, _ = archive
    command(store, 1, "/privacy")
    first = store.claim(NOW)
    assert first is not None and store.claim(NOW) is None
    assert store.recover_sending(NOW) == 1
    assert store.claim(NOW) is None
    assert rows(store, "SELECT state FROM telegram_outbox")[0][0] == "ambiguous"


def test_retry_after_is_persisted_and_global(archive):
    store, _ = archive
    command(store, 1, "/privacy")
    command(store, 2, "/stato")
    first = store.claim(NOW)
    store.finish_delivery(first["id"], "queued", NOW, retry_seconds=120)
    assert store.claim(NOW + timedelta(seconds=119)) is None
    assert store.claim(NOW + timedelta(seconds=120)) is not None


def test_sent_deliveries_do_not_repeat(archive):
    store, _ = archive
    command(store, 1, "/privacy")
    item = store.claim(NOW)
    store.finish_delivery(item["id"], "sent", NOW, message_id=999)
    assert store.claim(NOW) is None
    assert rows(store, "SELECT message_id FROM telegram_outbox")[0][0] == 999


def test_retention_prunes_completed_but_keeps_uncertain(archive):
    store, _ = archive
    command(store, 1, "/privacy")
    first = store.claim(NOW)
    store.finish_delivery(first["id"], "sent", NOW)
    command(store, 2, "/stato")
    second = store.claim(NOW)
    store.finish_delivery(second["id"], "ambiguous", NOW)
    store.prune(NOW + timedelta(days=8))
    assert store.delivery_counts() == {"ambiguous": 1}
