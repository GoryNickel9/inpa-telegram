"""Livello di acquisizione dei dati inPA (milestone M0).

Il resto dell'applicazione dovrebbe importare da qui e non dai moduli interni:
cio' che e' stabile e' il contratto di `models`, non il modo in cui oggi si
arriva ai dati. Cambiare rotta di acquisizione deve voler dire sostituire una
implementazione di `SourceAdapter`, non riscrivere il bot.

Due avvertenze che valgono per chiunque usi questo pacchetto:

* gli allegati non sono scaricabili. `portale.inpa.gov.it`, che li ospita
  tutti, dichiara `Disallow: /`. `fetch_attachment` restituisce quindi
  `ExtractionState.FETCH_BLOCKED` senza toccare la rete. Non e' un difetto
  da aggirare: vedi `reports/access_conditions.md`;
* la ricerca non filtra per territorio o stato. La sitemap non espone quei
  campi, quindi `SearchPage.unapplied_filters` elenca cio' che e' stato
  chiesto e non applicato. Chi consuma il risultato deve leggerlo: un filtro
  ignorato in silenzio e' peggio di un filtro assente.
"""

from __future__ import annotations

from .adapter import InpaSitemapAdapter, build_default_client, detect_mime
from .errors import (
    ContentTypeError,
    HttpStatusError,
    IncompleteAcquisitionError,
    ParseError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    SourceError,
    TransportError,
    UnsafeUrlError,
)
from .http import HttpClient, HttpConfig
from .models import (
    Anomaly,
    AnomalySeverity,
    AttachmentPayload,
    AttachmentRef,
    Certainty,
    ExtractionState,
    NoticePosition,
    NoticeSnapshot,
    NoticeStatus,
    NoticeStub,
    Provenance,
    SearchFilters,
    SearchPage,
    SourceAdapter,
    SourceDateTime,
    SyncRun,
    TerritoryScope,
    TerritoryValue,
)
from .robots import RobotsDecision, RobotsPolicy
from .snapshot import FieldChange, NoticeDiff, SnapshotStore, diff_snapshots

__all__ = [
    # adattatore
    "InpaSitemapAdapter", "build_default_client", "detect_mime",
    # contratto
    "SourceAdapter", "SearchFilters", "SearchPage", "NoticeStub",
    "NoticeSnapshot", "NoticePosition", "AttachmentRef", "AttachmentPayload",
    "TerritoryValue", "TerritoryScope", "SourceDateTime", "Provenance",
    "Anomaly", "AnomalySeverity", "Certainty", "ExtractionState",
    "NoticeStatus", "SyncRun",
    # archivio
    "SnapshotStore", "NoticeDiff", "FieldChange", "diff_snapshots",
    # trasporto
    "HttpClient", "HttpConfig", "RobotsPolicy", "RobotsDecision",
    # errori
    "SourceError", "TransportError", "HttpStatusError", "ContentTypeError",
    "ResponseTooLargeError", "RobotsDisallowedError", "UnsafeUrlError",
    "ParseError", "IncompleteAcquisitionError",
]
