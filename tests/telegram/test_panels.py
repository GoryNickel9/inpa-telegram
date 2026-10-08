from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")
from telegram.error import BadRequest, NetworkError  # noqa: E402

from inpa_bot.telegram.runtime import deliver_one  # noqa: E402
from inpa_bot.telegram.store import BotStore  # noqa: E402
from tests.sync.test_service import NOW  # noqa: E402
from .conftest import ADMIN, USER, command, rows  # noqa: E402


class PanelBot:
    def __init__(self):
        self.sent, self.edited = [], []
        self.edit_error = self.send_error = None

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        if self.edit_error:
            raise self.edit_error
        return SimpleNamespace(message_id=kwargs["message_id"])


def deliver(bot, store, now=NOW):
    return asyncio.run(deliver_one(bot, store, clock=lambda: now))


def test_repeated_filter_clicks_reuse_one_message_even_after_restart(archive):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/filtri")
    assert deliver(bot, store)
    command(store, 2, "menu:filtri", callback=True)
    assert deliver(bot, store)
    assert len(bot.sent) == 1 and not bot.edited
    command(store, 3, "flag:incerti", callback=True)
    assert deliver(bot, store)
    assert len(bot.sent) == 1 and bot.edited[0]["message_id"] == 101
    with BotStore(Path(store.path)) as reopened:
        command(reopened, 4, "menu:home", callback=True)
        assert deliver(bot, reopened)
    assert len(bot.sent) == 1 and bot.edited[-1]["message_id"] == 101


@pytest.mark.parametrize("text", ["/menu", "/start", "/filtri", "/"])
def test_typed_command_creates_visible_reply_even_if_previous_panel_is_identical(
    archive, text
):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, text)
    assert deliver(bot, store)
    with BotStore(Path(store.path)) as reopened:
        command(reopened, 2, text)
        assert deliver(bot, reopened)
        assert reopened.panel(ADMIN)["message_id"] == 102
        command(reopened, 3, "filters:regioni", callback=True)
        assert deliver(bot, reopened)
    assert len(bot.sent) == 2
    assert bot.edited[0]["message_id"] == 102


def test_click_overtaking_typed_command_preserves_visible_reply(archive):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/menu")
    deliver(bot, store)
    command(store, 2, "/menu")
    command(store, 3, "filters:regioni", callback=True)
    deliver(bot, store)
    assert len(bot.sent) == 2 and not bot.edited
    assert store.panel(ADMIN)["message_id"] == 102
    assert "Abruzzo" in str(bot.sent[-1]["reply_markup"])


def test_uncertain_fresh_reply_does_not_retry_or_replace_previous_panel(archive):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/menu")
    deliver(bot, store)
    command(store, 2, "/menu")
    bot.send_error = NetworkError("accepted but response lost")
    deliver(bot, store)
    assert (
        rows(store, "SELECT state FROM telegram_outbox ORDER BY id DESC LIMIT 1")[0][0]
        == "ambiguous"
    )
    assert store.panel(ADMIN)["message_id"] == 101
    assert not deliver(bot, store, NOW + timedelta(minutes=2))
    assert len(bot.sent) == 2 and not bot.edited


def test_rapid_clicks_keep_only_latest_panel_and_do_not_cancel_another_user(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, "/filtri")
    command(store, 3, "filters:regioni", callback=True)
    command(store, 4, "/filtri", uid=USER)
    panels = rows(
        store,
        "SELECT user_id,state FROM telegram_outbox WHERE category IN ('panel','panel_fresh')",
    )
    assert [(r[0], r[1]) for r in panels] == [
        (ADMIN, "cancelled"),
        (ADMIN, "queued"),
        (USER, "queued"),
    ]


def test_edit_timeout_retries_idempotently_without_new_message(archive):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/filtri")
    deliver(bot, store)
    command(store, 2, "filters:regioni", callback=True)
    bot.edit_error = NetworkError("response lost")
    deliver(bot, store)
    assert (
        rows(store, "SELECT state FROM telegram_outbox ORDER BY id DESC LIMIT 1")[0][0]
        == "queued"
    )
    assert not deliver(bot, store, NOW + timedelta(seconds=30))
    bot.edit_error = None
    assert deliver(bot, store, NOW + timedelta(seconds=61))
    assert len(bot.sent) == 1 and len(bot.edited) == 2


@pytest.mark.parametrize(
    "reason,new_messages",
    [("Message is not modified", 1), ("Message to edit not found", 2)],
)
def test_explicit_edit_rejections_are_handled_without_spurious_duplicate(
    archive, reason, new_messages
):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/filtri")
    deliver(bot, store)
    command(store, 2, "menu:home", callback=True)
    bot.edit_error = BadRequest(reason)
    deliver(bot, store)
    assert len(bot.sent) == new_messages
    assert (
        rows(store, "SELECT state FROM telegram_outbox ORDER BY id DESC LIMIT 1")[0][0]
        == "sent"
    )
    assert store.panel(ADMIN)["message_id"] == 100 + new_messages


def test_uncertain_replacement_send_is_not_automatically_retried(archive):
    store, _ = archive
    bot = PanelBot()
    command(store, 1, "/filtri")
    deliver(bot, store)
    command(store, 2, "menu:home", callback=True)
    bot.edit_error = BadRequest("Message to edit not found")
    bot.send_error = NetworkError("new message accepted but response lost")
    deliver(bot, store)
    assert (
        rows(store, "SELECT state FROM telegram_outbox ORDER BY id DESC LIMIT 1")[0][0]
        == "ambiguous"
    )
    assert not deliver(bot, store, NOW + timedelta(minutes=2))
    assert len(bot.sent) == 2


def test_deleting_user_cascades_panel_pointer(archive):
    store, _ = archive
    command(store, 1, f"/approva {USER}")
    command(store, 2, "/filtri", uid=USER)
    bot = PanelBot()
    while deliver(bot, store):
        pass
    assert store.panel(USER) is not None
    command(store, 3, f"/revoca {USER}")
    assert store.panel(USER) is None
