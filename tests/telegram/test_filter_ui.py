from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

from inpa_bot.source.models import TerritoryValue, TerritoryScope, Provenance
from inpa_bot.telegram.store import BotStore
from inpa_bot.telegram.views import Preferences, matches
from tests.sync.test_service import NOW

from .conftest import ADMIN, A, command, rows


def last_panel(store):
    row = rows(
        store,
        "SELECT * FROM telegram_outbox WHERE category IN ('panel','panel_fresh') ORDER BY id DESC LIMIT 1",
    )[0]
    return row["text"], json.loads(row["buttons"])


def test_filter_overview_has_flags_before_geographic_screens(archive):
    store, _ = archive
    command(store, 1, "/filtri")
    text, keyboard = last_panel(store)
    assert "Territori incerti: no" in str(keyboard)
    assert [row[0]["text"].split(":")[0] for row in keyboard[:4]] == [
        "Nazionali",
        "Territori incerti",
        "Regioni",
        "Provincia",
    ]
    command(store, 2, "flag:incerti", callback=True)
    assert "Territori incerti: sì" in str(last_panel(store)[1])
    command(store, 3, "flag:nazionali", callback=True)
    assert "Nazionali: no" in str(last_panel(store)[1])
    command(store, 4, "filters:reset", callback=True)
    assert "Territori incerti: no" in str(last_panel(store)[1])


def test_province_browsing_preserves_preferences_and_toggles_only_selected_province(
    archive,
):
    store, _ = archive
    command(store, 1, "filters:province", callback=True)
    command(store, 2, "provinces:IT-88", callback=True)
    user = rows(store, "SELECT * FROM telegram_users")[0]
    assert (
        user["filter_revision"] == 0
        and user["regions"] == "[]"
        and user["provinces"] == "[]"
    )
    _, keyboard = last_panel(store)
    assert any(b.get("callback_data") == "province:SS" for row in keyboard for b in row)
    command(store, 3, "province:SS", callback=True)
    user = rows(store, "SELECT * FROM telegram_users")[0]
    assert json.loads(user["provinces"]) == ["SS"] and user["regions"] == "[]"
    assert "✓ Sassari (SS)" in str(last_panel(store)[1])
    command(store, 4, "province:SS", callback=True)
    assert rows(store, "SELECT provinces FROM telegram_users")[0][0] == "[]"


def test_regional_and_provincial_filters_show_their_union_explicitly(archive):
    store, _ = archive
    command(store, 1, "region:IT-88", callback=True)
    command(store, 2, "province:SS", callback=True)
    assert "Per restringere alle province" in last_panel(store)[0]


def test_flags_apply_even_without_region_or_province(archive):
    store, _ = archive
    snapshot = store._latest(store._connection(), A)
    assert matches(snapshot, Preferences())
    unknown = replace(snapshot, territories=())
    assert not matches(unknown, Preferences())
    assert matches(unknown, Preferences(include_unresolved=True))
    national = replace(
        snapshot,
        territories=(
            TerritoryValue(
                "Nazionale",
                TerritoryScope.UNSPECIFIED,
                Provenance("https://www.inpa.gov.it", NOW),
                national=True,
            ),
        ),
    )
    assert matches(national, Preferences())
    assert not matches(national, Preferences(include_national=False))


def test_existing_default_is_migrated_once_without_overwriting_explicit_choices(
    tmp_path,
):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as old:
        old.execute("""CREATE TABLE telegram_users (
            user_id INTEGER PRIMARY KEY,chat_id INTEGER,admin INTEGER DEFAULT 0,
            started INTEGER DEFAULT 0,notifications INTEGER DEFAULT 0,event_cursor INTEGER DEFAULT 0,
            filter_revision INTEGER DEFAULT 0,regions TEXT DEFAULT '[]',provinces TEXT DEFAULT '[]',
            include_national INTEGER DEFAULT 1,include_unresolved INTEGER DEFAULT 1,
            keywords TEXT DEFAULT '',created_at TEXT NOT NULL)""")
        old.execute(
            "INSERT INTO telegram_users(user_id,created_at) VALUES(11,?)",
            (NOW.isoformat(),),
        )
        old.execute(
            "INSERT INTO telegram_users(user_id,created_at,filter_revision) VALUES(22,?,1)",
            (NOW.isoformat(),),
        )
        old.commit()
    with BotStore(path) as store:
        assert [
            (r[0], r[1])
            for r in rows(
                store,
                "SELECT user_id,include_unresolved FROM telegram_users ORDER BY user_id",
            )
        ] == [(11, 0), (22, 1)]
        store.configure_admins(frozenset({ADMIN}), 4, NOW)
        command(store, 1, "/approva 33")
        assert (
            rows(
                store, "SELECT include_unresolved FROM telegram_users WHERE user_id=33"
            )[0][0]
            == 0
        )
        command(store, 2, "/filtri incerti on")
    with BotStore(path) as store:
        assert (
            rows(
                store, "SELECT include_unresolved FROM telegram_users WHERE user_id=11"
            )[0][0]
            == 1
        )


def test_literal_slash_opens_menu_without_privacy_button(archive):
    store, _ = archive
    command(store, 1, "/")
    _, keyboard = last_panel(store)
    assert "Privacy" not in str(keyboard) and "menu:filtri" in str(keyboard)
