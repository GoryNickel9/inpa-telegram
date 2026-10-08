"""Private-chat commands; database effects and replies share the update transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from inpa_bot.source.snapshot import _utc_iso
from inpa_bot.source.territory import REGIONS, find_province, find_region

from .store import BotStore
from .filter_ui import filter_screen
from .views import buttons, card, clean, is_open, matches, notice_id_from_url

PAGE_SIZE = 5


class Reply(Protocol):
    def __call__(
        self,
        text: str,
        markup: list[list[dict[str, str]]] | None = None,
        notice_id: str | None = None,
        *,
        panel: bool = False,
    ) -> None: ...


HELP = (
    "Filtri: scegli i territori.\n"
    "Concorsi: cerca e salva con Segui.\n"
    "Notifiche: attiva gli avvisi.\n"
    "Puoi incollare un link inPA.\n"
    "/elimina cancella i tuoi dati.\n"
    "Requisiti personali: verifica il bando."
)

MENU_COMMANDS = {
    "home": "/menu",
    "concorsi": "/concorsi",
    "filtri": "/filtri",
    "seguiti": "/seguiti",
    "notifiche": "/notifiche",
    "stato": "/stato",
    "richieste": "/richieste",
}
BACK_MENU = [[{"text": "Menu", "callback_data": "menu:home"}]]


def main_menu(*, admin: bool) -> list[list[dict[str, str]]]:
    labels = [
        ("Concorsi", "concorsi"),
        ("Filtri", "filtri"),
        ("Seguiti", "seguiti"),
        ("Notifiche", "notifiche"),
        ("Stato", "stato"),
    ]
    rows = [
        [
            {"text": label, "callback_data": f"menu:{key}"}
            for label, key in labels[i : i + 2]
        ]
        for i in range(0, len(labels), 2)
    ]
    if admin:
        rows.append(
            [{"text": "Richieste di accesso", "callback_data": "menu:richieste"}]
        )
    return rows


PRIVACY = (
    "Conserviamo localmente il tuo ID Telegram, chat privata, filtri, bandi seguiti/nascosti "
    "e stato delle notifiche. Chi gestisce il PC può leggere questo database. "
    "Telegram riceve i messaggi inviati e gestisce i dati secondo le proprie condizioni. "
    "Non raccogliamo documenti personali, credenziali SPID/CIE o dati sanitari. "
    "Non inviamo profili a modelli esterni.\n"
    "Le consegne concluse restano al massimo 7 giorni; quelle pendenti o ambigue restano "
    "finché vengono gestite o elimini i tuoi dati. Le richieste di accesso scadono dopo 7 giorni. "
    "Chi gestisce il servizio può conservare backup separati: chiedigli i tempi di conservazione. "
    "La cancellazione dal database attivo non rimuove subito le copie di backup. "
    "/elimina rimuove i tuoi dati dal database, senza cancellare i bandi pubblici. "
    "I messaggi già ricevuti o in corso di invio su Telegram non vengono cancellati. "
    "Un amministratore configurato può riottenere l'accesso al riavvio."
)


@dataclass(frozen=True, slots=True)
class Incoming:
    update_id: int
    user_id: int
    chat_id: int
    private: bool
    text: str
    callback: bool = False


class Commands:
    def __init__(self, store: BotStore, *, max_users: int = 4) -> None:
        self.store = store
        self.max_users = max_users

    def handle(self, incoming: Incoming, now: datetime) -> str:
        acknowledgement = "Operazione ricevuta"

        def process(conn: sqlite3.Connection) -> None:
            nonlocal acknowledgement
            if (
                not incoming.private
                or incoming.chat_id != incoming.user_id
                or incoming.user_id <= 0
            ):
                return
            uid = incoming.user_id
            user = conn.execute(
                "SELECT * FROM telegram_users WHERE user_id=?", (uid,)
            ).fetchone()
            if user is None:
                if incoming.text.strip() == "/start":
                    count = conn.execute(
                        "SELECT COUNT(*) FROM telegram_requests"
                    ).fetchone()[0]
                    if count < 100:
                        conn.execute(
                            "INSERT OR IGNORE INTO telegram_requests VALUES(?,?)",
                            (uid, _utc_iso(now)),
                        )
                return
            # The chat binding is derived from a private message, never from a command argument.
            first_start = not user["started"]
            if first_start:
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM sync_events"
                ).fetchone()[0]
                conn.execute(
                    "UPDATE telegram_users SET started=1,chat_id=?,event_cursor=? WHERE user_id=?",
                    (uid, maximum, uid),
                )
                user = conn.execute(
                    "SELECT * FROM telegram_users WHERE user_id=?", (uid,)
                ).fetchone()
            reply_index = 0

            def reply(
                text: str,
                markup: list[list[dict[str, str]]] | None = None,
                notice_id: str | None = None,
                *,
                panel: bool = False,
            ) -> None:
                nonlocal reply_index
                self.store.enqueue(
                    conn,
                    f"reply:{incoming.update_id}:{reply_index}",
                    uid,
                    uid,
                    text,
                    now,
                    notice_id=notice_id,
                    markup=markup,
                    category=("panel" if incoming.callback else "panel_fresh")
                    if panel
                    else "reply",
                )
                reply_index += 1

            text = incoming.text.strip()
            if incoming.callback:
                action, _, argument = text.partition(":")
                text = {
                    "menu": MENU_COMMANDS.get(argument, "/menu"),
                    "approve": f"/approva {argument}",
                    "page": f"/concorsi {argument}",
                    "saved": f"/seguiti {argument}",
                    "follow": f"/segui {argument}",
                    "hide": f"/nascondi {argument}",
                    "unfollow": f"/nonseguire {argument}",
                    "region": f"/filtri toggle {argument}",
                    "notifications": f"/notifiche {argument}",
                    "delete": f"/elimina {argument}",
                    "filters": f"/filtri {argument}",
                    "provinces": f"/filtri province {argument}",
                    "province": f"/filtri toggle_provincia {argument}",
                    "flag": f"/filtri {argument} toggle",
                }.get(action, "/help")
            command, _, argument = text.partition(" ")
            command = command.split("@", 1)[0].casefold()
            argument = argument.strip()
            try:
                if command in {"/start", "/help", "/menu", "/"}:
                    reply(
                        HELP if command == "/help" else "Concorsi inPA",
                        main_menu(admin=bool(user["admin"])),
                        panel=True,
                    )
                elif command == "/filtri":
                    self._filters(conn, user, argument, now, reply)
                elif command in {"/concorsi", "/seguiti"}:
                    offset = int(argument or "0")
                    if not 0 <= offset <= 100_000:
                        raise ValueError("Numero pagina non valido")
                    self._page(
                        conn, user, offset, now, reply, saved=command == "/seguiti"
                    )
                elif command in {"/segui", "/nascondi", "/nonseguire"}:
                    snapshot = self.store._latest(conn, argument)
                    if snapshot is None:
                        raise ValueError("Bando non presente nell'archivio")
                    if command == "/segui":
                        conn.execute(
                            "INSERT OR IGNORE INTO telegram_follows VALUES(?,?)",
                            (uid, argument),
                        )
                        conn.execute(
                            "DELETE FROM telegram_hidden WHERE user_id=? AND notice_id=?",
                            (uid, argument),
                        )
                        reply("Seguito. Avvisi se le notifiche sono attive.")
                    elif command == "/nonseguire":
                        conn.execute(
                            "DELETE FROM telegram_follows WHERE user_id=? AND notice_id=?",
                            (uid, argument),
                        )
                        reply("Rimosso dai seguiti.")
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO telegram_hidden VALUES(?,?)",
                            (uid, argument),
                        )
                        conn.execute(
                            "DELETE FROM telegram_follows WHERE user_id=? AND notice_id=?",
                            (uid, argument),
                        )
                        conn.execute(
                            "UPDATE telegram_outbox SET state='cancelled',finished_at=? "
                            "WHERE user_id=? AND notice_id=? AND category='notice' AND state='queued'",
                            (_utc_iso(now), uid, argument),
                        )
                        reply("Bando nascosto.")
                elif command == "/notifiche":
                    if argument and argument not in {"on", "off"}:
                        raise ValueError("Usa /notifiche on oppure /notifiche off")
                    if argument and int(argument == "on") != user["notifications"]:
                        cursor = conn.execute(
                            "SELECT COALESCE(MAX(id),0) FROM sync_events"
                        ).fetchone()[0]
                        conn.execute(
                            "UPDATE telegram_users SET notifications=?,event_cursor=? WHERE user_id=?",
                            (int(argument == "on"), cursor, uid),
                        )
                        self._cancel_notices(conn, uid, now)
                    enabled = conn.execute(
                        "SELECT notifications FROM telegram_users WHERE user_id=?",
                        (uid,),
                    ).fetchone()[0]
                    reply(
                        f"Notifiche {'attive' if enabled else 'disattivate'}.\n"
                        "Avvisi dall'attivazione in poi.",
                        [
                            [
                                {"text": "Attiva", "callback_data": "notifications:on"},
                                {
                                    "text": "Disattiva",
                                    "callback_data": "notifications:off",
                                },
                            ]
                        ]
                        + BACK_MENU,
                        panel=True,
                    )
                elif command == "/stato":
                    last = conn.execute(
                        "SELECT finished_at FROM sync_batches WHERE phase='complete' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM sync_jobs WHERE done=0"
                    ).fetchone()[0]
                    last_local = (
                        datetime.fromisoformat(last[0])
                        .astimezone(ZoneInfo("Europe/Rome"))
                        .strftime("%d/%m/%Y %H:%M %Z")
                        if last
                        else "mai"
                    )
                    reply(
                        f"Ultimo aggiornamento: {last_local}\n"
                        f"Bandi da acquisire: {pending}",
                        BACK_MENU,
                        panel=True,
                    )
                elif command == "/privacy":
                    reply(PRIVACY, BACK_MENU, panel=True)
                elif command == "/elimina":
                    if argument == "confirm" and incoming.callback:
                        confirmation = conn.execute(
                            "SELECT expires_at FROM telegram_deletions WHERE user_id=?",
                            (uid,),
                        ).fetchone()
                        if confirmation is None or confirmation[0] < _utc_iso(now):
                            raise ValueError(
                                "Conferma scaduta: usa nuovamente /elimina"
                            )
                        conn.execute(
                            "DELETE FROM telegram_users WHERE user_id=?", (uid,)
                        )
                        conn.execute(
                            "DELETE FROM telegram_requests WHERE user_id=?", (uid,)
                        )
                        acknowledgement = "I tuoi dati locali sono stati cancellati"
                    else:
                        conn.execute(
                            "INSERT INTO telegram_deletions VALUES(?,?) ON CONFLICT(user_id) "
                            "DO UPDATE SET expires_at=excluded.expires_at",
                            (uid, _utc_iso(now + timedelta(minutes=5))),
                        )
                        reply(
                            "Confermi la cancellazione di filtri, seguiti e notifiche? "
                            "La conferma scade tra 5 minuti. I messaggi su Telegram restano.",
                            [
                                [
                                    {
                                        "text": "Conferma cancellazione",
                                        "callback_data": "delete:confirm",
                                    }
                                ]
                            ],
                        )
                elif command in {"/richieste", "/approva", "/revoca", "/consegne"}:
                    self._admin(conn, user, command, argument, now, reply)
                elif text.startswith("https://"):
                    notice_id = notice_id_from_url(text)
                    snapshot = self.store._latest(conn, notice_id)
                    if snapshot is None:
                        reply("Bando non in archivio. Consulta il link su inPA.")
                    else:
                        reply(card(snapshot), buttons(notice_id), notice_id)
                else:
                    reply(
                        "Scegli un pulsante per continuare.",
                        main_menu(admin=bool(user["admin"])),
                        panel=True,
                    )
            except (ValueError, OverflowError) as exc:
                reply(clean(str(exc), 500))

        self.store.apply_update(incoming.update_id, process)
        return acknowledgement

    @staticmethod
    def _cancel_notices(conn: sqlite3.Connection, uid: int, now: datetime) -> None:
        conn.execute(
            "UPDATE telegram_outbox SET state='cancelled',finished_at=? "
            "WHERE user_id=? AND category='notice' AND state='queued'",
            (_utc_iso(now), uid),
        )

    def _filters(
        self,
        conn: sqlite3.Connection,
        user: sqlite3.Row,
        argument: str,
        now: datetime,
        reply: Reply,
    ) -> None:
        uid = user["user_id"]
        kind, _, value = argument.partition(" ")
        value = value.strip()
        if not argument or kind in {"regioni", "province"}:
            text, markup = filter_screen(self.store.prefs(user), kind, value)
            reply(text, markup, panel=True)
            return
        section, region = "", ""
        if argument:
            if kind in {"regione", "provincia"}:
                resolver = find_region if kind == "regione" else find_province
                values = [
                    resolver(item.strip()) for item in value.split(",") if item.strip()
                ]
                if not values or any(item is None for item in values):
                    raise ValueError(
                        "Territorio non riconosciuto: separa i nomi o codici con una virgola"
                    )
                column = "regions" if kind == "regione" else "provinces"
                conn.execute(
                    f"UPDATE telegram_users SET {column}=? WHERE user_id=?",
                    (json.dumps(sorted({item.code for item in values})), uid),
                )
            elif kind == "toggle" and value in {r.code for r in REGIONS}:
                codes = set(json.loads(user["regions"]))
                codes.symmetric_difference_update({value})
                conn.execute(
                    "UPDATE telegram_users SET regions=? WHERE user_id=?",
                    (json.dumps(sorted(codes)), uid),
                )
                section = "regioni"
            elif kind == "toggle_provincia":
                province = find_province(value)
                if province is None:
                    raise ValueError("Provincia non riconosciuta")
                codes = set(json.loads(user["provinces"]))
                codes.symmetric_difference_update({province.code})
                conn.execute(
                    "UPDATE telegram_users SET provinces=? WHERE user_id=?",
                    (json.dumps(sorted(codes)), uid),
                )
                section, region = "province", province.region_code
            elif kind == "parole" and len(value) <= 200:
                conn.execute(
                    "UPDATE telegram_users SET keywords=? WHERE user_id=?", (value, uid)
                )
            elif kind in {"nazionali", "incerti"} and value in {"on", "off", "toggle"}:
                column = (
                    "include_national" if kind == "nazionali" else "include_unresolved"
                )
                conn.execute(
                    f"UPDATE telegram_users SET {column}=? WHERE user_id=?",
                    (
                        int(not user[column])
                        if value == "toggle"
                        else int(value == "on"),
                        uid,
                    ),
                )
            elif kind == "reset":
                conn.execute(
                    "UPDATE telegram_users SET regions='[]',provinces='[]',keywords='',"
                    "include_national=1,include_unresolved=0 WHERE user_id=?",
                    (uid,),
                )
            else:
                raise ValueError(
                    "Usa /filtri regione Sardegna, Lazio; /filtri provincia SS; "
                    "/filtri parole amministrativo; /filtri nazionali on|off; "
                    "/filtri incerti on|off; /filtri reset"
                )
            cursor = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM sync_events"
            ).fetchone()[0]
            conn.execute(
                "UPDATE telegram_users SET filter_revision=filter_revision+1,event_cursor=? WHERE user_id=?",
                (cursor, uid),
            )
            self._cancel_notices(conn, uid, now)
        current = conn.execute(
            "SELECT * FROM telegram_users WHERE user_id=?", (uid,)
        ).fetchone()
        text, markup = filter_screen(self.store.prefs(current), section, region)
        reply(text, markup, panel=True)

    def _page(
        self,
        conn: sqlite3.Connection,
        user: sqlite3.Row,
        offset: int,
        now: datetime,
        reply: Reply,
        *,
        saved: bool = False,
    ) -> None:
        uid = user["user_id"]
        if saved:
            ids = conn.execute(
                "SELECT notice_id FROM telegram_follows WHERE user_id=? ORDER BY notice_id",
                (uid,),
            )
        else:
            ids = conn.execute(
                "SELECT notice_id FROM notices WHERE notice_id NOT IN "
                "(SELECT notice_id FROM telegram_hidden WHERE user_id=?) ORDER BY notice_id",
                (uid,),
            )
        candidates = []
        for row in ids:
            snapshot = self.store._latest(conn, row[0])
            if snapshot and (
                saved
                or is_open(snapshot, now)
                and matches(snapshot, self.store.prefs(user))
            ):
                candidates.append(snapshot)
        if not candidates:
            reply(
                "Nessun bando seguito."
                if saved
                else "Nessun bando aperto. Modifica i filtri.",
                BACK_MENU,
            )
            return
        if offset >= len(candidates):
            raise ValueError(
                "Pagina non più disponibile: riparti con /concorsi o /seguiti"
            )
        reply(
            f"{'Seguiti' if saved else 'Bandi aperti'}: {len(candidates)}. "
            f"Risultati {offset + 1}–{min(offset + PAGE_SIZE, len(candidates))}.",
            BACK_MENU,
        )
        for snapshot in candidates[offset : offset + PAGE_SIZE]:
            markup = buttons(snapshot.notice_id)
            if saved:
                markup.append(
                    [
                        {
                            "text": "Non seguire più",
                            "callback_data": f"unfollow:{snapshot.notice_id}",
                        }
                    ]
                )
            reply(card(snapshot), markup, snapshot.notice_id)
        navigation = []
        prefix = "saved" if saved else "page"
        if offset:
            navigation.append(
                {
                    "text": "Precedenti",
                    "callback_data": f"{prefix}:{max(0, offset - PAGE_SIZE)}",
                }
            )
        if offset + PAGE_SIZE < len(candidates):
            navigation.append(
                {
                    "text": "Successivi",
                    "callback_data": f"{prefix}:{offset + PAGE_SIZE}",
                }
            )
        if navigation:
            reply("Cambia pagina:", [navigation])

    def _admin(
        self,
        conn: sqlite3.Connection,
        user: sqlite3.Row,
        command: str,
        argument: str,
        now: datetime,
        reply: Reply,
    ) -> None:
        if not user["admin"]:
            raise ValueError("Comando riservato all'amministratore")
        if command == "/richieste":
            ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT user_id FROM telegram_requests ORDER BY requested_at LIMIT 20"
                )
            ]
            reply(
                "Richieste di accesso (ID): "
                + (", ".join(ids) or "nessuna")
                + ". Verifica con la persona il suo ID prima di approvare. Dopo l'approvazione deve inviare di nuovo /start.",
                [
                    [{"text": f"Approva {uid}", "callback_data": f"approve:{uid}"}]
                    for uid in ids
                ]
                + BACK_MENU,
                panel=True,
            )
        elif command == "/consegne":
            counts = [
                f"{row[0]}: {row[1]}"
                for row in conn.execute(
                    "SELECT state,COUNT(*) FROM telegram_outbox GROUP BY state"
                )
            ]
            reply(
                "Consegne: "
                + (", ".join(counts) or "nessuna")
                + ". Gli invii ambigui non vengono ritentati automaticamente; /concorsi resta disponibile."
            )
        else:
            uid = int(argument)
            if uid <= 0:
                raise ValueError("ID utente non valido")
            if command == "/approva":
                present = conn.execute(
                    "SELECT 1 FROM telegram_users WHERE user_id=?", (uid,)
                ).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM telegram_users").fetchone()[
                    0
                ]
                if not present and count >= self.max_users:
                    raise ValueError("Limite degli utenti autorizzati raggiunto")
                conn.execute(
                    "INSERT OR IGNORE INTO telegram_users(user_id,include_unresolved,created_at) VALUES(?,0,?)",
                    (uid, _utc_iso(now)),
                )
                conn.execute("DELETE FROM telegram_requests WHERE user_id=?", (uid,))
                reply("Utente autorizzato. Deve inviare nuovamente /start al bot.")
            else:
                target = conn.execute(
                    "SELECT admin FROM telegram_users WHERE user_id=?", (uid,)
                ).fetchone()
                if target and target[0]:
                    raise ValueError(
                        "Per revocare un amministratore modifica la configurazione locale"
                    )
                conn.execute("DELETE FROM telegram_users WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM telegram_requests WHERE user_id=?", (uid,))
                reply("Accesso revocato e dati locali dell'utente cancellati.")
