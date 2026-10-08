"""Lettura della scheda pubblica di un bando (SPEC 2.2, 4.1).

La scheda su `www.inpa.gov.it` e' l'unica superficie di dettaglio utilizzabile:
`portale.inpa.gov.it` dichiara `Disallow: /` per ogni user-agent, quindi non lo
sono ne' l'endpoint di ricerca ne' gli allegati, che vivono tutti su
quell'host. Questo modulo legge l'HTML reso lato server e produce un
`NoticeSnapshot` senza scaricare nulla: se un allegato sia prelevabile lo
decide il chiamante, tramite `attachment_permission` (SPEC 3.1).

Perche' una scansione a flusso di token e non un albero: nei campioni reali il
`<p>` dell'etichetta "Data chiusura candidature" e' chiuso da `</div>`. Un
parser che pretende un annidamento coerente, davanti a quel markup, perde il
valore oppure lo attacca al nodo sbagliato; leggere invece la sequenza
"etichetta -> primo blocco valore successivo" rende l'errore irrilevante,
perche' l'ordine dei token resta corretto anche quando l'annidamento non lo e'.
Per la stessa ragione la fine di un blocco non e' solo il suo tag di chiusura:
e' anche l'inizio del blocco successivo, cosi' che un tag mai chiuso non si
porti dietro tutto il resto della pagina.

Tre cose che il parser deliberatamente NON fa:

* non deduce `published_at`. La scheda mostra "Data apertura candidature", che
  nel JS della fonte (`dro-bandi-functions.js`) e' alimentata da
  `dataPubblicazione`: stesso dato, due significati diversi, ed e' proprio la
  confusione che SPEC 4.1 vieta. Senza una data di pubblicazione distinta il
  campo resta `None`.
* non ricostruisce i profili (`NoticePosition`). La ripartizione per codice sta
  nella prosa della descrizione e nel PDF del bando: dedurla dal testo
  produrrebbe numeri plausibili e sbagliati (SPEC 9.1).
* non deduce una scheda dall'assenza di dati. Se non c'e' nessuna etichetta
  `concorso-label` solleva `ParseError` invece di restituire un bando con tutti
  i campi vuoti: una pagina di errore non deve diventare un "nessun concorso"
  silenzioso (M0-08).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

from .errors import ParseError
from .models import (
    Anomaly,
    AnomalySeverity,
    AttachmentRef,
    Certainty,
    NoticeSnapshot,
    Provenance,
    SourceDateTime,
    TerritoryScope,
)
from .normalize import (
    clean_text,
    document_hash,
    metadata_hash,
    normalize_status,
    parse_int,
    parse_italian_datetime,
)
# La forma dell'id e' gia' dichiarata dove e' stata osservata (le `<loc>` della
# sitemap): duplicarla qui creerebbe due verita' che possono divergere.
from .sitemap import NOTICE_ID_RE
from .territory import parse_area_geografica

DETAIL_URL_TEMPLATE = (
    "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/"
    "?concorso_id={notice_id}"
)

MAX_DETAIL_CHARS = 4 * 1024 * 1024
"""Tetto al documento accettato dal parser (SPEC 11).

La scheda piu' pesante dei campioni era 115 KB: il limite lascia trenta volte
quel volume e serve solo a impedire che una risposta anomala faccia crescere
senza controllo i buffer di questo modulo.
"""

AttachmentPermission = Callable[[str], tuple[bool, str | None]]
"""Valutazione delle condizioni di accesso a un allegato: `(permesso, motivo)`.

Il parser non conosce robots.txt e non deve conoscerlo: riceve una funzione
gia' informata (tipicamente costruita su `RobotsPolicy.check`) e ne registra
l'esito nel riferimento all'allegato.
"""

UNEVALUATED_PERMISSION = "permesso non valutato"
"""Motivo registrato quando nessuno ha valutato le condizioni di accesso.

Senza valutazione l'allegato resta `fetch_permitted=False`: un falso "si'"
porta a una richiesta che la fonte ha vietato, un falso "no" costa solo un
download in meno.
"""

# --------------------------------------------------------------------------
# Etichette della scheda
# --------------------------------------------------------------------------

LABEL_DESCRIPTION = "Descrizione:"
LABEL_AREA = "Area geografica:"
LABEL_PROCEDURE = "Valutazione:"
LABEL_STATUS = "Stato:"
LABEL_OPENING = "Data apertura candidature:"
LABEL_CLOSING = "Data chiusura candidature:"
LABEL_POSITIONS = "Numero di posti:"
LABEL_ENTI = "Ente di riferimento:"
LABEL_PA_LINK = "Link al sito della PA:"
LABEL_ATTACHMENTS = "Bando/Avviso e Allegati:"

MAPPED_LABELS = frozenset({
    LABEL_DESCRIPTION, LABEL_AREA, LABEL_PROCEDURE, LABEL_STATUS,
    LABEL_OPENING, LABEL_CLOSING, LABEL_POSITIONS, LABEL_ENTI,
    LABEL_PA_LINK, LABEL_ATTACHMENTS,
})

_MIN_LABELS_FOR_RATIO = 4
"""Sotto questa soglia il rapporto etichette/valori non dice nulla.

Una scheda con una o due etichette puo' essere legittimamente scarna;
il confronto fra quante etichette ci sono e quante hanno prodotto un
valore diventa significativo solo quando le etichette sono parecchie.
"""

EXPECTED_LABELS: tuple[str, ...] = (
    LABEL_DESCRIPTION, LABEL_AREA, LABEL_STATUS, LABEL_CLOSING,
    LABEL_POSITIONS, LABEL_ENTI,
)
"""Etichette presenti in tutte le 26 schede campionate il 19/09/2026.

Allegati e link alla PA restano fuori perche' mancavano rispettivamente in 2 e
in 12 schede: la loro assenza e' normale, non un'anomalia.
"""

# --------------------------------------------------------------------------
# Scansione dell'HTML
# --------------------------------------------------------------------------

_LABEL_CLASS = "concorso-label"
_CONTENT_CLASS = "concorso-content"
_STATUS_CLASS = "concorso-status"
_PROSE_CLASS = "font-lora"

_KIND_CONTENT = "content"
_KIND_STATUS = "status"
_KIND_PROSE = "prose"

_OPAQUE_TAGS = frozenset({"script", "style"})
"""Elementi il cui contenuto e' codice: `HTMLParser` lo consegna come testo e
senza questo filtro finirebbe dentro una descrizione."""

_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "div", "dd", "dt", "h1", "h2",
    "h3", "h4", "h5", "h6", "li", "ol", "p", "section", "table", "td", "th",
    "tr", "ul",
})
"""Tag che separano il testo. La distinzione da quelli in linea e' necessaria:
`<strong>seicen</strong>to` deve restare una parola sola, mentre
`<p>uno</p><p>due</p>` deve restare due frasi."""

_PUBLISHED_RE = re.compile(
    r"\(\s*Pubblicat[oa]\s+il\s+(?P<when>[^)]+?)\s*\)", re.IGNORECASE
)
"""La data dell'allegato e' dentro il testo del link, non in un attributo:
"NOMEFILE (Pubblicato il 06 Agosto 2026  14:00)"."""

_EXTENSION_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,7}")
"""Forma accettata per un'estensione di file.

Deve iniziare per lettera: in "Comunicazione n. 3" il testo dopo l'ultimo punto
e' " 3", e chiamarlo estensione significherebbe inventare un formato (SPEC 4.1).
"""


@dataclass(slots=True)
class _Anchor:
    """Un `<a>` letto dentro un blocco valore."""

    href: str | None
    title: str | None
    text: str = ""


@dataclass(slots=True)
class _Block:
    """Un contenitore di valore che segue un'etichetta."""

    kind: str
    text: str | None = None
    anchors: list[_Anchor] = field(default_factory=list)


@dataclass(slots=True)
class _LabelledField:
    """Un'etichetta con i blocchi valore che la seguono fino alla successiva.

    I blocchi sono piu' d'uno per un motivo osservato: gli allegati sono un
    `<p class="concorso-content">` ciascuno, tutti sotto la stessa etichetta.
    """

    label: str
    blocks: list[_Block] = field(default_factory=list)


def _classes(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
    for name, value in attrs:
        if name == "class":
            return frozenset((value or "").split())
    return frozenset()


def _block_kind(tag: str, classes: frozenset[str]) -> str | None:
    """Riconosce un contenitore di valore, o `None` se il tag non lo e'.

    `concorso-content` e' usato sia su `<span>` sia su `<p>` (il campo raro
    "Corrispettivo minimo annuo" e' un `<p>`), quindi il tag non entra nel
    riconoscimento. Fa eccezione la prosa: `font-lora` compare anche sugli
    span di valore, e solo su un `<div>` indica il corpo della descrizione.
    """
    if _CONTENT_CLASS in classes:
        return _KIND_CONTENT
    if _STATUS_CLASS in classes:
        return _KIND_STATUS
    if tag == "div" and _PROSE_CLASS in classes:
        return _KIND_PROSE
    return None


class _DetailScanner(HTMLParser):
    """Scansione sequenziale della scheda: titolo, etichette, blocchi valore.

    Non costruisce nessun albero e non si fida dei tag di chiusura. La cattura
    di un blocco termina al primo fra: il suo tag di chiusura alla profondita'
    giusta, l'inizio di un'altra etichetta, l'inizio di un altro blocco valore,
    la fine del documento. E' questa ridondanza a rendere il parser insensibile
    al `<p>` chiuso da `</div>` della scheda reale.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.fields: list[_LabelledField] = []
        self.label_seen = False
        self._mode: str | None = None
        self._tag: str | None = None
        self._depth = 0
        self._chunks: list[str] = []
        self._block: _Block | None = None
        self._anchor: _Anchor | None = None
        self._opaque = 0

    # -- gestione della cattura ------------------------------------------

    def _begin(self, mode: str, tag: str) -> None:
        self._mode = mode
        self._tag = tag
        self._depth = 1
        self._chunks = []

    def _flush(self) -> None:
        """Chiude la cattura in corso e assegna il testo raccolto."""
        if self._mode is None:
            return
        self._close_anchor()
        mode, block = self._mode, self._block
        text = clean_text("".join(self._chunks), unescape=False)
        self._mode = None
        self._tag = None
        self._depth = 0
        self._chunks = []
        self._block = None

        if mode == "title":
            self.title = text
        elif mode == "label":
            self.fields[-1].label = text or ""
        elif block is not None:
            block.text = text
            self.fields[-1].blocks.append(block)

    def _close_anchor(self) -> None:
        if self._anchor is None:
            return
        if self._block is not None:
            self._block.anchors.append(self._anchor)
        self._anchor = None

    # -- interfaccia di HTMLParser ---------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque += 1
            return

        classes = _classes(attrs)

        if _LABEL_CLASS in classes:
            self._flush()
            self.label_seen = True
            self.fields.append(_LabelledField(label=""))
            self._begin("label", tag)
            return

        kind = _block_kind(tag, classes)
        if kind is not None and self.fields and self._mode != "label":
            self._flush()
            self._block = _Block(kind=kind)
            self._begin("value", tag)
            return

        if self._mode is None:
            # Il titolo e' l'unico `<h1>` di tutte le schede campionate. Si
            # prende il primo e non l'ultimo: un secondo `<h1>` aggiunto dal
            # tema sarebbe comunque dopo il contenuto dell'articolo.
            if tag == "h1" and self.title is None:
                self._begin("title", tag)
            return

        if tag == self._tag:
            self._depth += 1
        if tag == "a" and self._mode == "value":
            attributes = dict(attrs)
            self._close_anchor()
            self._anchor = _Anchor(href=attributes.get("href"),
                                   title=attributes.get("title"))
        if tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque = max(0, self._opaque - 1)
            return
        if self._mode is None:
            return
        if tag == "a":
            self._close_anchor()
        if tag == self._tag:
            self._depth -= 1
            if self._depth <= 0:
                self._flush()
                return
        if tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._mode is None or self._opaque:
            return
        self._chunks.append(data)
        if self._anchor is not None:
            self._anchor.text += data

    def close(self) -> None:
        super().close()
        # Un documento troncato lascia una cattura aperta: il testo gia' letto
        # e' un dato osservato e va consegnato, non buttato.
        self._flush()


# --------------------------------------------------------------------------
# Lettura dei valori
# --------------------------------------------------------------------------


def build_detail_url(notice_id: str) -> str:
    """URL della scheda per un identificativo gia' ottenuto dalla fonte.

    L'id viene convalidato e non codificato: accettare qualunque stringa
    significherebbe lasciar costruire una query arbitraria a partire da un
    valore letto altrove. La specifica (2.2) vieta comunque di enumerare gli
    id, quindi qui passano solo quelli visti nella sitemap o in un link.
    """
    if not NOTICE_ID_RE.fullmatch(notice_id):
        raise ValueError(
            f"concorso_id non nella forma osservata (32 cifre esadecimali): "
            f"{notice_id!r}"
        )
    return DETAIL_URL_TEMPLATE.format(notice_id=notice_id)


def _first_text(item: _LabelledField | None) -> str | None:
    if item is None:
        return None
    for block in item.blocks:
        if block.text:
            return block.text
    return None


def _first_href(item: _LabelledField | None) -> str | None:
    if item is None:
        return None
    for block in item.blocks:
        for anchor in block.anchors:
            href = (anchor.href or "").strip()
            if href:
                return href
    return None


def _read_date(raw: str | None, label: str,
               anomalies: list[Anomaly]) -> SourceDateTime | None:
    """Interpreta una data della scheda segnalando cio' che non si e' risolto.

    L'anomalia accompagna il valore invece di sostituirlo: `SourceDateTime`
    conserva comunque il testo originale, cosi' a valle si vede sia che cosa
    la fonte aveva pubblicato sia perche' non e' stato possibile usarlo.
    """
    value = parse_italian_datetime(raw)
    if value is not None and value.certainty is Certainty.AMBIGUOUS:
        anomalies.append(Anomaly(
            code="data_non_interpretabile",
            message=f"Data non risolta per '{label}': {value.note}",
            severity=AnomalySeverity.WARNING,
            context=value.raw,
        ))
    return value


def _declared_extension(filename: str | None) -> str | None:
    """Estensione dedotta dal NOME del file, mai dall'URL (SPEC 2.3).

    Gli allegati sono serviti da `/api/media/<uuid>`, un percorso che non
    termina in `.pdf` pur restituendo un PDF: l'unica estensione dichiarata
    dalla fonte e' quella nel nome del file.
    """
    if filename is None:
        return None
    _, dot, suffix = filename.rpartition(".")
    if not dot:
        return None
    candidate = suffix.strip()
    if not _EXTENSION_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def _attachment_filename(anchor: _Anchor) -> str | None:
    """Nome del file: prima l'attributo `title`, poi il testo del link.

    Il testo del link e' una versione tipografica del nome ("Allegato A
    &#8211; Docente TD.rtf" con la lineetta al posto del trattino) e porta in
    coda "(Pubblicato il ...)": serve da ripiego, non da fonte primaria.
    """
    from_title = clean_text(anchor.title, unescape=False)
    if from_title:
        return from_title
    return clean_text(_PUBLISHED_RE.sub(" ", anchor.text), unescape=False)


def _hash_moment(value: SourceDateTime | None) -> str | None:
    """Chiave stabile di una data per `metadata_hash`.

    Si usa l'istante risolto quando c'e', altrimenti il giorno, e solo in
    ultima istanza il testo grezzo: una riscrittura del formato da parte della
    fonte non deve apparire come una proroga (SPEC 4.2).
    """
    if value is None:
        return None
    if value.instant is not None:
        return value.instant.astimezone(timezone.utc).isoformat()
    if value.date_only is not None:
        return value.date_only.isoformat()
    return clean_text(value.raw, unescape=False)


def parse_detail(html_text: str, *, notice_id: str, source_url: str,
                 observed_at: datetime,
                 attachment_permission: AttachmentPermission | None = None,
                 ) -> NoticeSnapshot:
    """Estrae uno `NoticeSnapshot` dall'HTML della scheda pubblica.

    `attachment_permission` riceve l'URL di ogni allegato e risponde
    `(permesso, motivo)`. Quando manca, ogni allegato resta non prelevabile con
    motivo `UNEVALUATED_PERMISSION`: il riferimento e' comunque valido e
    completo, solo nessuno ha ancora verificato le condizioni di accesso
    (SPEC 3.1).

    Le anomalie sono dati del risultato, non eccezioni: una scheda senza stato
    o con una data illeggibile va acquisita e marcata, perche' scartarla
    silenziosamente e' il falso "nessun concorso" vietato da M0-08. L'unica
    condizione che solleva `ParseError` e' il documento che non e' una scheda.
    """
    if len(html_text) > MAX_DETAIL_CHARS:
        raise ParseError(
            f"Scheda oltre il limite accettato: {len(html_text)} caratteri "
            f"su un massimo di {MAX_DETAIL_CHARS}"
        )

    scanner = _DetailScanner()
    scanner.feed(html_text)
    scanner.close()

    if not scanner.label_seen:
        raise ParseError(
            "Nessuna etichetta 'concorso-label' nel documento: non e' una "
            "scheda di dettaglio (pagina di errore, redirect o pagina non "
            "renderizzata)"
        )

    anomalies: list[Anomaly] = []
    provenance = Provenance(source_url=source_url, observed_at=observed_at,
                            locator="scheda pubblica di dettaglio")

    # Etichetta ripetuta: vince la prima occorrenza. Sceglierne un'altra
    # sarebbe arbitrario e unirle produrrebbe un valore che la scheda non
    # mostra da nessuna parte.
    by_label: dict[str, _LabelledField] = {}
    for item in scanner.fields:
        if item.label:
            by_label.setdefault(item.label, item)

    title = scanner.title
    if title is None:
        anomalies.append(Anomaly(
            code="scheda_senza_titolo",
            message="Nessun <h1> nella scheda: senza titolo il bando non e' "
                    "presentabile ne' verificabile",
            severity=AnomalySeverity.BLOCKING,
        ))

    description = _first_text(by_label.get(LABEL_DESCRIPTION))
    procedure_type = _first_text(by_label.get(LABEL_PROCEDURE))

    status_normalized, status_original = normalize_status(
        _first_text(by_label.get(LABEL_STATUS))
    )
    if status_original is None:
        anomalies.append(Anomaly(
            code="stato_assente",
            message="Stato non leggibile nella scheda: resta UNKNOWN, non "
                    "viene dedotto dalle date di apertura e chiusura",
            severity=AnomalySeverity.WARNING,
        ))

    opening_at = _read_date(_first_text(by_label.get(LABEL_OPENING)),
                            LABEL_OPENING, anomalies)
    closing_at = _read_date(_first_text(by_label.get(LABEL_CLOSING)),
                            LABEL_CLOSING, anomalies)
    total_positions = parse_int(_first_text(by_label.get(LABEL_POSITIONS)))

    area_raw = _first_text(by_label.get(LABEL_AREA))
    territories = parse_area_geografica(
        area_raw,
        # La scheda non dichiara se "Area geografica" sia sede di lavoro,
        # ambito del bando o sede delle prove: SPEC 6 vieta di sceglierne una.
        scope=TerritoryScope.UNSPECIFIED,
        provenance=Provenance(source_url=source_url, observed_at=observed_at,
                              locator=LABEL_AREA),
    ) if area_raw else ()

    enti_raw = _first_text(by_label.get(LABEL_ENTI))
    # Il separatore comprende lo spazio, come nella scheda: una denominazione
    # con una virgola interna resterebbe comunque spezzata, ed e' il limite
    # noto di questo campo, che non distingue un ente da un elenco di enti.
    enti = tuple(part.strip() for part in enti_raw.split(", ")
                 if part.strip()) if enti_raw else ()

    pa_field = by_label.get(LABEL_PA_LINK)
    # L'href e' il valore vero: il testo mostrato e' lo stesso URL ma troncato
    # o ri-codificato dal tema, quindi non e' detto sia richiedibile.
    pa_link = _first_href(pa_field) or _first_text(pa_field)

    attachments = _read_attachments(
        by_label.get(LABEL_ATTACHMENTS), notice_id=notice_id,
        source_url=source_url, observed_at=observed_at,
        attachment_permission=attachment_permission, anomalies=anomalies,
    )

    extra_fields: dict[str, str] = {}
    for label, item in by_label.items():
        if label in MAPPED_LABELS:
            continue
        value = _first_text(item)
        anomalies.append(Anomaly(
            code="etichetta_non_mappata",
            message=f"Etichetta non prevista dal parser: '{label}'. Il valore "
                    f"e' conservato in extra_fields, non interpretato",
            severity=AnomalySeverity.INFO,
            context=value,
        ))
        if value is not None:
            extra_fields[label] = value

    present = {
        LABEL_DESCRIPTION: description,
        LABEL_AREA: area_raw,
        LABEL_STATUS: status_original,
        LABEL_CLOSING: closing_at,
        LABEL_POSITIONS: total_positions,
        LABEL_ENTI: enti_raw,
    }
    valorizzate = sum(1 for item in by_label.values() if _first_text(item) is not None)
    if len(by_label) >= _MIN_LABELS_FOR_RATIO and valorizzate * 2 < len(by_label):
        # Molte etichette, quasi nessun valore: la scheda non e' povera, non e'
        # piu' interpretabile. Sulle schede reali il rapporto e' 1 a 1 (da 8 a
        # 11 etichette, altrettanti valori); rinominando la classe dei
        # contenitori di valore diventa 1 su 10.
        #
        # Serve una BLOCKING perche' altrimenti la scheda passa per valida e
        # svuotata, e il confronto con la versione precedente annuncia scadenza
        # sparita, posti azzerati e allegati rimossi: un cambio di tema del
        # sito trasformato in notifiche di aggiornamento (M0-08). Le anomalie
        # per singolo campo, qui sotto, restano INFO: una per una sono
        # innocue, il problema si vede solo nel rapporto fra le due quantita'.
        anomalies.append(Anomaly(
            code="valori_non_estratti",
            message=f"{len(by_label)} etichette trovate ma solo {valorizzate} "
                    "con un valore leggibile: la struttura della scheda non e' "
                    "piu' quella attesa",
            severity=AnomalySeverity.BLOCKING,
            context=", ".join(sorted(by_label)[:6]),
        ))

    for label in EXPECTED_LABELS:
        if present[label] is not None:
            continue
        # Si guarda il valore ottenuto e non la sola etichetta: per chi legge
        # lo snapshot "etichetta assente" e "etichetta senza valore" sono la
        # stessa cosa, cioe' un campo atteso che non c'e'.
        anomalies.append(Anomaly(
            code="campo_atteso_assente",
            message=f"Campo atteso senza valore utilizzabile: '{label}'",
            severity=AnomalySeverity.INFO,
            context=("etichetta assente" if label not in by_label
                     else "etichetta presente, valore vuoto o non leggibile"),
        ))

    fingerprint = metadata_hash({
        "title": title,
        "description": description,
        "status": status_normalized.value,
        "status_original": status_original,
        "opening_at": _hash_moment(opening_at),
        "closing_at": _hash_moment(closing_at),
        "total_positions": total_positions,
        "enti": list(enti),
        # Territori e allegati sono ordinati: un riordino della scheda non
        # cambia cio' che il bando offre, mentre un elemento in piu' o in meno
        # si', ed e' l'unica delle due cose che deve muovere l'hash (SPEC 4.2).
        "territories": sorted(value.raw for value in territories),
        "attachments": sorted(ref.url for ref in attachments),
    })

    return NoticeSnapshot(
        notice_id=notice_id,
        source_url=source_url,
        observed_at=observed_at,
        status_original=status_original,
        status_normalized=status_normalized,
        title=title,
        description=description,
        enti=enti,
        procedure_type=procedure_type,
        total_positions=total_positions,
        opening_at=opening_at,
        closing_at=closing_at,
        # published_at resta assente: vedi il modulo. "Data apertura
        # candidature" non e' una data di pubblicazione (SPEC 4.1).
        published_at=None,
        territories=territories,
        attachments=attachments,
        pa_link=pa_link,
        extra_fields=extra_fields,
        metadata_hash=fingerprint,
        raw_document_hash=document_hash(html_text),
        anomalies=tuple(anomalies),
        provenance=provenance,
    )


def _read_attachments(item: _LabelledField | None, *, notice_id: str,
                      source_url: str, observed_at: datetime,
                      attachment_permission: AttachmentPermission | None,
                      anomalies: list[Anomaly]) -> tuple[AttachmentRef, ...]:
    """Costruisce i riferimenti agli allegati dell'etichetta dedicata.

    Si leggono solo i link dentro i blocchi `concorso-content`: e' la forma
    osservata su tutti i 36 allegati campionati, e restringerla a quella evita
    che un link decorativo comparso nella stessa zona della pagina diventi un
    documento del bando.
    """
    if item is None:
        return ()

    refs: list[AttachmentRef] = []
    for block in item.blocks:
        if block.kind != _KIND_CONTENT:
            continue
        for anchor in block.anchors:
            # L'URL si conserva come pubblicato: normalizzarlo significherebbe
            # richiedere una risorsa diversa da quella indicata dalla fonte.
            url = (anchor.href or "").strip()
            if not url:
                continue

            filename = _attachment_filename(anchor)
            marker = _PUBLISHED_RE.search(anchor.text)
            published = _read_date(
                marker["when"] if marker else None,
                f"{LABEL_ATTACHMENTS} {filename or url}", anomalies,
            )

            if attachment_permission is None:
                permitted, reason = False, UNEVALUATED_PERMISSION
            else:
                permitted, reason = attachment_permission(url)

            refs.append(AttachmentRef(
                notice_id=notice_id,
                url=url,
                provenance=Provenance(source_url=source_url,
                                      observed_at=observed_at,
                                      locator=LABEL_ATTACHMENTS),
                filename=filename,
                published=published,
                declared_extension=_declared_extension(filename),
                fetch_permitted=permitted,
                fetch_block_reason=reason,
            ))
    return tuple(refs)
