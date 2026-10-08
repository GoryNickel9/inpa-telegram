"""Long polling and delivery workers, isolated from inPA acquisition."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from .commands import Commands, Incoming
from .config import TelegramConfig
from .store import BotStore

PUBLIC_COMMANDS = [
    BotCommand("menu", "Apri il menu principale"),
    BotCommand("concorsi", "Cerca i concorsi aperti"),
    BotCommand("filtri", "Scegli territori e preferenze"),
    BotCommand("seguiti", "Ritrova i bandi salvati"),
    BotCommand("notifiche", "Attiva o disattiva gli avvisi"),
    BotCommand("stato", "Controlla gli aggiornamenti"),
    BotCommand("help", "Come usare il bot"),
    BotCommand("start", "Avvia il bot o richiedi accesso"),
    BotCommand("elimina", "Cancella i tuoi dati, con conferma"),
]


async def register_commands(bot: Bot, admin_chats: list[int]) -> None:
    """Configure Telegram's native slash popup and Menu button, without messages."""
    for language in ("", "it"):
        await bot.set_my_commands(PUBLIC_COMMANDS, language_code=language)
        for chat_id in admin_chats:
            await bot.set_my_commands(
                PUBLIC_COMMANDS
                + [
                    BotCommand("richieste", "Approva gli accessi al bot"),
                    BotCommand("consegne", "Controlla gli invii"),
                ],
                scope=BotCommandScopeChat(chat_id),
                language_code=language,
            )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def poller_lock(database: Path) -> Iterator[None]:
    """A separate lock lets the M1 synchronizer continue on the same SQLite file."""
    handle = open(str(database.resolve()) + ".telegram.lock", "a+b")
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "Un altro processo Telegram usa questo archivio"
            ) from None
        acquired = True
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def incoming_from_update(update: Update) -> Incoming | None:
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or user.is_bot:
        return None
    if update.callback_query:
        text = update.callback_query.data or ""
    elif update.message:
        text = update.message.text or ""
    else:
        return None
    return Incoming(
        update.update_id,
        user.id,
        chat.id,
        chat.type == "private",
        text,
        callback=update.callback_query is not None,
    )


async def deliver_one(
    bot: Bot, store: BotStore, *, clock: Callable[[], datetime] = now
) -> bool:
    delivery = store.claim(clock())
    if delivery is None:
        return False
    panel = (
        store.panel(delivery["user_id"]) if delivery["category"] == "panel" else None
    )
    try:
        markup = json.loads(delivery["buttons"])
        payload = {
            "chat_id": delivery["chat_id"],
            "text": delivery["text"],
            "reply_markup": InlineKeyboardMarkup(
                [[InlineKeyboardButton(**button) for button in row] for row in markup]
            )
            if markup
            else None,
        }
        message_id = None
        if panel:
            message_id = panel["message_id"]
            if panel["payload_hash"] != store.panel_hash(
                delivery["text"], delivery["buttons"]
            ):
                try:
                    await bot.edit_message_text(message_id=message_id, **payload)
                except BadRequest as exc:
                    reason = str(exc).casefold()
                    if "message is not modified" in reason:
                        pass  # The target already contains this panel after an interrupted edit.
                    elif any(
                        part in reason
                        for part in (
                            "message to edit not found",
                            "message can't be edited",
                            "message_id_invalid",
                        )
                    ):
                        message_id = (
                            None  # Explicit rejection; a replacement panel is safe.
                        )
                        panel = None  # A failed new send is uncertain, unlike an idempotent edit.
                    else:
                        raise
        if message_id is None:
            message = await bot.send_message(**payload)
            message_id = message.message_id
    except RetryAfter as exc:
        seconds = (
            exc.retry_after.total_seconds()
            if isinstance(exc.retry_after, timedelta)
            else float(exc.retry_after)
        )
        store.finish_delivery(
            delivery["id"],
            "queued",
            clock(),
            error_type="RetryAfter",
            retry_seconds=max(1, seconds),
        )
    except (Forbidden, BadRequest):
        store.finish_delivery(
            delivery["id"], "failed", clock(), error_type="RejectedByTelegram"
        )
    except InvalidToken:
        store.finish_delivery(
            delivery["id"], "failed", clock(), error_type="InvalidToken"
        )
        raise RuntimeError(
            "Telegram ha rifiutato il token; verifica la configurazione"
        ) from None
    except (NetworkError, TelegramError):
        # Transport failure after a send does not prove the server rejected it.
        store.finish_delivery(
            delivery["id"],
            "queued" if panel else "ambiguous",
            clock(),
            error_type="PanelEditRetry" if panel else "UncertainDelivery",
            retry_seconds=60,
        )
    else:
        store.finish_delivery(delivery["id"], "sent", clock(), message_id=message_id)
    return True


async def _pause(stopped: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stopped.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _poll(
    bot: Bot,
    store: BotStore,
    commands: Commands,
    stopped: asyncio.Event,
    emit: Callable[[object], None],
) -> None:
    backoff = 1
    while not stopped.is_set():
        try:
            updates = await bot.get_updates(
                offset=store.offset(),
                timeout=20,
                allowed_updates=["message", "callback_query"],
                limit=50,
            )
            backoff = 1
        except RetryAfter as exc:
            seconds = (
                exc.retry_after.total_seconds()
                if isinstance(exc.retry_after, timedelta)
                else float(exc.retry_after)
            )
            await _pause(stopped, max(1, seconds))
            continue
        except (Conflict, InvalidToken, Forbidden):
            raise RuntimeError(
                "Polling Telegram rifiutato: verifica token, webhook e processi concorrenti"
            ) from None
        except NetworkError:
            emit(
                {
                    "level": "warning",
                    "event": "telegram_poll_retry",
                    "retry_seconds": backoff,
                }
            )
            await _pause(stopped, backoff)
            backoff = min(60, backoff * 2)
            continue
        for update in updates:
            if stopped.is_set():
                break
            incoming = incoming_from_update(update)
            ack = (
                commands.handle(incoming, now()) if incoming else "Operazione ricevuta"
            )
            if incoming is None:
                store.apply_update(update.update_id, lambda conn: None)
            if update.callback_query:
                try:
                    await bot.answer_callback_query(update.callback_query.id, text=ack)
                except TelegramError:
                    pass  # Ephemeral acknowledgement; durable effects and reply are already saved.


async def _deliver(
    bot: Bot, store: BotStore, stopped: asyncio.Event, emit: Callable[[object], None]
) -> None:
    next_housekeeping = now()
    while not stopped.is_set():
        store.collect_events(now())
        if now() >= next_housekeeping:
            store.prune(now())
            emit(
                {
                    "level": "info",
                    "event": "telegram_outbox",
                    "counts": store.delivery_counts(),
                }
            )
            next_housekeeping = now() + timedelta(minutes=5)
        await deliver_one(bot, store)
        await _pause(stopped, 1)  # Four users don't need burst sending.


async def run_bot(
    database: Path, config: TelegramConfig, emit: Callable[[object], None]
) -> None:
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    stopped = asyncio.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda signum, frame: stopped.set())
        with poller_lock(database), BotStore(database) as store:
            async with Bot(config.token) as bot:
                webhook = await bot.get_webhook_info()
                if webhook.url:
                    raise RuntimeError(
                        "Il bot ha un webhook attivo; rimuovilo prima di usare il polling"
                    )
                store.bind_bot(bot.id)
                store.configure_admins(config.admin_ids, config.max_users, now())
                await register_commands(bot, store.active_admin_chats())
                recovered = store.recover_sending(now())
                emit(
                    {
                        "level": "info",
                        "event": "telegram_started",
                        "recovered_ambiguous": recovered,
                    }
                )
                tasks = [
                    asyncio.create_task(
                        _poll(
                            bot,
                            store,
                            Commands(store, max_users=config.max_users),
                            stopped,
                            emit,
                        )
                    ),
                    asyncio.create_task(_deliver(bot, store, stopped, emit)),
                ]
                try:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                finally:
                    stopped.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except TelegramError:
        # Never propagate SDK exceptions containing the credential-bearing request URL.
        raise RuntimeError(
            "Connessione Telegram non riuscita; verifica configurazione e rete"
        ) from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def identify(token: str) -> list[int]:
    """Read private /start requests to help the operator configure numeric IDs.

    No messages are sent and no offset is acknowledged. Only run before the
    regular poller. This intentionally does not choose an administrator.
    """
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        async with Bot(token) as bot:
            if (await bot.get_webhook_info()).url:
                raise RuntimeError(
                    "Webhook attivo: identifica l'utente prima di attivare il webhook"
                )
            updates = await bot.get_updates(
                timeout=20, allowed_updates=["message", "callback_query"]
            )
            return sorted(
                {
                    incoming.user_id
                    for update in updates
                    if (incoming := incoming_from_update(update))
                    and incoming.private
                    and incoming.user_id == incoming.chat_id
                    and incoming.text.strip() == "/start"
                }
            )
    except TelegramError:
        raise RuntimeError(
            "Identificazione Telegram non riuscita; verifica token e rete"
        ) from None
