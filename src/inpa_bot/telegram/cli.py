"""Local setup, identity discovery and launching the optional Telegram runtime."""

from __future__ import annotations

import asyncio
import argparse
import getpass
import os
import tempfile
from pathlib import Path
from collections.abc import Callable

from .config import TelegramConfig, load_local_env


def configure(path: Path) -> None:
    from dotenv import dotenv_values

    current = dotenv_values(path) if path.exists() else {}
    token = getpass.getpass(
        "Token BotFather (nascosto; Invio mantiene quello esistente): "
    ).strip()
    token = token or current.get("INPA_TELEGRAM_TOKEN") or ""
    # Allow an empty admin list during the first, local identification step.
    TelegramConfig(token, frozenset({1}))
    admin_text = input(
        "ID amministratore numerico (Invio mantiene quello esistente o lo lascia da identificare): "
    ).strip()
    admin_text = admin_text or current.get("INPA_TELEGRAM_ADMIN_IDS") or ""
    if admin_text:
        TelegramConfig.from_environment(
            {"INPA_TELEGRAM_TOKEN": token, "INPA_TELEGRAM_ADMIN_IDS": admin_text}
        )
    # Preserve unrelated .env settings. dotenv parses the generated quoted values.
    values = {
        **current,
        "INPA_TELEGRAM_TOKEN": token,
        "INPA_TELEGRAM_ADMIN_IDS": admin_text,
    }
    content = "".join(
        f"{key}='{str(value or '').replace(chr(39), chr(92) + chr(39))}'\n"
        for key, value in values.items()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".telegram-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def telegram_command(
    args: argparse.Namespace, database: Path, emit: Callable[[object], None]
) -> int:
    try:
        from dotenv import load_dotenv  # noqa: F401 - verify optional dependencies first
        from .runtime import identify, poller_lock, run_bot
    except ImportError:
        raise RuntimeError(
            'Installa prima le dipendenze: python -m pip install -e ".[telegram]"'
        ) from None
    env_path = Path(args.env_file).resolve()
    if args.telegram_action == "configure":
        configure(env_path)
        emit(
            {"level": "info", "event": "telegram_configured", "env_file": str(env_path)}
        )
        return 0
    load_local_env(env_path)
    if args.telegram_action == "identify":
        token = os.environ.get("INPA_TELEGRAM_TOKEN", "")
        TelegramConfig(token, frozenset({1}))
        with poller_lock(database):
            ids = asyncio.run(identify(token))
        emit(
            {
                "private_start_user_ids": ids,
                "next": "Verifica il tuo ID e impostalo con telegram configure; poi avvia telegram run",
            }
        )
        return 0
    config = TelegramConfig.from_environment()
    asyncio.run(run_bot(database, config, emit))
    return 0
