"""Contratto interno dell'acquisizione (SPEC 4).

I nomi di questo modulo appartengono al progetto. NON descrivono un'API
ufficiale inPA e non devono seguire i nomi correnti dei campi della fonte:
l'applicazione dipende da questa interfaccia, non dal portale.

Tre invarianti attraversano tutto il modulo:

1. Un dato assente resta assente. Nessun campo viene riempito con un valore
   plausibile (SPEC 4.1). Per questo `SourceDateTime` separa la data
   pubblicata dall'istante: se la fonte non pubblica un'ora, `instant`
   resta `None` e nessuno puo' inventare le 23:59.
2. Ogni valore porta con se' la sua provenienza e il suo grado di certezza
   (`Provenance`), cosi' che un'inferenza non si confonda con un'osservazione.
3. Le anomalie sono dati di prima classe, non log: un'acquisizione parziale
   deve poter essere riconosciuta come tale a valle (SPEC 5.2, M0-08).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Vocabolari
# --------------------------------------------------------------------------


class Certainty(str, Enum):
    """Quanto e' solido un valore, indipendentemente dal fatto che esista."""

    OBSERVED = "observed"
    """Letto alla lettera dalla fonte, senza interpretazione."""

    DERIVED = "derived"
    """Calcolato da valori osservati tramite una regola documentata."""

    AMBIGUOUS = "ambiguous"
    """La fonte espone qualcosa, ma non e' risolvibile in modo affidabile."""

    ABSENT = "absent"
    """La fonte non ha fornito il dato. Diverso da "il dato e' negativo"."""


class NoticeStatus(str, Enum):
    """Stato normalizzato del bando.

    La specifica (2.1) avverte che gli stati osservati nel sorgente di terzi
    non sono un elenco completo. Qualunque valore non riconosciuto diventa
    `UNKNOWN` e conserva l'originale: non viene forzato su OPEN/CLOSED.
    """

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class TerritoryScope(str, Enum):
    """Che cosa descrive un valore territoriale (SPEC 6).

    Sede dell'ente, sede di lavoro, ambito del bando e sede delle prove sono
    concetti distinti. Confonderli e' il modo piu' rapido per proporre a un
    utente un bando che si svolge altrove.
    """

    WORK_SITE = "work_site"
    ENTE_SEAT = "ente_seat"
    NOTICE_SCOPE = "notice_scope"
    EXAM_SITE = "exam_site"
    UNSPECIFIED = "unspecified"
    """La fonte espone un territorio senza dichiarare a cosa si riferisca.

    E' il caso del campo "Area geografica" della scheda pubblica: va trattato
    in modo conservativo, mai come prova della sede di lavoro.
    """


class AnomalySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"
    """Impedisce di considerare completa l'acquisizione interessata."""


class ExtractionState(str, Enum):
    """Esito del recupero/lettura di un allegato (SPEC 8.2)."""

    OK = "ok"
    EMPTY = "empty"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FETCH_BLOCKED = "fetch_blocked"
    """Non scaricato per una condizione di accesso dichiarata (es. robots)."""
    FETCH_FAILED = "fetch_failed"
    EXTRACTION_FAILED = "extraction_failed"


# --------------------------------------------------------------------------
# Provenienza, anomalie, date
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Da dove viene un valore e quanto e' affidabile."""

    source_url: str
    observed_at: datetime
    """Istante UTC dell'osservazione del bot. Non e' una data dichiarata
    dalla fonte (SPEC 4.2)."""
    locator: str | None = None
    """Ancoraggio verificabile: etichetta della scheda, xpath-like, pagina."""
    certainty: Certainty = Certainty.OBSERVED
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Anomaly:
    """Scostamento osservato, da propagare invece che da nascondere."""

    code: str
    message: str
    severity: AnomalySeverity = AnomalySeverity.WARNING
    context: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is AnomalySeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class SourceDateTime:
    """Una data pubblicata dalla fonte, senza arrotondamenti inventati.

    `date_only` e' valorizzato quando la fonte pubblica almeno un giorno.
    `instant` esiste solo se la fonte pubblica anche un orario: la specifica
    (4.1) vieta di inventare le 23:59 quando e' pubblicata la sola data.
    """

    raw: str
    date_only: date | None = None
    instant: datetime | None = None
    tz_assumption: str | None = None
    """Fuso applicato per ottenere `instant`, quando la fonte non lo dichiara.
    Per l'interfaccia italiana e' `Europe/Rome` (SPEC 4.1)."""
    certainty: Certainty = Certainty.OBSERVED
    note: str | None = None

    @property
    def has_explicit_time(self) -> bool:
        return self.instant is not None

    @property
    def resolved(self) -> bool:
        return self.date_only is not None or self.instant is not None


@dataclass(frozen=True, slots=True)
class TerritoryValue:
    """Un territorio come lo espone la fonte, piu' la normalizzazione provata."""

    raw: str
    scope: TerritoryScope
    provenance: Provenance
    region_code: str | None = None
    region_name: str | None = None
    province_code: str | None = None
    province_name: str | None = None
    comune_name: str | None = None
    certainty: Certainty = Certainty.OBSERVED
    """Certezza della NORMALIZZAZIONE, non dell'esistenza del testo grezzo."""
    national: bool = False
    """La fonte dichiara l'ambito come nazionale (valore osservato: "Nazionale").

    Tenuto distinto dai codici territoriali per una ragione misurata sul campo:
    filtrando la ricerca pubblica per Molise e per Valle d'Aosta si ottengono
    in testa gli stessi bandi, tutti marcati "Nazionale". Un bando nazionale
    non e' quindi un bando "di quella regione", e non e' nemmeno un territorio
    non riconosciuto: e' una terza categoria, che SPEC 6 chiede di poter
    includere o escludere con una scelta esplicita dell'utente.
    """

    @property
    def normalized(self) -> bool:
        return (self.region_code is not None or self.province_code is not None
                or self.national)


# --------------------------------------------------------------------------
# Allegati
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Riferimento a un allegato, senza averlo ancora scaricato.

    `fetch_permitted` registra l'esito della valutazione delle condizioni di
    accesso al momento dell'osservazione: un riferimento puo' essere
    perfettamente valido e comunque non scaricabile (SPEC 3.1).
    """

    notice_id: str
    url: str
    provenance: Provenance
    filename: str | None = None
    published: SourceDateTime | None = None
    declared_extension: str | None = None
    """Estensione dichiarata dal NOME del file, non dall'URL: il percorso
    osservato (`/api/media/<uuid>`) non termina in `.pdf` (SPEC 2.3)."""
    fetch_permitted: bool = True
    fetch_block_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentPayload:
    """Byte effettivamente scaricati di un allegato."""

    ref: AttachmentRef
    state: ExtractionState
    content: bytes | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    declared_mime: str | None = None
    """Content-Type dichiarato dal server."""
    detected_mime: str | None = None
    """Tipo dedotto dalla firma dei byte. Se diverge dal dichiarato va
    segnalato: SPEC 11 richiede di convalidare MIME e firma."""
    http_etag: str | None = None
    http_last_modified: str | None = None
    fetched_at: datetime | None = None
    anomalies: tuple[Anomaly, ...] = ()


# --------------------------------------------------------------------------
# Bandi
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoticeStub:
    """Quanto si conosce di un bando prima di aprirne la scheda.

    La rotta di scoperta verificata (sitemap) fornisce ID, URL e `lastmod`:
    deliberatamente poco. Un `NoticeStub` non e' un bando impoverito, e'
    l'onesta rappresentazione di cio' che la scoperta ha davvero visto.
    """

    notice_id: str
    source_url: str
    provenance: Provenance
    source_last_modified: SourceDateTime | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class NoticePosition:
    """Un profilo/codice di concorso all'interno di un bando (SPEC 4.1).

    Un bando multiregionale con piu' codici non va appiattito: la specifica
    (9.1) vieta di presentare tutti i posti come disponibili in una provincia.
    """

    code: str | None
    title: str | None = None
    positions: int | None = None
    territories: tuple[TerritoryValue, ...] = ()
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class NoticeSnapshot:
    """Una scheda letta in un preciso istante.

    Gli hash sono due (SPEC 4.2): `metadata_hash` copre solo i campi
    significativi, `raw_document_hash` l'intero documento. Elementi di pagina
    che cambiano a ogni richiesta muovono il secondo ma non il primo, evitando
    una raffica di falsi aggiornamenti.
    """

    notice_id: str
    source_url: str
    observed_at: datetime
    status_original: str | None = None
    status_normalized: NoticeStatus = NoticeStatus.UNKNOWN
    title: str | None = None
    description: str | None = None
    enti: tuple[str, ...] = ()
    procedure_type: str | None = None
    total_positions: int | None = None
    opening_at: SourceDateTime | None = None
    closing_at: SourceDateTime | None = None
    published_at: SourceDateTime | None = None
    source_updated_at: SourceDateTime | None = None
    territories: tuple[TerritoryValue, ...] = ()
    positions: tuple[NoticePosition, ...] = ()
    attachments: tuple[AttachmentRef, ...] = ()
    pa_link: str | None = None
    extra_fields: dict[str, str] = field(default_factory=dict)
    """Etichette della scheda non ancora mappate. Conservarle e' cio' che
    permette di accorgersi che la fonte ha aggiunto un campo."""
    metadata_hash: str | None = None
    raw_document_hash: str | None = None
    anomalies: tuple[Anomaly, ...] = ()
    provenance: Provenance | None = None

    @property
    def has_blocking_anomaly(self) -> bool:
        return any(a.blocking for a in self.anomalies)


# --------------------------------------------------------------------------
# Ricerca
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Filtri richiesti dal chiamante.

    Un adattatore che non sa applicare un filtro lato fonte deve dichiararlo
    in `SearchPage.unapplied_filters` e non fingere di averlo applicato.
    """

    text: str | None = None
    statuses: tuple[NoticeStatus, ...] = ()
    region_codes: tuple[str, ...] = ()
    province_codes: tuple[str, ...] = ()
    updated_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Una pagina di risultati (SPEC 4).

    `complete` non dice "ho letto tutto il perimetro": dice che QUESTA pagina
    e' stata letta integralmente. La completezza della scansione e' di
    `SyncRun`. Tenerle separate evita di avanzare un cursore su dati parziali.
    """

    records: tuple[NoticeStub, ...]
    complete: bool
    next_cursor: str | None = None
    declared_total: int | None = None
    """Totale dichiarato dalla fonte, se esposto. Non e' necessariamente uno
    snapshot immutabile durante la paginazione (SPEC 5.2)."""
    unapplied_filters: tuple[str, ...] = ()
    anomalies: tuple[Anomaly, ...] = ()

    @property
    def exhausted(self) -> bool:
        return self.next_cursor is None


@dataclass(frozen=True, slots=True)
class SyncRun:
    """Registro di un'acquisizione (SPEC 4.1)."""

    started_at: datetime
    method: str
    finished_at: datetime | None = None
    outcome: str = "running"
    pages_expected: int | None = None
    pages_read: int = 0
    records_seen: int = 0
    records_new: int = 0
    records_changed: int = 0
    requests_made: int = 0
    anomalies: tuple[Anomaly, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Vero solo se la scansione puo' reggere l'avanzamento del cursore."""
        if self.outcome != "ok" or self.errors:
            return False
        if any(a.blocking for a in self.anomalies):
            return False
        if self.pages_expected is not None and self.pages_read < self.pages_expected:
            return False
        return True


# --------------------------------------------------------------------------
# Interfaccia dell'adattatore
# --------------------------------------------------------------------------


@runtime_checkable
class SourceAdapter(Protocol):
    """Interfaccia da cui dipende l'applicazione (SPEC 4).

    Il resto del bot non deve mai importare un modulo che conosca l'HTML di
    inPA: cambiare rotta di acquisizione deve significare sostituire una
    implementazione di questo Protocol.
    """

    name: str

    def search(self, filters: SearchFilters,
               page_cursor: str | None = None) -> SearchPage: ...

    def fetch_notice(self, notice_id: str) -> NoticeSnapshot: ...

    def list_attachments(self, notice_id: str) -> tuple[AttachmentRef, ...]: ...

    def fetch_attachment(self, ref: AttachmentRef) -> AttachmentPayload: ...
