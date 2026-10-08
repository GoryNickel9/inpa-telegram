"""Additive M2 schema, atomic command effects/offsets and notification outbox."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from inpa_bot.source.snapshot import _utc_iso, snapshot_from_json
from inpa_bot.sync.store import SyncStore

from .views import Preferences, buttons, card, is_open, matches


class BotStore(SyncStore):
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(path)
        try:
            with self._write() as conn:
                version = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='m2_schema_version'"
                ).fetchone()
                if version and version[0] != "1":
                    raise RuntimeError("Versione schema M2 non supportata")
                for sql in (
                    """CREATE TABLE IF NOT EXISTS telegram_users (
                        user_id INTEGER PRIMARY KEY CHECK(user_id>0), chat_id INTEGER,
                        admin INTEGER NOT NULL DEFAULT 0, started INTEGER NOT NULL DEFAULT 0,
                        notifications INTEGER NOT NULL DEFAULT 0,
                        event_cursor INTEGER NOT NULL DEFAULT 0,
                        filter_revision INTEGER NOT NULL DEFAULT 0,
                        regions TEXT NOT NULL DEFAULT '[]', provinces TEXT NOT NULL DEFAULT '[]',
                        include_national INTEGER NOT NULL DEFAULT 1,
                        include_unresolved INTEGER NOT NULL DEFAULT 0,
                        keywords TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS telegram_follows (
                        user_id INTEGER NOT NULL REFERENCES telegram_users ON DELETE CASCADE,
                        notice_id TEXT NOT NULL REFERENCES notices, PRIMARY KEY(user_id,notice_id))""",
                    """CREATE TABLE IF NOT EXISTS telegram_hidden (
                        user_id INTEGER NOT NULL REFERENCES telegram_users ON DELETE CASCADE,
                        notice_id TEXT NOT NULL REFERENCES notices, PRIMARY KEY(user_id,notice_id))""",
                    """CREATE TABLE IF NOT EXISTS telegram_outbox (
                        id INTEGER PRIMARY KEY, logical_key TEXT NOT NULL UNIQUE,
                        user_id INTEGER NOT NULL REFERENCES telegram_users ON DELETE CASCADE,
                        chat_id INTEGER NOT NULL, notice_id TEXT, text TEXT NOT NULL,
                        buttons TEXT NOT NULL DEFAULT '[]', category TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'queued'
                            CHECK(state IN ('queued','sending','sent','ambiguous','failed','cancelled')),
                        attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                        created_at TEXT NOT NULL, finished_at TEXT, message_id INTEGER,
                        error_type TEXT)""",
                    """CREATE INDEX IF NOT EXISTS telegram_outbox_ready
                        ON telegram_outbox(state,available_at,id)""",
                    """CREATE TABLE IF NOT EXISTS telegram_attempts (
                        id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL
                            REFERENCES telegram_outbox ON DELETE CASCADE,
                        started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS telegram_requests (
                        user_id INTEGER PRIMARY KEY, requested_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS telegram_deletions (
                        user_id INTEGER PRIMARY KEY REFERENCES telegram_users ON DELETE CASCADE,
                        expires_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS telegram_panels (
                        user_id INTEGER PRIMARY KEY REFERENCES telegram_users ON DELETE CASCADE,
                        message_id INTEGER NOT NULL, payload_hash TEXT NOT NULL)""",
                ):
                    conn.execute(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES('m2_schema_version','1')"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES('telegram_offset','0')"
                )
                if not conn.execute(
                    "SELECT 1 FROM schema_meta WHERE key='m2_filter_defaults_v2'"
                ).fetchone():
                    # Preserve explicit preferences; change only untouched users.
                    conn.execute(
                        "UPDATE telegram_users SET include_unresolved=0 WHERE filter_revision=0"
                    )
                    conn.execute(
                        "INSERT INTO schema_meta VALUES('m2_filter_defaults_v2','1')"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES('m2_ui_schema_version','1')"
                )
        except BaseException:
            self.close()
            raise

    def configure_admins(
        self, ids: frozenset[int], max_users: int, now: datetime
    ) -> None:
        with self._write() as conn:
            if not ids or any(type(uid) is not int or uid <= 0 for uid in ids):
                raise ValueError("Serve almeno un amministratore numerico positivo")
            previous_admins = {
                row[0]
                for row in conn.execute(
                    "SELECT user_id FROM telegram_users WHERE admin=1"
                )
            }
            for removed in previous_admins - ids:
                conn.execute("DELETE FROM telegram_users WHERE user_id=?", (removed,))
            present = {r[0] for r in conn.execute("SELECT user_id FROM telegram_users")}
            if len(present | ids) > max_users:
                raise ValueError("Troppi utenti autorizzati rispetto a max_users")
            conn.execute("UPDATE telegram_users SET admin=0")
            for uid in ids:
                conn.execute(
                    "INSERT INTO telegram_users(user_id,admin,include_unresolved,created_at) VALUES(?,1,0,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET admin=1",
                    (uid, _utc_iso(now)),
                )

    @staticmethod
    def prefs(user: sqlite3.Row) -> Preferences:
        return Preferences(
            tuple(json.loads(user["regions"])),
            tuple(json.loads(user["provinces"])),
            bool(user["include_national"]),
            bool(user["include_unresolved"]),
            user["keywords"],
        )

    @staticmethod
    def enqueue(
        conn: sqlite3.Connection,
        key: str,
        uid: int,
        chat_id: int,
        text: str,
        now: datetime,
        *,
        category: str = "reply",
        notice_id: str | None = None,
        markup: list[list[dict[str, str]]] | None = None,
    ) -> None:
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Messaggio oltre il limite Telegram")
        if category in {"panel", "panel_fresh"}:
            # A click may overtake a typed command before delivery. Preserve the
            # pending request for a visible reply at the bottom of the chat.
            if conn.execute(
                "SELECT 1 FROM telegram_outbox WHERE user_id=? "
                "AND category='panel_fresh' AND state='queued' LIMIT 1",
                (uid,),
            ).fetchone():
                category = "panel_fresh"
            conn.execute(
                "UPDATE telegram_outbox SET state='cancelled',finished_at=? "
                "WHERE user_id=? AND category IN ('panel','panel_fresh') AND state='queued'",
                (_utc_iso(now), uid),
            )
        conn.execute(
            "INSERT OR IGNORE INTO telegram_outbox(logical_key,user_id,chat_id,notice_id,text,buttons,"
            "category,available_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key,
                uid,
                chat_id,
                notice_id,
                text,
                json.dumps(markup or []),
                category,
                _utc_iso(now),
                _utc_iso(now),
            ),
        )

    def apply_update(
        self, update_id: int, handler: Callable[[sqlite3.Connection], None]
    ) -> bool:
        with self._write() as conn:
            offset = int(
                conn.execute(
                    "SELECT value FROM schema_meta WHERE key='telegram_offset'"
                ).fetchone()[0]
            )
            if update_id < offset:
                return False
            handler(conn)
            conn.execute(
                "UPDATE schema_meta SET value=? WHERE key='telegram_offset'",
                (str(update_id + 1),),
            )
            return True

    def offset(self) -> int:
        with self._read() as conn:
            return int(
                conn.execute(
                    "SELECT value FROM schema_meta WHERE key='telegram_offset'"
                ).fetchone()[0]
            )

    def bind_bot(self, bot_id: int) -> None:
        with self._write() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='telegram_bot_id'"
            ).fetchone()
            if row and int(row[0]) != bot_id:
                raise ValueError("Questo archivio appartiene a un altro bot Telegram")
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta VALUES('telegram_bot_id',?)",
                (str(bot_id),),
            )

    @staticmethod
    def panel_hash(text: str, markup_json: str) -> str:
        return hashlib.sha256((text + "\0" + markup_json).encode()).hexdigest()

    def panel(self, uid: int) -> sqlite3.Row | None:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM telegram_panels WHERE user_id=?", (uid,)
            ).fetchone()

    def active_admin_chats(self) -> list[int]:
        with self._read() as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT chat_id FROM telegram_users WHERE admin=1 AND started=1"
                )
            ]

    def collect_events(self, now: datetime, limit: int = 200) -> int:
        """Per-user event cursor and resulting deliveries commit together."""
        queued = 0
        with self._write() as conn:
            for user in conn.execute(
                "SELECT * FROM telegram_users WHERE started=1 AND notifications=1"
            ).fetchall():
                events = conn.execute(
                    "SELECT * FROM sync_events WHERE id>? ORDER BY id LIMIT ?",
                    (user["event_cursor"], limit),
                ).fetchall()
                for event in events:
                    if event["kind"] != "baseline":
                        row = conn.execute(
                            "SELECT payload FROM notice_versions WHERE notice_id=? AND version_no=?",
                            (event["notice_id"], event["version_no"]),
                        ).fetchone()
                        if row is None:
                            raise RuntimeError("Evento senza versione del bando")
                        snapshot = snapshot_from_json(row[0])
                        hidden = conn.execute(
                            "SELECT 1 FROM telegram_hidden WHERE user_id=? AND notice_id=?",
                            (user["user_id"], snapshot.notice_id),
                        ).fetchone()
                        followed = conn.execute(
                            "SELECT 1 FROM telegram_follows WHERE user_id=? AND notice_id=?",
                            (user["user_id"], snapshot.notice_id),
                        ).fetchone()
                        relevant = matches(snapshot, self.prefs(user)) or bool(followed)
                        if (
                            not hidden
                            and relevant
                            and (event["kind"] == "changed" or is_open(snapshot, now))
                        ):
                            self.enqueue(
                                conn,
                                f"event:{user['user_id']}:{event['id']}",
                                user["user_id"],
                                user["chat_id"],
                                card(
                                    snapshot,
                                    "Nuovo bando"
                                    if event["kind"] == "new"
                                    else "Bando aggiornato",
                                ),
                                now,
                                category="notice",
                                notice_id=snapshot.notice_id,
                                markup=buttons(snapshot.notice_id),
                            )
                            queued += 1
                    conn.execute(
                        "UPDATE telegram_users SET event_cursor=? WHERE user_id=?",
                        (event["id"], user["user_id"]),
                    )
        return queued

    def recover_sending(self, now: datetime) -> int:
        """A crashed send may have reached Telegram: don't retry it automatically."""
        with self._write() as conn:
            result = conn.execute(
                "UPDATE telegram_outbox SET state='ambiguous',finished_at=?,error_type='ProcessInterrupted' "
                "WHERE state='sending'",
                (_utc_iso(now),),
            )
            conn.execute(
                "UPDATE telegram_attempts SET outcome='ambiguous',finished_at=? WHERE outcome='sending'",
                (_utc_iso(now),),
            )
            return result.rowcount

    def claim(self, now: datetime) -> sqlite3.Row | None:
        with self._write() as conn:
            cooldown = conn.execute(
                "SELECT value FROM schema_meta WHERE key='telegram_retry_not_before'"
            ).fetchone()
            if cooldown and cooldown[0] > _utc_iso(now):
                return None
            row = conn.execute(
                "SELECT o.* FROM telegram_outbox o JOIN telegram_users u ON o.user_id=u.user_id "
                "WHERE o.state='queued' AND o.available_at<=? AND u.started=1 "
                "ORDER BY o.id LIMIT 1",
                (_utc_iso(now),),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE telegram_outbox SET state='sending',attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                conn.execute(
                    "INSERT INTO telegram_attempts(outbox_id,started_at,outcome) VALUES(?,?,'sending')",
                    (row["id"], _utc_iso(now)),
                )
            return row

    def finish_delivery(
        self,
        delivery_id: int,
        state: str,
        now: datetime,
        *,
        message_id: int | None = None,
        error_type: str | None = None,
        retry_seconds: float | None = None,
    ) -> None:
        if state not in {"sent", "ambiguous", "failed", "queued"}:
            raise ValueError("Stato di consegna non valido")
        with self._write() as conn:
            deadline = _utc_iso(now + timedelta(seconds=retry_seconds or 0))
            delivery = conn.execute(
                "SELECT * FROM telegram_outbox WHERE id=? AND state='sending'",
                (delivery_id,),
            ).fetchone()
            if delivery is None:
                return
            conn.execute(
                "UPDATE telegram_outbox SET state=?,message_id=?,error_type=?,finished_at=?,available_at=? "
                "WHERE id=? AND state='sending'",
                (state, message_id, error_type, _utc_iso(now), deadline, delivery_id),
            )
            conn.execute(
                "UPDATE telegram_attempts SET outcome=?,finished_at=? WHERE outbox_id=? AND outcome='sending'",
                (state, _utc_iso(now), delivery_id),
            )
            if (
                state == "sent"
                and message_id is not None
                and delivery["category"] in {"panel", "panel_fresh"}
            ):
                conn.execute(
                    "INSERT INTO telegram_panels VALUES(?,?,?) ON CONFLICT(user_id) "
                    "DO UPDATE SET message_id=excluded.message_id,payload_hash=excluded.payload_hash",
                    (
                        delivery["user_id"],
                        message_id,
                        self.panel_hash(delivery["text"], delivery["buttons"]),
                    ),
                )
            if retry_seconds:
                conn.execute(
                    "INSERT INTO schema_meta VALUES('telegram_retry_not_before',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                    (deadline,),
                )

    def delivery_counts(self) -> dict[str, int]:
        with self._read() as conn:
            return {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT state,COUNT(*) FROM telegram_outbox GROUP BY state"
                )
            }

    def prune(self, now: datetime) -> None:
        with self._write() as conn:
            cutoff = _utc_iso(now - timedelta(days=7))
            conn.execute(
                "DELETE FROM telegram_outbox WHERE state IN ('sent','failed','cancelled') AND finished_at<?",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM telegram_requests WHERE requested_at<?", (cutoff,)
            )
            conn.execute(
                "DELETE FROM telegram_deletions WHERE expires_at<?", (_utc_iso(now),)
            )
