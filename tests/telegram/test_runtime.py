from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")
from telegram import Bot, Update  # noqa: E402
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter  # noqa: E402
from telegram.request import BaseRequest  # noqa: E402

from inpa_bot.telegram.commands import Commands  # noqa: E402
from inpa_bot.telegram.runtime import deliver_one, incoming_from_update, poller_lock  # noqa: E402
from tests.sync.test_service import NOW  # noqa: E402
from .conftest import ADMIN, command, rows  # noqa: E402
from .test_views_config import TOKEN  # noqa: E402


class FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=123)


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "sent"),
        (NetworkError("timeout after accepted"), "ambiguous"),
        (Forbidden("blocked"), "failed"),
        (BadRequest("bad"), "failed"),
        (RetryAfter(120), "queued"),
    ],
)
def test_delivery_states_and_persisted_errors(archive, error, expected):
    store, _ = archive
    command(store, 1, "/privacy")
    bot = FakeBot(error)
    assert asyncio.run(deliver_one(bot, store, clock=lambda: NOW))
    row = rows(store, "SELECT * FROM telegram_outbox")[0]
    assert row["state"] == expected and row["attempts"] == 1
    assert (
        row["error_type"] is None or "timeout after accepted" not in row["error_type"]
    )
    if expected == "ambiguous":
        assert not asyncio.run(
            deliver_one(bot, store, clock=lambda: NOW + timedelta(minutes=2))
        )


def test_invalid_token_stops_without_leaking_exception_message(archive):
    store, _ = archive
    command(store, 1, "/privacy")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(deliver_one(FakeBot(InvalidToken(TOKEN)), store, clock=lambda: NOW))
    assert TOKEN not in str(exc.value)


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, request_data.parameters if request_data else {}))
        if endpoint == "getMe":
            result = {
                "id": 123456,
                "is_bot": True,
                "first_name": "Test",
                "username": "test_bot",
            }
        elif endpoint in {"sendMessage", "editMessageText"}:
            result = {
                "message_id": 99,
                "date": 1791453600,
                "chat": {"id": ADMIN, "type": "private"},
                "text": "Reply",
            }
        elif endpoint in {"setMyCommands", "setChatMenuButton"}:
            result = True
        else:
            raise AssertionError(f"Unexpected SDK request: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


def test_actual_sdk_serializes_keyboard_without_network(archive):
    store, _ = archive
    command(store, 1, "/filtri")
    request = FakeRequest()

    async def run():
        async with Bot(
            TOKEN, request=request, get_updates_request=FakeRequest()
        ) as bot:
            assert await deliver_one(bot, store, clock=lambda: NOW)

    asyncio.run(run())
    sent = [params for method, params in request.calls if method == "sendMessage"]
    assert len(sent) == 1
    assert "inline_keyboard" in sent[0]["reply_markup"]
    assert "parse_mode" not in sent[0]
    assert rows(store, "SELECT state FROM telegram_outbox")[0][0] == "sent"


def test_sdk_update_command_roundtrip(archive):
    store, _ = archive
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1791453600,
                "from": {"id": ADMIN, "is_bot": False, "first_name": "User"},
                "chat": {"id": ADMIN, "type": "private"},
                "text": "/start",
            },
        },
        None,
    )
    incoming = incoming_from_update(update)
    assert incoming.user_id == ADMIN and incoming.private
    Commands(store).handle(incoming, NOW)
    assert store.offset() == 2


def test_same_database_has_one_poller_but_independent_sync_lock(archive):
    from pathlib import Path

    store, _ = archive
    with poller_lock(Path(store.path)):
        with pytest.raises(RuntimeError, match="altro processo"):
            with poller_lock(Path(store.path)):
                pass
        with store.synchronizing():
            pass


def test_native_command_popup_and_menu_button_are_registered_without_messages():
    from inpa_bot.telegram.runtime import register_commands

    request = FakeRequest()

    async def run():
        async with Bot(
            TOKEN, request=request, get_updates_request=FakeRequest()
        ) as bot:
            await register_commands(bot, [ADMIN])

    asyncio.run(run())
    calls = [params for name, params in request.calls if name == "setMyCommands"]
    assert len(calls) == 4
    for params in calls:
        names = {item["command"] for item in params["commands"]}
        assert "filtri" in names and "menu" in names and "privacy" not in names
        if params.get("scope"):
            assert params["scope"]["chat_id"] == ADMIN
            assert "richieste" in names
        else:
            assert "richieste" not in names
    menus = [params for name, params in request.calls if name == "setChatMenuButton"]
    assert menus[0]["menu_button"]["type"] == "commands"
    assert not any(name == "sendMessage" for name, _ in request.calls)


def test_sdk_edits_existing_filter_message_without_new_send(archive):
    store, _ = archive
    request = FakeRequest()

    async def run():
        async with Bot(
            TOKEN, request=request, get_updates_request=FakeRequest()
        ) as bot:
            command(store, 1, "/filtri")
            await deliver_one(bot, store, clock=lambda: NOW)
            command(store, 2, "filters:regioni", callback=True)
            await deliver_one(bot, store, clock=lambda: NOW)

    asyncio.run(run())
    assert len([1 for name, _ in request.calls if name == "sendMessage"]) == 1
    edits = [params for name, params in request.calls if name == "editMessageText"]
    assert edits[0]["message_id"] == 99 and edits[0]["chat_id"] == ADMIN
    assert "inline_keyboard" in edits[0]["reply_markup"]
