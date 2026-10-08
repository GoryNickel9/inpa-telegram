from __future__ import annotations

import json

import pytest

from .conftest import ADMIN, USER, command, rows


def test_welcome_has_menu_and_admin_can_approve_by_button(archive):
    store, _ = archive
    command(store, 1, "/start", uid=USER)
    command(store, 2, "/start")
    welcome = rows(store, "SELECT buttons FROM telegram_outbox ORDER BY id LIMIT 1")[0]
    assert "menu:richieste" in welcome[0]
    command(store, 3, "menu:richieste", callback=True)
    request = rows(
        store, "SELECT buttons FROM telegram_outbox ORDER BY id DESC LIMIT 1"
    )[0]
    assert f"approve:{USER}" in request[0]
    command(store, 4, f"approve:{USER}", callback=True)
    command(store, 5, "/start", uid=USER)
    assert rows(store, "SELECT started FROM telegram_users WHERE user_id=?", (USER,))[
        0
    ][0]
    welcome = rows(
        store,
        "SELECT buttons FROM telegram_outbox WHERE user_id=? ORDER BY id LIMIT 1",
        (USER,),
    )[0]
    assert "menu:concorsi" in welcome[0] and "menu:richieste" not in welcome[0]


@pytest.mark.parametrize("action", ["menu:richieste", "approve:33"])
def test_admin_buttons_cannot_be_used_by_regular_user(archive, action):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, action, uid=USER, callback=True)
    assert (
        "riservato"
        in rows(store, "SELECT text FROM telegram_outbox ORDER BY id DESC LIMIT 1")[0][
            0
        ]
    )
    assert {row[0] for row in rows(store, "SELECT user_id FROM telegram_users")} == {
        ADMIN,
        USER,
    }


@pytest.mark.parametrize(
    "destination",
    ["home", "concorsi", "filtri", "seguiti", "notifiche", "stato"],
)
def test_menu_destinations_work_without_typed_commands(archive, destination):
    store, _ = archive
    command(store, 1, "/start")
    before = rows(store, "SELECT MAX(id) FROM telegram_outbox")[0][0]
    command(store, 2, f"menu:{destination}", callback=True)
    replies = rows(
        store, "SELECT text,buttons FROM telegram_outbox WHERE id>?", (before,)
    )
    assert replies
    assert not any("Scegli un pulsante" in row[0] for row in replies)
    for row in replies:
        for buttons in json.loads(row[1]):
            for button in buttons:
                assert len(button.get("callback_data", "").encode()) <= 64


def test_menu_request_does_not_expose_archive_to_unauthorized_user(archive):
    store, _ = archive
    command(store, 1, "menu:concorsi", uid=USER, callback=True)
    assert not rows(store, "SELECT * FROM telegram_outbox")
