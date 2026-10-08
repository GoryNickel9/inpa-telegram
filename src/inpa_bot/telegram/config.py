"""Local secrets and fail-closed Telegram configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token: str = field(repr=False)
    admin_ids: frozenset[int]
    max_users: int = 4

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", self.token):
            raise ValueError(
                "Token Telegram mancante o non valido; usa telegram configure"
            )
        if not self.admin_ids or any(
            type(i) is not int or i <= 0 for i in self.admin_ids
        ):
            raise ValueError("Configura almeno un ID amministratore numerico positivo")
        if not 1 <= self.max_users <= 10 or len(self.admin_ids) > self.max_users:
            raise ValueError(
                "max_users deve essere 1..10 e includere tutti gli amministratori"
            )

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> TelegramConfig:
        env = os.environ if values is None else values
        raw = env.get("INPA_TELEGRAM_ADMIN_IDS", "")
        try:
            admins = frozenset(
                int(item.strip()) for item in raw.split(",") if item.strip()
            )
            maximum = int(env.get("INPA_TELEGRAM_MAX_USERS", "4"))
        except ValueError:
            raise ValueError(
                "Gli ID amministratore e max_users devono essere numerici"
            ) from None
        return cls(env.get("INPA_TELEGRAM_TOKEN", ""), admins, maximum)


def load_local_env(path: Path) -> None:
    # Optional dependency: M0/M1 remain usable without Telegram packages.
    from dotenv import load_dotenv

    load_dotenv(path, override=False)
