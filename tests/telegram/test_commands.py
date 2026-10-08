from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from inpa_bot.telegram.store import BotStore
from tests.sync.test_service import NOW
from .conftest import A, ADMIN, USER, command, rows


def test_start_is_one_menu_and_concorsi_opens_paged_results(archive):
    store, _ = archive
    command(store, 1, "/start")
    user = rows(store, "SELECT * FROM telegram_users")[0]
    assert user["chat_id"] == ADMIN and not user["notifications"]
    outbox = rows(store, "SELECT * FROM telegram_outbox")
    assert len(outbox) == 1 and outbox[0]["category"] == "panel_fresh"
    command(store, 2, "/concorsi")
    outbox = rows(store, "SELECT * FROM telegram_outbox")
    assert len(outbox) == 4
    assert outbox[0]["category"] == "panel_fresh"
    assert all(r["category"] == "reply" for r in outbox[1:])
    assert "Risultati 1–2" in outbox[1]["text"]


def test_repeated_update_has_one_effect_and_one_set_of_replies(archive):
    store, _ = archive
    command(store, 10, "/start")
    count = len(rows(store, "SELECT * FROM telegram_outbox"))
    command(store, 10, "/start")
    assert len(rows(store, "SELECT * FROM telegram_outbox")) == count
    assert store.offset() == 11


def test_command_and_offset_roll_back_together(archive):
    store, _ = archive
    with store._write() as conn:
        conn.execute(
            "CREATE TRIGGER fail_reply BEFORE INSERT ON telegram_outbox BEGIN SELECT RAISE(ABORT,'failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        command(store, 1, "/start")
    assert store.offset() == 0
    assert not rows(store, "SELECT started FROM telegram_users")[0][0]


@pytest.mark.parametrize("private,chat_id", [(False, -100), (True, USER)])
def test_groups_and_other_users_chat_are_rejected(archive, private, chat_id):
    store, _ = archive
    command(store, 1, "/start", private=private, chat_id=chat_id)
    assert not rows(store, "SELECT * FROM telegram_outbox")
    assert not rows(store, "SELECT started FROM telegram_users")[0][0]


def test_unauthorized_user_cannot_read_archive_or_mutate_filters(archive):
    store, _ = archive
    command(store, 1, "/concorsi", uid=USER)
    command(store, 2, "/filtri regione Lazio", uid=USER)
    assert not rows(store, "SELECT * FROM telegram_outbox")
    assert not rows(store, "SELECT * FROM telegram_users WHERE user_id=?", (USER,))


def test_access_requests_and_approval_then_start(archive):
    store, _ = archive
    command(store, 1, "/start", uid=USER)
    assert rows(store, "SELECT user_id FROM telegram_requests")[0][0] == USER
    command(store, 2, f"/approva {USER}")
    command(store, 3, "/start", uid=USER)
    assert (
        rows(store, "SELECT started FROM telegram_users WHERE user_id=?", (USER,))[0][0]
        == 1
    )


def test_user_cannot_approve_or_revoke_others(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, "/approva 33", uid=USER)
    command(store, 3, f"/revoca {ADMIN}", uid=USER)
    assert {r[0] for r in rows(store, "SELECT user_id FROM telegram_users")} == {
        ADMIN,
        USER,
    }


def test_authorized_user_limit(archive):
    store, _ = archive
    for update, uid in enumerate([22, 33, 44, 55], 1):
        command(store, update, f"/approva {uid}")
    assert len(rows(store, "SELECT user_id FROM telegram_users")) == 4


def test_revocation_cascades_private_data_and_queued_deliveries(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, f"/segui {A}", uid=USER)
    command(store, 3, f"/revoca {USER}")
    assert not rows(store, "SELECT * FROM telegram_follows WHERE user_id=?", (USER,))
    assert not rows(store, "SELECT * FROM telegram_outbox WHERE user_id=?", (USER,))


def test_filters_are_isolated_and_region_names_resolve(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, "/filtri regione Lazio", uid=USER)
    command(store, 3, "/filtri regione Sardegna")
    users = {r["user_id"]: r for r in rows(store, "SELECT * FROM telegram_users")}
    assert json.loads(users[ADMIN]["regions"]) == ["IT-88"]
    assert json.loads(users[USER]["regions"]) == ["IT-62"]
    command(store, 4, "/concorsi", uid=USER)
    assert (
        "Nessun bando aperto"
        in rows(
            store, "SELECT text FROM telegram_outbox WHERE logical_key='reply:4:0'"
        )[0][0]
    )


def test_province_filter_does_not_assign_region_jobs_to_province(archive):
    store, _ = archive
    command(store, 1, "/filtri provincia SS")
    command(store, 2, "/concorsi")
    assert (
        "Nessun bando aperto"
        in rows(
            store, "SELECT text FROM telegram_outbox WHERE logical_key='reply:2:0'"
        )[0][0]
    )


def test_invalid_filters_leave_preferences_unchanged(archive):
    store, _ = archive
    command(store, 1, "/filtri regione Sardegna")
    command(store, 2, "/filtri regione Inventata")
    assert json.loads(rows(store, "SELECT regions FROM telegram_users")[0][0]) == [
        "IT-88"
    ]


def test_callback_is_scoped_to_clicking_user(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, f"follow:{A}", uid=USER, callback=True)
    assert rows(store, "SELECT user_id FROM telegram_follows")[0][0] == USER
    command(store, 3, f"hide:{A}", callback=True)
    assert rows(store, "SELECT user_id FROM telegram_follows")[0][0] == USER


def test_delete_requires_current_user_confirmation(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, "/elimina", uid=USER)
    command(store, 3, "delete:confirm", callback=True)
    assert rows(store, "SELECT * FROM telegram_users WHERE user_id=?", (ADMIN,))
    ack = command(store, 4, "delete:confirm", uid=USER, callback=True)
    assert "cancellati" in ack
    assert not rows(store, "SELECT * FROM telegram_users WHERE user_id=?", (USER,))
    assert store.offset() == 5


def test_expired_confirmation_does_not_delete(archive):
    store, _ = archive
    command(store, 1, "/elimina")
    command(store, 2, "delete:confirm", callback=True, now=NOW + timedelta(minutes=6))
    assert rows(store, "SELECT * FROM telegram_users")


def test_official_link_uses_archive_without_network(archive):
    store, _ = archive
    command(
        store,
        1,
        f"https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id={A}",
    )
    assert rows(store, "SELECT notice_id FROM telegram_outbox")[0][0] == A


def test_notifications_default_off_and_enable_skips_old_events(archive):
    store, _ = archive
    command(store, 1, "/start")
    command(store, 2, "/notifiche on")
    assert store.collect_events(NOW) == 0
    assert not rows(store, "SELECT * FROM telegram_outbox WHERE category='notice'")


def test_reopen_keeps_offset_and_filters(archive):
    store, _ = archive
    command(store, 12, "/filtri parole Original")
    with BotStore(store.path) as reopened:
        assert reopened.offset() == 13
        assert rows(reopened, "SELECT keywords FROM telegram_users")[0][0] == "Original"


def test_updates_handler_failure_does_not_acknowledge(archive):
    store, _ = archive
    with pytest.raises(RuntimeError):
        store.apply_update(
            99, lambda conn: (_ for _ in ()).throw(RuntimeError("crash"))
        )
    assert store.offset() == 0


def test_bot_identity_is_bound_to_archive(archive):
    store, _ = archive
    store.bind_bot(123)
    store.bind_bot(123)
    with pytest.raises(ValueError):
        store.bind_bot(456)


def test_removed_admin_config_revokes_old_admin_data(archive):
    store, _ = archive
    command(store, 1, "/filtri parole amministrativo")
    store.configure_admins(frozenset({33}), 4, NOW)
    assert not rows(store, "SELECT * FROM telegram_users WHERE user_id=?", (ADMIN,))
    assert not rows(store, "SELECT * FROM telegram_outbox WHERE user_id=?", (ADMIN,))


def test_m1_can_reopen_archive_after_m2_extension(archive):
    from inpa_bot.sync.store import SyncStore

    store, _ = archive
    with SyncStore(store.path) as m1:
        assert m1.latest(A) is not None
        assert m1.status()["notices"] == 2
