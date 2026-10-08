"""Archivio delle versioni e confronto fra acquisizioni (SPEC 4.2, 5.2).

Il modulo risponde a M0-06: due acquisizioni degli stessi ID devono produrre
un confronto deterministico, e i casi di proroga, aggiunta di un allegato e
sostituzione del contenuto devono essere distinguibili fra loro.

Tre scelte reggono il resto del file:

1. Il confronto e' una funzione pura (`diff_snapshots`), separata dalla
   persistenza. I casi controllati di M0-06 si verificano cosi' senza rete e
   senza database, e il risultato non dipende dall'ordine con cui le righe
   tornano da SQLite.
2. I due hash hanno due significati diversi (SPEC 4.2). `raw_document_hash`
   copre tutto il documento e si muove anche per un elemento di pagina che
   cambia a ogni richiesta; `metadata_hash` copre i campi significativi. Per
   questo un `document_changed` isolato NON e' significativo: e' esattamente
   il falso aggiornamento che la specifica chiede di evitare.
3. Nulla viene cancellato per assenza. Un allegato che sparisce dalla scheda
   e un ID che sparisce da un elenco restano in archivio con la loro ultima
   osservazione: SPEC 4.2 avverte che un record assente da una ricerca non e'
   automaticamente revocato o eliminato.

Le date delle colonne sono sempre ISO 8601 in UTC a larghezza fissa, cosi' il
confronto fra istanti si puo' fare in SQL sulle stringhe senza ambiguita'.
Le date *dichiarate dalla fonte* non passano di qui: restano dentro
`SourceDateTime`, con il loro testo originale e la loro assunzione di fuso.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum

from .errors import ParseError
from .models import (
    Anomaly,
    AnomalySeverity,
    AttachmentRef,
    Certainty,
    ExtractionState,
    NoticePosition,
    NoticeSnapshot,
    NoticeStatus,
    Provenance,
    SourceDateTime,
    SyncRun,
    TerritoryScope,
    TerritoryValue,
)

SCHEMA_VERSION = 1
"""Versione dello schema SQLite scritta in `schema_meta`.

Esiste perche' una migrazione futura possa leggere lo stato invece di
indovinarlo: senza questo numero, l'unico modo per capire com'e' fatto un
database gia' popolato sarebbe ispezionarne le tabelle a mano.
"""

_SEPARATOR = " | "
"""Separatore per rendere confrontabili le sequenze (enti, territori).

Non e' la virgola perche' i valori della fonte la contengono gia': l'area
geografica multiregionale e' pubblicata proprio come "Lazio, Campania, ...".
"""


# --------------------------------------------------------------------------
# Differenze
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldChange:
    """Un singolo campo che si e' mosso fra due versioni.

    I valori sono resi come testo di proposito: servono a mostrare e a
    registrare la modifica, non a essere ricalcolati. `None` significa
    "assente in quella versione", mai "vuoto" o "zero" (SPEC 4.1).
    """

    field: str
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class NoticeDiff:
    """Esito del confronto fra due osservazioni dello stesso bando."""

    notice_id: str
    is_new: bool
    metadata_changed: bool
    document_changed: bool
    """Il documento e' cambiato ma i metadati significativi no.

    Da solo non e' un aggiornamento da comunicare: e' il caso che SPEC 4.2
    chiede di separare dagli altri, non di nascondere. Resta registrato
    perche' un documento che cambia di continuo senza mai toccare i metadati
    e' un segnale sul parser, non sul bando.
    """
    changes: tuple[FieldChange, ...]
    attachments_added: tuple[str, ...]
    attachments_removed: tuple[str, ...]
    attachments_changed: tuple[str, ...]
    """Stesso URL, nome o data di pubblicazione diversi.

    L'URL resta l'identita' dell'allegato (SPEC 4.2): un file sostituito
    dietro lo stesso indirizzo va notato qui, non trattato come un allegato
    nuovo. Il confronto dei byte appartiene a chi scarica il documento.
    """

    @property
    def significant(self) -> bool:
        """Se questa differenza merita di essere trattata come aggiornamento."""
        return bool(
            self.is_new
            or self.metadata_changed
            or self.attachments_added
            or self.attachments_removed
            or self.attachments_changed
        )


def _join(values: Iterable[str]) -> str | None:
    """Rende confrontabile una sequenza, distinguendo il vuoto dall'assente."""
    items = [value for value in values]
    if not items:
        return None
    return _SEPARATOR.join(items)


def _join_unordered(values: Iterable[str]) -> str | None:
    """Come `_join`, ma insensibile all'ordine degli elementi.

    Per enti e territori l'ordine non e' un dato: la fonte pubblica
    "Lazio, Campania, Liguria, ..." in un ordine che non e' alfabetico e che
    non dichiara di mantenere. `metadata_hash` in `detail.py` li ordina gia'
    prima di calcolare l'impronta, proprio per non muoversi a ogni riordino
    del backend.

    Se il confronto qui usasse l'ordine del documento, i due meccanismi si
    contraddirebbero: l'impronta direbbe "identico" e il diff "aggiornato",
    e un bando multiregionale genererebbe una notifica ogni volta che il
    backend restituisce le stesse regioni in ordine diverso. Un vero
    cambiamento -- una regione aggiunta o tolta -- resta visibile.
    """
    items = sorted(values)
    if not items:
        return None
    return _SEPARATOR.join(items)


def _instant_key(moment: datetime) -> str:
    """Chiave di confronto per un istante.

    Lo stesso istante scritto in fusi diversi non e' una modifica editoriale,
    quindi si normalizza a UTC. Un datetime senza fuso resta com'e': una
    conversione richiederebbe di assumere un fuso che nessuno ha dichiarato.
    """
    if moment.tzinfo is None:
        return moment.isoformat()
    return moment.astimezone(timezone.utc).isoformat()


def _date_key(value: SourceDateTime | None) -> str | None:
    """Rappresentazione normalizzata di una data della fonte.

    L'istante vince sulla sola data quando esiste: il passaggio da
    "06 Agosto 2026" a "06 Agosto 2026  14:00" e' una modifica reale della
    scadenza e deve risultare tale. Se la fonte pubblica un testo che non e'
    stato risolto, la chiave resta `None` e la differenza emerge dal raw.
    """
    if value is None:
        return None
    if value.instant is not None:
        return _instant_key(value.instant)
    if value.date_only is not None:
        return value.date_only.isoformat()
    return None


def _date_changes(name: str, before: SourceDateTime | None,
                  after: SourceDateTime | None) -> list[FieldChange]:
    """Confronta una data su due piani, tenendoli distinti.

    `<nome>` porta l'istante o il giorno normalizzato, `<nome>.raw` il testo
    pubblicato. Una riscrittura del testo con lo stesso istante e' comunque un
    cambiamento editoriale da registrare, ma non e' una proroga: chi legge il
    diff deve poter distinguere i due casi senza riaprire la scheda.
    """
    changed: list[FieldChange] = []
    before_key, after_key = _date_key(before), _date_key(after)
    if before_key != after_key:
        changed.append(FieldChange(name, before_key, after_key))
    before_raw = before.raw if before is not None else None
    after_raw = after.raw if after is not None else None
    if before_raw != after_raw:
        changed.append(FieldChange(f"{name}.raw", before_raw, after_raw))
    return changed


_TEXT_FIELDS: tuple[tuple[str, Callable[[NoticeSnapshot], str | None]], ...] = (
    ("title", lambda s: s.title),
    ("description", lambda s: s.description),
    ("status_normalized", lambda s: s.status_normalized.value),
    ("status_original", lambda s: s.status_original),
    ("total_positions",
     lambda s: None if s.total_positions is None else str(s.total_positions)),
    ("procedure_type", lambda s: s.procedure_type),
    ("enti", lambda s: _join_unordered(s.enti)),
    # Sui raw: la normalizzazione territoriale ha una sua certezza e puo'
    # migliorare fra due letture senza che la fonte abbia cambiato nulla
    # (SPEC 6). Il testo pubblicato invece cambia solo se cambia la scheda.
    ("territories", lambda s: _join_unordered(t.raw for t in s.territories)),
    ("pa_link", lambda s: s.pa_link),
)

_DATE_FIELDS: tuple[str, ...] = ("opening_at", "closing_at", "published_at")
# `source_updated_at` resta fuori: non e' contenuto della scheda ma un
# metadato di pubblicazione della fonte (il `lastmod` della sitemap). Un suo
# movimento da solo non e' un aggiornamento da comunicare (SPEC 4.2).
# Anche `positions` ed `extra_fields` restano fuori da M0: il primo non e'
# ancora popolato da un'estrazione verificata, il secondo raccoglie etichette
# non mappate, dove una variazione dice piu' sul parser che sul bando.


def _attachment_index(snapshot: NoticeSnapshot) -> dict[str, AttachmentRef]:
    """Allegati per URL, nell'ordine in cui la scheda li pubblica.

    Il primo riferimento vince sui duplicati: la stessa scheda puo' ripetere
    un collegamento, e contarlo due volte produrrebbe differenze inventate.
    """
    index: dict[str, AttachmentRef] = {}
    for ref in snapshot.attachments:
        index.setdefault(ref.url, ref)
    return index


def _attachment_key(ref: AttachmentRef) -> tuple[str | None, str | None, str | None]:
    """Cio' che, a parita' di URL, distingue due osservazioni di un allegato."""
    published_raw = ref.published.raw if ref.published is not None else None
    return (ref.filename, published_raw, _date_key(ref.published))


def diff_snapshots(before: NoticeSnapshot | None,
                   after: NoticeSnapshot) -> NoticeDiff:
    """Confronta due osservazioni dello stesso bando, senza alcun I/O.

    `before is None` significa "mai visto prima": in quel caso non si parla di
    campi modificati, ma gli allegati vengono comunque elencati come aggiunti,
    perche' chi reagisce ad `attachments_added` per scaricarli non deve
    perdere quelli della prima osservazione.
    """
    if before is None:
        return NoticeDiff(
            notice_id=after.notice_id,
            is_new=True,
            metadata_changed=False,
            document_changed=False,
            changes=(),
            attachments_added=tuple(_attachment_index(after)),
            attachments_removed=(),
            attachments_changed=(),
        )

    if before.notice_id != after.notice_id:
        # L'identita' e' l'ID della fonte (SPEC 4.2): confrontare due bandi
        # diversi produrrebbe un elenco di modifiche privo di senso.
        raise ValueError(
            f"Confronto fra bandi diversi: {before.notice_id!r} e {after.notice_id!r}"
        )

    changes: list[FieldChange] = []
    for name, reader in _TEXT_FIELDS:
        before_value, after_value = reader(before), reader(after)
        if before_value != after_value:
            changes.append(FieldChange(name, before_value, after_value))
    for name in _DATE_FIELDS:
        changes.extend(_date_changes(name, getattr(before, name), getattr(after, name)))

    if (not changes and before.metadata_hash and after.metadata_hash
            and before.metadata_hash != after.metadata_hash):
        # Rete di sicurezza: l'hash dei metadati copre anche campi che questo
        # confronto non elenca. Se si muove senza che nessun campo noto sia
        # cambiato, la modifica esiste comunque e va registrata invece di
        # sparire (SPEC 4.2).
        changes.append(
            FieldChange("metadata_hash", before.metadata_hash, after.metadata_hash)
        )

    before_attachments = _attachment_index(before)
    after_attachments = _attachment_index(after)
    added = tuple(url for url in after_attachments if url not in before_attachments)
    removed = tuple(url for url in before_attachments if url not in after_attachments)
    changed_attachments = tuple(
        url for url, ref in after_attachments.items()
        if url in before_attachments
        and _attachment_key(before_attachments[url]) != _attachment_key(ref)
    )

    metadata_changed = bool(changes)
    document_changed = (
        not metadata_changed
        and before.raw_document_hash is not None
        and after.raw_document_hash is not None
        and before.raw_document_hash != after.raw_document_hash
    )
    # Senza entrambi gli hash non si afferma nulla: un dato assente resta
    # assente (SPEC 4.1), non diventa "documento invariato".

    return NoticeDiff(
        notice_id=after.notice_id,
        is_new=False,
        metadata_changed=metadata_changed,
        document_changed=document_changed,
        changes=tuple(changes),
        attachments_added=added,
        attachments_removed=removed,
        attachments_changed=changed_attachments,
    )


def _persist_version(diff: NoticeDiff) -> bool:
    """Se l'osservazione merita una nuova riga in `notice_versions`.

    Oltre a metadati e documento (SPEC 4.2) entra anche un cambio di allegati:
    su una scheda senza hash del documento, l'aggiunta di un allegato sarebbe
    altrimenti l'unica modifica di cui non resta alcuna copia archiviata.
    """
    return bool(
        diff.is_new
        or diff.metadata_changed
        or diff.document_changed
        or diff.attachments_added
        or diff.attachments_removed
        or diff.attachments_changed
    )


# --------------------------------------------------------------------------
# Serializzazione degli snapshot
# --------------------------------------------------------------------------

_DATACLASSES: dict[str, type] = {
    cls.__name__: cls for cls in (
        Anomaly,
        AttachmentRef,
        NoticePosition,
        NoticeSnapshot,
        Provenance,
        SourceDateTime,
        SyncRun,
        TerritoryValue,
    )
}

_ENUMS: dict[str, type[Enum]] = {
    cls.__name__: cls for cls in (
        AnomalySeverity,
        Certainty,
        ExtractionState,
        NoticeStatus,
        TerritoryScope,
    )
}


def _encode(value: object) -> object:
    """Traduce un valore del contratto interno in JSON, tipo compreso.

    Ogni valore non primitivo viaggia in una busta `{"_t": ...}`: senza di
    essa un Enum tornerebbe come stringa, una tupla come lista e un istante
    con fuso come testo qualunque, e il round-trip restituirebbe un oggetto
    somigliante ma diverso da quello archiviato.
    """
    # Gli Enum del contratto ereditano da `str`: vanno riconosciuti prima dei
    # primitivi, altrimenti perderebbero la loro classe.
    if isinstance(value, Enum):
        return {"_t": "enum", "cls": type(value).__name__, "v": value.value}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # `datetime` e' sottoclasse di `date`: l'ordine di questi due rami decide
    # se un istante sopravvive o viene troncato al giorno.
    if isinstance(value, datetime):
        return {"_t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"_t": "date", "v": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "_t": "obj",
            "cls": type(value).__name__,
            "v": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, tuple):
        return {"_t": "tuple", "v": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Chiavi non testuali non rappresentabili nel payload")
        return {"_t": "map", "v": [[key, _encode(item)] for key, item in value.items()]}
    raise TypeError(f"Valore non serializzabile nel payload: {type(value).__name__}")


def _decode(value: object) -> object:
    """Inverso di `_encode`. Un tipo non riconosciuto e' un errore, non un salto."""
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value

    tag = value.get("_t")
    payload = value.get("v")
    if tag == "enum":
        enum_cls = _ENUMS.get(str(value.get("cls")))
        if enum_cls is None:
            raise ValueError(f"Enum sconosciuto nel payload: {value.get('cls')!r}")
        return enum_cls(payload)
    if tag == "datetime":
        return datetime.fromisoformat(str(payload))
    if tag == "date":
        return date.fromisoformat(str(payload))
    if tag == "tuple":
        if not isinstance(payload, list):
            raise ValueError("Busta 'tuple' senza elenco di elementi")
        return tuple(_decode(item) for item in payload)
    if tag == "map":
        if not isinstance(payload, list):
            raise ValueError("Busta 'map' senza elenco di coppie")
        return {str(pair[0]): _decode(pair[1]) for pair in payload}
    if tag == "obj":
        name = str(value.get("cls"))
        cls = _DATACLASSES.get(name)
        if cls is None:
            raise ValueError(f"Tipo sconosciuto nel payload: {name!r}")
        if not isinstance(payload, dict):
            raise ValueError(f"Busta '{name}' senza campi")
        known = {f.name for f in fields(cls)}
        arguments: dict[str, object] = {}
        for key, raw in payload.items():
            if key not in known:
                # Un campo scomparso dal contratto non viene scartato in
                # silenzio: e' il segnale che il payload appartiene a uno
                # schema diverso da quello dichiarato in `schema_meta`.
                raise ValueError(f"Campo '{key}' non previsto da {name}")
            arguments[key] = _decode(raw)
        return cls(**arguments)
    raise ValueError(f"Busta non riconosciuta nel payload: {tag!r}")


def snapshot_to_json(snapshot: NoticeSnapshot) -> str:
    """Serializza uno snapshot per la colonna `payload`.

    Le chiavi sono ordinate perche' due serializzazioni dello stesso snapshot
    devono produrre gli stessi byte: e' quello che rende ripetibile un
    confronto fatto sul contenuto archiviato.
    """
    if not isinstance(snapshot, NoticeSnapshot):
        raise TypeError("snapshot_to_json accetta solo un NoticeSnapshot")
    return json.dumps(_encode(snapshot), ensure_ascii=False, sort_keys=True)


def snapshot_from_json(payload: str) -> NoticeSnapshot:
    """Ricostruisce lo snapshot archiviato, con i tipi originali."""
    decoded = _decode(json.loads(payload))
    if not isinstance(decoded, NoticeSnapshot):
        raise ValueError("Il payload archiviato non contiene un NoticeSnapshot")
    return decoded


# --------------------------------------------------------------------------
# Archivio
# --------------------------------------------------------------------------


def _utc_iso(moment: datetime) -> str:
    """Istante in ISO 8601 UTC, a larghezza fissa.

    Un datetime senza fuso viene letto come UTC: e' l'orologio con cui il bot
    marca le proprie osservazioni, non un orario dichiarato dalla fonte (SPEC
    4.2). Gli orari pubblicati da inPA non passano da qui, restano in
    `SourceDateTime` con il loro `tz_assumption`.

    La larghezza fissa non e' estetica: i confronti fra istanti avvengono in
    SQL sulle stringhe, e solo con microsecondi sempre presenti l'ordine
    lessicografico coincide con quello cronologico.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notices (
        notice_id     TEXT PRIMARY KEY,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        source_url    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_versions (
        version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        notice_id         TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
        version_no        INTEGER NOT NULL,
        observed_at       TEXT NOT NULL,
        metadata_hash     TEXT,
        raw_document_hash TEXT,
        payload           TEXT NOT NULL,
        UNIQUE (notice_id, version_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments_seen (
        notice_id     TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
        url           TEXT NOT NULL,
        filename      TEXT,
        published_raw TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        PRIMARY KEY (notice_id, url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        method          TEXT NOT NULL,
        outcome         TEXT NOT NULL,
        complete        INTEGER NOT NULL,
        pages_expected  INTEGER,
        pages_read      INTEGER NOT NULL,
        records_seen    INTEGER NOT NULL,
        records_new     INTEGER NOT NULL,
        records_changed INTEGER NOT NULL,
        requests_made   INTEGER NOT NULL,
        anomalies       TEXT NOT NULL,
        errors          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notices_last_seen ON notices(last_seen_at)",
    """
    CREATE INDEX IF NOT EXISTS idx_versions_notice
        ON notice_versions(notice_id, version_no DESC)
    """,
)

_UPSERT_NOTICE = """
    INSERT INTO notices (notice_id, first_seen_at, last_seen_at, source_url)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(notice_id) DO UPDATE SET
        first_seen_at = MIN(notices.first_seen_at, excluded.first_seen_at),
        last_seen_at  = MAX(notices.last_seen_at, excluded.last_seen_at),
        source_url    = COALESCE(excluded.source_url, notices.source_url)
"""

_UPSERT_ATTACHMENT = """
    INSERT INTO attachments_seen
        (notice_id, url, filename, published_raw, first_seen_at, last_seen_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(notice_id, url) DO UPDATE SET
        filename      = excluded.filename,
        published_raw = excluded.published_raw,
        first_seen_at = MIN(attachments_seen.first_seen_at, excluded.first_seen_at),
        last_seen_at  = MAX(attachments_seen.last_seen_at, excluded.last_seen_at)
"""


class SnapshotStore:
    """Archivio SQLite degli snapshot, delle versioni e delle acquisizioni.

    Gli accessi sono brevi e le scritture serializzate da un lock (SPEC 10):
    il processo che riceve i comandi Telegram non deve restare fermo dietro a
    una sincronizzazione. Ogni scrittura apre e chiude la propria transazione
    esplicita; `PRAGMA foreign_keys` e i vincoli di unicita' sono attivi, cosi'
    una versione orfana o un allegato duplicato falliscono subito invece di
    sopravvivere in archivio.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = ":memory:" if path is None else os.fspath(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            timeout=30.0,
            # Nessuna transazione implicita: l'inizio e la fine di ogni
            # scrittura devono essere visibili nel codice, non dedotte dal
            # comportamento del driver.
            isolation_level=None,
            # Il lock di questa classe, non il controllo del thread di sqlite3,
            # e' cio' che serializza le scritture.
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Uno store senza tabelle non e' utile a nessuno: lo schema viene
        # creato subito. `initialise` resta pubblica ed e' idempotente.
        self.initialise()

    # -- ciclo di vita ---------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SnapshotStore gia' chiuso")
        return self._conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Una lettura, tenendo il lock per la sola durata della query.

        Serve anche in lettura, non solo in scrittura: `check_same_thread=False`
        dichiara che la connessione attraversa i thread, e `close()` azzera
        `self._conn`. Senza lock, una `close()` che cada fra il recupero della
        connessione e l'esecuzione della query fa fallire la lettura con un
        errore del driver, cioe' un guasto del processo invece di un "non c'e'
        niente da leggere". Con WAL i lettori non bloccano lo scrittore: il
        costo di questo lock e' la durata di una SELECT.
        """
        with self._lock:
            yield self._connection()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Una scrittura per volta, con transazione esplicita."""
        with self._lock:
            conn = self._connection()
            # IMMEDIATE: il lock di scrittura si prende all'inizio, cosi' il
            # conflitto emerge subito e non a meta' di una serie di INSERT.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def initialise(self) -> None:
        """Crea lo schema se assente. Ripetibile senza effetti."""
        with self._write() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                    ("created_at", _utc_iso(datetime.now(timezone.utc))),
                )
                return
            stored = int(row["value"])
            if stored != SCHEMA_VERSION:
                # Meglio fermarsi che scrivere in uno schema che non e'
                # quello atteso: una migrazione va scritta, non improvvisata.
                raise RuntimeError(
                    f"Schema versione {stored} in {self.path!r}, atteso "
                    f"{SCHEMA_VERSION}: serve una migrazione esplicita"
                )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- lettura ----------------------------------------------------------

    @staticmethod
    def _latest(conn: sqlite3.Connection, notice_id: str) -> NoticeSnapshot | None:
        row = conn.execute(
            "SELECT payload FROM notice_versions WHERE notice_id = ?"
            " ORDER BY version_no DESC LIMIT 1",
            (notice_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return snapshot_from_json(row["payload"])
        except (ValueError, TypeError, KeyError) as exc:
            # Una riga illeggibile -- schema cambiato, scrittura interrotta,
            # file danneggiato -- non deve uscire come eccezione del modulo
            # `json`: finirebbe fuori dalla tassonomia di `errors.py` e
            # risalirebbe fino a interrompere l'intera acquisizione per colpa
            # di un solo bando.
            #
            # Non si restituisce `None`: sarebbe peggio del guasto. `record()`
            # userebbe quel `None` come "mai visto prima", dichiarerebbe il
            # bando nuovo e rigenererebbe una notifica gia' inviata. Meglio un
            # errore tipizzato e visibile, che dice quale riga va riparata.
            raise ParseError(
                f"Versione archiviata non decodificabile per {notice_id!r}: {exc}"
            ) from exc

    def latest(self, notice_id: str) -> NoticeSnapshot | None:
        """Ultima versione archiviata, o `None` se il bando non ne ha.

        Un ID puo' esistere in `notices` senza alcuna versione: e' il caso di
        un ID visto in un elenco ma la cui scheda non e' ancora stata letta.
        """
        with self._read() as conn:
            return self._latest(conn, notice_id)

    # -- scrittura --------------------------------------------------------

    def record(self, snapshot: NoticeSnapshot) -> NoticeDiff:
        """Archivia un'osservazione e restituisce il confronto con la precedente.

        Una nuova riga di versione nasce solo se qualcosa e' cambiato; la data
        di ultima osservazione invece si aggiorna sempre, perche' "rivisto
        identico" e' un'informazione diversa da "non piu' visto" (SPEC 5.2).

        Un ID gia' noto non viene mai scartato: SPEC 5.2 chiede esplicitamente
        che un ID esistente possa aggiornare i propri contenuti.
        """
        with self._write() as conn:
            return self._record(conn, snapshot)

    def _record(self, conn: sqlite3.Connection, snapshot: NoticeSnapshot) -> NoticeDiff:
        """Record within the caller's transaction, including M1 job progress."""
        seen_at = _utc_iso(snapshot.observed_at)
        # Confronto e scrittura nella stessa transazione: fra il calcolo
        # del diff e l'inserimento della versione non deve potersi
        # infilare un'altra acquisizione dello stesso bando.
        diff = diff_snapshots(self._latest(conn, snapshot.notice_id), snapshot)
        conn.execute(
            _UPSERT_NOTICE,
            (snapshot.notice_id, seen_at, seen_at, snapshot.source_url),
        )
        if _persist_version(diff):
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS last"
                " FROM notice_versions WHERE notice_id = ?",
                (snapshot.notice_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO notice_versions (notice_id, version_no, observed_at,"
                " metadata_hash, raw_document_hash, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot.notice_id,
                    int(row["last"]) + 1,
                    seen_at,
                    snapshot.metadata_hash,
                    snapshot.raw_document_hash,
                    snapshot_to_json(snapshot),
                ),
            )
        for ref in _attachment_index(snapshot).values():
            published_raw = ref.published.raw if ref.published is not None else None
            conn.execute(
                _UPSERT_ATTACHMENT,
                (snapshot.notice_id, ref.url, ref.filename, published_raw,
                 seen_at, seen_at),
            )
        # Gli allegati spariti dalla scheda restano in tabella con la loro
        # ultima osservazione: un'assenza non e' una cancellazione
        # (SPEC 4.2), ed e' cio' che permette di raccontare cosa e' stato
        # tolto da un bando.
        return diff

    def record_sync_run(self, run: SyncRun) -> int:
        """Registra un'acquisizione e restituisce il suo identificativo.

        `complete` viene salvato come colonna e non ricalcolato a posteriori:
        e' la condizione che decide se il punto di ripresa puo' avanzare
        (SPEC 5.2), e deve restare quella valutata in quel momento.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, method, outcome,"
                " complete, pages_expected, pages_read, records_seen, records_new,"
                " records_changed, requests_made, anomalies, errors)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_iso(run.started_at),
                    None if run.finished_at is None else _utc_iso(run.finished_at),
                    run.method,
                    run.outcome,
                    int(run.complete),
                    run.pages_expected,
                    run.pages_read,
                    run.records_seen,
                    run.records_new,
                    run.records_changed,
                    run.requests_made,
                    json.dumps([_encode(item) for item in run.anomalies],
                               ensure_ascii=False, sort_keys=True),
                    json.dumps(list(run.errors), ensure_ascii=False),
                ),
            )
            run_id = cursor.lastrowid
        if run_id is None:
            raise RuntimeError("SQLite non ha restituito l'identificativo della run")
        return int(run_id)

    def mark_seen(self, notice_ids: Collection[str], observed_at: datetime) -> None:
        """Registra che questi ID sono stati osservati in un elenco.

        Un ID puo' arrivare qui prima della sua scheda: in quel caso la riga
        nasce con `source_url` a `NULL` invece che con un URL ricostruito a
        tavolino (SPEC 4.1). La colonna si popola quando arriva il primo
        `NoticeSnapshot`.
        """
        if not notice_ids:
            return
        seen_at = _utc_iso(observed_at)
        rows = [(notice_id, seen_at, seen_at, None) for notice_id in notice_ids]
        with self._write() as conn:
            conn.executemany(_UPSERT_NOTICE, rows)

    def not_seen_since(self, moment: datetime) -> tuple[str, ...]:
        """ID la cui ultima osservazione e' anteriore a `moment`.

        Questo NON significa revocato, chiuso o eliminato (SPEC 4.2, 5.2): e'
        soltanto un'assenza da un elenco, e un elenco puo' cambiare per un
        filtro diverso, per una paginazione incompleta o per un guasto della
        fonte. La sitemap verificata, per esempio, copre i bandi attivi e non
        lo storico: un bando che si chiude ne esce pur restando pubblicato.
        Il risultato e' materiale per una riconciliazione, non un verdetto.
        """
        threshold = _utc_iso(moment)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT notice_id FROM notices WHERE last_seen_at < ?"
                " ORDER BY notice_id",
                (threshold,),
            ).fetchall()
        return tuple(str(row["notice_id"]) for row in rows)
