"""Implementazione di `SourceAdapter` sulla rotta verificata (SPEC 4).

Questo file non aggiunge conoscenza sulla fonte: la mette insieme. La
scoperta viene da `sitemap.py`, la scheda da `detail.py`, le condizioni di
accesso da `robots.py` e il ritmo da `http.py`. Qui si decide soltanto come
quei pezzi si compongono, e tre di queste decisioni vanno dichiarate perche'
sono scelte e non dettagli implementativi.

**I filtri che non si possono applicare vengono dichiarati, non simulati.**
La sitemap espone `loc` e `lastmod`: nient'altro. Stato, regione, provincia e
testo stanno nella scheda, cioe' a valle della ricerca, quindi `search` li
elenca in `SearchPage.unapplied_filters` invece di applicarli a caso o di
restituire un elenco vuoto lasciando credere che il filtro abbia funzionato
(SPEC 4, M0-03). Un filtro ignorato in silenzio e' peggio di un filtro
assente: il chiamante non ha modo di accorgersene.

**Gli allegati non si scaricano, e il motivo e' un dato.** Tutti gli
allegati osservati nei campioni stanno su `portale.inpa.gov.it`, che dichiara
`Disallow: /` per ogni agente. `fetch_attachment` su un riferimento non
consentito non fa alcuna richiesta e restituisce `FETCH_BLOCKED` con la
regola che lo ha impedito: SPEC 3.1 vieta di aggirare una condizione
dichiarata dalla fonte, e M0-10 chiede che il limite resti visibile nei dati
invece di sparire in un log.

**La paginazione e' locale.** La sitemap arriva in un'unica risposta, quindi
paginare a colpi di richieste sarebbe solo traffico in piu' verso la fonte:
l'elenco si legge una volta, resta nell'istanza e `page_cursor` scorre la
copia in memoria. Il cursore e' un indice, ma resta una stringa opaca per il
chiamante: che sia un numero e' una comodita' di questo adattatore, non una
promessa del contratto.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .detail import UNEVALUATED_PERMISSION, build_detail_url, parse_detail
from .errors import (
    ContentTypeError,
    HttpStatusError,
    ParseError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    TransportError,
    UnsafeUrlError,
)
from .http import HttpClient, HttpConfig
from .models import (
    Anomaly,
    AnomalySeverity,
    AttachmentPayload,
    AttachmentRef,
    ExtractionState,
    NoticeSnapshot,
    NoticeStub,
    SearchFilters,
    SearchPage,
)
from .normalize import document_hash
from .robots import RobotsPolicy
from .sitemap import (
    SITEMAP_INDEX_URL,
    entries_to_stubs,
    parse_sitemap_index,
    parse_urlset_detailed,
)

ATTACHMENT_HOST = "portale.inpa.gov.it"
"""Host che serve gli allegati: nei campioni del 19/09/2026, senza eccezioni.

E' scritto qui come costante e non dedotto dagli URL della scheda per una
ragione di sicurezza: le condizioni di accesso di un host si leggono solo per
host noti. Interrogare il robots.txt di un indirizzo qualunque letto
nell'HTML significherebbe trasformare il contenuto della pagina in una
richiesta di rete, che e' esattamente cio' che SPEC 11 chiede di impedire.
"""

SITEMAP_BANDI_MARKER = "bandi"
"""Come si riconosce la sitemap dei bandi fra quelle dell'indice.

L'indice osservato elenca `sitemap-pages.xml`, `sitemap-posts.xml` e
`sitemap-bandi.xml`. Il riconoscimento per nome e' fragile per costruzione, ed
e' una fragilita' voluta: se la fonte rinomina il file, `search` fallisce in
modo esplicito invece di restituire zero bandi come se non ce ne fossero.
"""

XML_ACCEPT = "application/xml,text/xml;q=0.9,*/*;q=0.8"
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"

_SNIFF_BYTES = 64
"""Quanti byte iniziali guarda `detect_mime`. Le firme note stanno nei primi
cinque; il margine serve solo a saltare un BOM e qualche spazio."""

_GENERIC_MIMES = frozenset({
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
})
"""Tipi che non affermano nulla sul contenuto e quindi non possono
contraddire la firma dei byte."""

_MIME_COMPATIBLE: dict[str, frozenset[str]] = {
    # I formati OOXML e ODF sono archivi ZIP: la firma non puo' distinguerli
    # dal contenitore, quindi un .docx dichiarato per esteso non e' una
    # contraddizione. Il campione conteneva un DOCX servito cosi'.
    "application/zip": frozenset({
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/epub+zip",
        "application/x-zip-compressed",
    }),
    # I .p7m osservati sono DER: la firma dice "SEQUENCE" e nulla piu', mentre
    # i server annunciano quel formato con nomi diversi fra loro.
    "application/pkcs7-mime": frozenset({
        "application/x-pkcs7-mime",
        "application/pkcs7-signature",
        "application/x-pkcs7-signature",
    }),
    "application/rtf": frozenset({"text/rtf", "application/x-rtf"}),
    "text/html": frozenset({"application/xhtml+xml", "text/xml", "application/xml"}),
}
"""Tipi dichiarati che restano coerenti con una firma diversa.

La chiave e' il tipo *rilevato*, i valori sono i tipi *dichiarati* che quella
firma puo' legittimamente descrivere.
"""


def detect_mime(data: bytes) -> str | None:
    """Tipo dedotto dai primi byte, `None` se la firma non e' riconosciuta.

    Serve a confrontare cio' che il server dichiara con cio' che ha davvero
    mandato (SPEC 11). Il caso concreto da intercettare e' quello di SPEC A.1:
    una pagina di errore HTML archiviata come se fosse il PDF del bando.

    Il riconoscimento e' per prefisso esatto e non restituisce mai un tipo
    "probabile": un file che non inizia con una firma nota resta `None`, cioe'
    un dato assente, e in quanto tale non contraddice nulla (SPEC 4.1).

    Un documento XML viene riconosciuto come `text/html`: distinguerli non
    serve allo scopo di questa funzione, che e' accorgersi che al posto di un
    documento e' arrivato del markup.
    """
    if not data:
        return None
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if data.startswith(b"{\\rtf"):
        return "application/rtf"
    # DER: SEQUENCE (0x30) con lunghezza su due byte (0x82). E' la forma dei
    # .p7m osservati; non collide con il markup, perche' '0' e' 0x30 ma il
    # byte successivo di un testo non e' 0x82.
    if data.startswith(b"\x30\x82"):
        return "application/pkcs7-mime"
    head = data[:_SNIFF_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return "text/html"
    return None


def _mime_consistent(declared: str, detected: str) -> bool:
    """Se tipo dichiarato e firma dei byte possono descrivere lo stesso file.

    Non e' un confronto di stringhe: un .docx e' un archivio ZIP e un .p7m e'
    un DER, quindi la firma non puo' che vedere il contenitore. Trattare quei
    casi come contraddizioni riempirebbe i rapporti di falsi allarmi e
    renderebbe invisibile l'unico caso che conta, il markup servito al posto
    di un documento.
    """
    if declared == detected:
        return True
    if declared in _GENERIC_MIMES:
        return True
    return declared in _MIME_COMPATIBLE.get(detected, frozenset())


def build_default_client(*, allow_attachment_host: str | None = None) -> HttpClient:
    """Client con gli host della rotta verificata; la deroga e' esplicita.

    Senza argomenti il client parla solo con `www.inpa.gov.it` e
    `inpa.gov.it` e rispetta il robots.txt di entrambi: e' il comportamento
    predefinito ed e' l'unico per cui le condizioni sono state verificate.

    `allow_attachment_host` non e' un interruttore: e' il testo
    dell'autorizzazione ricevuta dal gestore (per esempio "autorizzazione via
    email di <ufficio>, <data>"). Passarlo aggiunge `portale.inpa.gov.it` agli
    host ammessi e registra la deroga nel `RobotsPolicy`, dove resta visibile
    in ogni decisione come `origin="override"` invece di sparire in una
    configurazione silenziosa.

    **Va usato solo a fronte di un'autorizzazione documentata del gestore.**
    Quell'host dichiara `Disallow: /` per ogni agente; la domanda posta al
    gestore e il perimetro che ne consegue sono in
    `reports/access_conditions.md` (§2.2 e §5). Inventare una motivazione per
    far passare i download sarebbe l'elusione vietata da SPEC 3.1.
    """
    base = HttpConfig()
    if allow_attachment_host is None:
        return HttpClient(base)

    authorisation = allow_attachment_host.strip()
    if not authorisation:
        # Una deroga senza motivazione non e' tracciabile: chi la legge fra sei
        # mesi non saprebbe distinguerla da una svista (M0-09, M0-10).
        raise ValueError(
            "allow_attachment_host deve contenere la motivazione "
            "dell'autorizzazione ricevuta, non una stringa vuota"
        )

    config = replace(base, allowed_hosts=base.allowed_hosts | {ATTACHMENT_HOST})
    client = HttpClient(config)
    # Il fetcher e' quello del client, non uno nuovo: anche il robots.txt
    # dell'host in deroga deve passare per ritmo, timeout e tetto di byte gia'
    # configurati li' dentro.
    client.robots = RobotsPolicy(
        fetcher=client._fetch_robots_txt,  # noqa: SLF001
        user_agent=config.user_agent,
        authorized_hosts={ATTACHMENT_HOST: authorisation},
    )
    return client


@dataclass(frozen=True, slots=True)
class _Catalogue:
    """L'elenco letto dalla sitemap, con quanto serve a giudicarne la qualita'.

    `declared_total` conta le voci `<url>` del documento, non gli stub
    prodotti: e' un conteggio dichiarato dalla fonte e resta tale anche se la
    conversione ne ha scartata qualcuna (SPEC 4).
    """

    stubs: tuple[NoticeStub, ...]
    declared_total: int
    complete: bool
    anomalies: tuple[Anomaly, ...]
    source_url: str
    observed_at: datetime


def _unapplicable_filters(filters: SearchFilters) -> tuple[str, ...]:
    """Filtri che la scoperta non e' in grado di applicare.

    Tutti e quattro richiedono la scheda: la sitemap non pubblica ne' stato,
    ne' territorio, ne' titolo. `updated_since` non compare qui perche' il
    `lastmod` c'e' e viene usato davvero.
    """
    names: list[str] = []
    if filters.text:
        names.append("text")
    if filters.statuses:
        names.append("statuses")
    if filters.region_codes:
        names.append("region_codes")
    if filters.province_codes:
        names.append("province_codes")
    return tuple(names)


def _select_updated_since(stubs: tuple[NoticeStub, ...],
                          moment: datetime) -> tuple[tuple[NoticeStub, ...], int]:
    """Tiene le voci il cui `lastmod` non e' anteriore a `moment`.

    Le voci senza un istante confrontabile restano dentro e vengono contate a
    parte: escluderle significherebbe dichiararle vecchie sulla base di un
    dato che la fonte non ha pubblicato (SPEC 4.1). Il conteggio serve a chi
    legge la pagina per sapere quanto e' larga la maglia.

    Un `moment` senza fuso viene letto come UTC: e' l'orologio con cui il bot
    marca le proprie osservazioni, la stessa convenzione di `snapshot.py`, e
    non una data dichiarata da inPA.
    """
    threshold = (moment if moment.tzinfo is not None
                 else moment.replace(tzinfo=timezone.utc))

    kept: list[NoticeStub] = []
    uncomparable = 0
    for stub in stubs:
        published = stub.source_last_modified
        instant = published.instant if published is not None else None
        if instant is None:
            uncomparable += 1
            kept.append(stub)
            continue
        if instant >= threshold:
            kept.append(stub)
    return tuple(kept), uncomparable


def _parse_cursor(page_cursor: str | None) -> int:
    """Interpreta il cursore prodotto da questo adattatore.

    Un cursore malformato e' un errore del chiamante e non va corretto: farlo
    ripartire da zero in silenzio riconsegnerebbe pagine gia' lette come se
    fossero nuove.
    """
    if page_cursor is None:
        return 0
    try:
        start = int(page_cursor)
    except ValueError as exc:
        raise ValueError(
            f"Cursore non riconosciuto da {InpaSitemapAdapter.name}: "
            f"{page_cursor!r}"
        ) from exc
    if start < 0:
        raise ValueError(f"Cursore negativo: {page_cursor!r}")
    return start


class InpaSitemapAdapter:
    """Acquisizione dei bandi da `www.inpa.gov.it` via sitemap e scheda.

    Soddisfa il Protocol `SourceAdapter` di `models.py`: il resto del bot
    dipende da quello, non da questa classe, cosi' che cambiare rotta di
    acquisizione significhi scrivere un'altra implementazione e non toccare
    l'applicazione (SPEC 4).
    """

    name = "inpa-www-sitemap"

    def __init__(self, client: HttpClient | None = None, *,
                 sitemap_index_url: str = SITEMAP_INDEX_URL,
                 page_size: int = 100) -> None:
        if page_size < 1:
            raise ValueError(f"page_size deve essere almeno 1: {page_size}")
        self.client = client if client is not None else build_default_client()
        self.sitemap_index_url = sitemap_index_url
        self.page_size = page_size
        self._catalogue_cache: _Catalogue | None = None
        self._previous_total: int | None = None

    # -- scoperta ---------------------------------------------------------

    def refresh(self) -> None:
        """Scarta il catalogo in cache: la prossima `search` rilegge la sitemap.

        La cache non ha scadenza automatica. E' una scelta: la cadenza di
        riacquisizione appartiene a chi orchestra la sincronizzazione
        (SPEC 5.2), e un adattatore di lunga vita non deve decidere da solo
        che i dati di stamattina sono ancora buoni.
        """
        self._catalogue_cache = None

    def search(self, filters: SearchFilters,
               page_cursor: str | None = None) -> SearchPage:
        """Una pagina di `NoticeStub` presi dalla sitemap dei bandi.

        La sitemap si scarica una volta sola per istanza e la paginazione
        avviene sulla copia in memoria: `page_cursor` e' l'indice della prima
        voce della pagina, come stringa. Il cursore vale per la sequenza
        prodotta dagli **stessi** filtri: cambiare `updated_since` fra due
        chiamate cambia la sequenza, quindi il cursore precedente non ha piu'
        significato.

        `updated_since` e' l'unico filtro applicabile qui, e va letto per
        quello che e': il `lastmod` e' la data che la fonte dichiara sulla
        pagina, non la data in cui il bando e' cambiato. Un bando prorogato
        senza che la sua pagina venga ripubblicata non emergerebbe da questo
        filtro, motivo per cui SPEC 5.2 chiede comunque una riconciliazione
        periodica dell'intero perimetro.

        `declared_total` resta il conteggio delle voci pubblicate dalla fonte
        anche quando `updated_since` ne ha scartate: e' un dato della sitemap,
        non il numero dei record restituiti.
        """
        catalogue = self._catalogue()

        anomalies: list[Anomaly] = list(catalogue.anomalies)
        # Le anomalie del catalogo accompagnano ogni pagina, non solo la
        # prima: chi legge una pagina sola deve poter sapere che l'elenco da
        # cui viene era incompleto.

        unapplied = _unapplicable_filters(filters)
        if unapplied:
            anomalies.append(Anomaly(
                code="filtri_non_applicati",
                message="Filtri non applicabili in fase di scoperta: "
                        f"{', '.join(unapplied)}. La sitemap non espone stato, "
                        "territorio ne' titolo: vanno applicati dal chiamante "
                        "sulle schede, non qui (SPEC 4, M0-03)",
                severity=AnomalySeverity.WARNING,
            ))

        records = catalogue.stubs
        if filters.updated_since is not None:
            records, uncomparable = _select_updated_since(
                records, filters.updated_since
            )
            if uncomparable:
                anomalies.append(Anomaly(
                    code="lastmod_non_confrontabile",
                    message=f"{uncomparable} voci senza un lastmod risolto sono "
                            "state tenute nel risultato: senza la data "
                            "pubblicata dalla fonte non e' possibile dirle "
                            "anteriori alla soglia richiesta",
                    severity=AnomalySeverity.WARNING,
                    context=filters.updated_since.isoformat(),
                ))

        start = _parse_cursor(page_cursor)
        window = records[start:start + self.page_size]
        end = start + len(window)
        next_cursor = str(end) if end < len(records) else None

        return SearchPage(
            records=window,
            complete=catalogue.complete,
            next_cursor=next_cursor,
            declared_total=catalogue.declared_total,
            unapplied_filters=unapplied,
            anomalies=tuple(anomalies),
        )

    def _catalogue(self) -> _Catalogue:
        if self._catalogue_cache is None:
            self._catalogue_cache = self._load_catalogue()
        return self._catalogue_cache

    def _load_catalogue(self) -> _Catalogue:
        """Legge indice e sitemap dei bandi, una richiesta ciascuno."""
        observed_at = datetime.now(timezone.utc)

        index = self.client.get(self.sitemap_index_url, accept=XML_ACCEPT)
        locations = parse_sitemap_index(index.text())
        candidates = [loc for loc in locations
                      if SITEMAP_BANDI_MARKER in loc.lower()]
        if not candidates:
            # Non e' "nessun bando": e' la rotta di scoperta che non c'e' piu'.
            # Le due condizioni vanno tenute separate (SPEC 5.3, M0-08).
            raise ParseError(
                f"Nessuna sitemap dei bandi in {self.sitemap_index_url}: "
                f"{len(locations)} sitemap elencate, nessuna con "
                f"'{SITEMAP_BANDI_MARKER}' nell'indirizzo"
            )

        anomalies: list[Anomaly] = []
        if len(candidates) > 1:
            anomalies.append(Anomaly(
                code="sitemap_bandi_multiple",
                message="L'indice elenca piu' di una sitemap dei bandi: letta "
                        "solo la prima, quindi una parte del perimetro "
                        "pubblicato non e' stata nemmeno richiesta",
                severity=AnomalySeverity.BLOCKING,
                context=" ".join(candidates),
            ))

        # L'indirizzo arriva da un documento della fonte, quindi non e' fidato:
        # passa comunque dai controlli di `http.py` (schema, host ammesso,
        # indirizzo pubblico) prima di diventare una richiesta.
        document = self.client.get(candidates[0], accept=XML_ACCEPT)
        entries, skipped = parse_urlset_detailed(document.text())
        if skipped:
            # Le voci mutile non sono rappresentabili, ma tacerle falserebbe il
            # totale: il conteggio finirebbe per descrivere cio' che e'
            # sopravvissuto invece di cio' che la fonte ha pubblicato.
            anomalies.append(Anomaly(
                code="sitemap_voci_senza_loc",
                message=f"{skipped} voci <url> prive di <loc> sono state saltate: "
                        "il documento ne dichiarava di piu' di quante se ne "
                        "possano leggere",
                severity=AnomalySeverity.WARNING,
                context=document.final_url,
            ))
        if not entries:
            anomalies.append(Anomaly(
                code="sitemap_bandi_vuota",
                message="Nessuna voce <url> nella sitemap dei bandi: puo' voler "
                        "dire che il documento non e' un <urlset> oppure che "
                        "la fonte ha pubblicato un elenco vuoto. Da qui le due "
                        "cose non sono distinguibili e nessuna delle due "
                        "permette di dichiarare completa l'acquisizione",
                severity=AnomalySeverity.BLOCKING,
                context=document.final_url,
            ))

        stubs, conversion = entries_to_stubs(
            entries, source_url=document.final_url, observed_at=observed_at
        )
        anomalies.extend(conversion)

        if entries and not stubs:
            # La sitemap c'e', ha voci, e nessuna di esse produce un id
            # utilizzabile: nella pratica significa che la fonte ha cambiato la
            # forma degli URL di dettaglio. Le singole conversioni fallite sono
            # gia' segnalate, ma come WARNING: presi uno per uno sono innocui,
            # tutti insieme fanno "zero bandi, scansione completa", che e'
            # l'esito che M0-08 vieta di produrre per un guasto. Serve qui una
            # BLOCKING, perche' solo guardando l'insieme si vede il problema.
            anomalies.append(Anomaly(
                code="sitemap_nessun_id_utilizzabile",
                message=f"Nessuna delle {len(entries)} voci della sitemap "
                        "espone un id utilizzabile: l'elenco non e' vuoto, e' "
                        "illeggibile. Non equivale a 'nessun bando'",
                severity=AnomalySeverity.BLOCKING,
                context=document.final_url,
            ))
        elif entries and len(stubs) < len(entries) // 2:
            # Meta' del perimetro persa non e' un incidente isolato: e' un
            # sintomo. Resta WARNING perche' cio' che si e' letto e' valido.
            anomalies.append(Anomaly(
                code="sitemap_perdita_massiva",
                message=f"Solo {len(stubs)} voci su {len(entries)} hanno "
                        "prodotto un id utilizzabile: verificare se la fonte "
                        "ha cambiato la forma degli URL",
                severity=AnomalySeverity.WARNING,
                context=document.final_url,
            ))

        totale = len(entries) + skipped
        precedente = self._previous_total
        if precedente is not None and totale * 2 < precedente:
            # La sitemap si e' dimezzata fra due letture di questo stesso
            # processo. Le cause plausibili sono tutte guasti -- un file
            # rigenerato a meta', una cache stantia, un deploy in corso --
            # e nessuna di esse significa che i bandi mancanti siano stati
            # revocati (SPEC 4.2). Dichiararlo BLOCKING impedisce a chi
            # sincronizza di prendere questa lettura per un perimetro valido
            # e di concludere che migliaia di bandi sono spariti (SPEC 5.2).
            #
            # Il confronto vive nell'istanza e non in archivio: copre il caso
            # reale, un processo che rilegge periodicamente. Al primo giro
            # dopo un riavvio non c'e' termine di paragone, ed e' un limite
            # dichiarato nei rapporti invece che mascherato da un valore
            # atteso inventato.
            anomalies.append(Anomaly(
                code="sitemap_crollo_improvviso",
                message=f"La sitemap e' passata da {precedente} a {totale} voci "
                        "fra due letture: un calo simile e' molto piu' "
                        "probabilmente un guasto della fonte che la scadenza "
                        "di tutti quei bandi",
                severity=AnomalySeverity.BLOCKING,
                context=document.final_url,
            ))
        self._previous_total = totale

        # Una regola sola per la completezza, ed e' gia' scritta nella
        # tassonomia: `BLOCKING` significa per definizione "non si puo'
        # considerare completa" (models.AnomalySeverity).
        return _Catalogue(
            stubs=stubs,
            declared_total=totale,
            complete=not any(anomaly.blocking for anomaly in anomalies),
            anomalies=tuple(anomalies),
            source_url=document.final_url,
            observed_at=observed_at,
        )

    # -- scheda -----------------------------------------------------------

    def fetch_notice(self, notice_id: str) -> NoticeSnapshot:
        """Scarica e interpreta la scheda pubblica di un bando.

        Il controllo sul tipo di contenuto viene prima del parser per il caso
        di M0-08: una pagina di cortesia, un redirect verso la home o una
        risposta di errore servita con stato 200 non devono poter diventare un
        bando con tutti i campi vuoti. Se non e' HTML non si prova nemmeno a
        leggerla.
        """
        url = build_detail_url(notice_id)
        response = self.client.get(url, accept=HTML_ACCEPT)

        if response.content_type != "text/html":
            raise ContentTypeError(
                f"La scheda {url} e' arrivata come "
                f"{response.content_type or 'tipo non dichiarato'}: una "
                "risposta che non e' HTML non viene interpretata come un bando"
            )

        html_text, sostituito = response.decoded()
        snapshot = parse_detail(
            html_text,
            notice_id=notice_id,
            # L'URL finale e non quello richiesto: il documento letto sta li',
            # e attribuirlo all'indirizzo di partenza nasconderebbe un
            # eventuale salto che `http.py` ha gia' verificato e registrato.
            source_url=response.final_url,
            # L'istante della risposta, non quello di adesso: e' il momento in
            # cui il bot ha osservato la scheda (SPEC 4.2).
            observed_at=response.fetched_at,
            attachment_permission=self._attachment_permission,
        )
        if sostituito:
            # Il testo si legge, ma qualche byte e' stato sostituito: il
            # contenuto letto non coincide con quello pubblicato. Non basta a
            # buttare la scheda -- il grosso resta valido -- ma deve risultare,
            # perche' altrimenti una differenza introdotta dalla decodifica
            # sarebbe indistinguibile da una modifica del bando.
            snapshot = replace(snapshot, anomalies=snapshot.anomalies + (Anomaly(
                code="decodifica_con_sostituzioni",
                message="Alcuni byte non erano interpretabili con il charset "
                        f"dichiarato ({response.charset or 'non dichiarato'}) e "
                        "sono stati sostituiti: il testo letto non coincide "
                        "byte per byte con quello pubblicato",
                severity=AnomalySeverity.WARNING,
                context=response.final_url,
            ),))
        return snapshot

    def list_attachments(self, notice_id: str) -> tuple[AttachmentRef, ...]:
        """Riferimenti agli allegati dichiarati dalla scheda.

        Costa una richiesta intera perche' la scheda e' l'unico posto in cui
        gli allegati sono elencati: non esiste un indirizzo che restituisca i
        soli allegati di un bando. I riferimenti tornano con le condizioni di
        accesso gia' valutate, quindi oggi con `fetch_permitted=False`.
        """
        return self.fetch_notice(notice_id).attachments

    def _attachment_permission(self, url: str) -> tuple[bool, str | None]:
        """Condizioni di accesso a un allegato, per `parse_detail` (SPEC 3.1).

        L'ordine dei controlli e' voluto. Prima schema e host, che si
        verificano senza toccare la rete; solo per un host gia' noto si
        interroga il robots.txt, perche' chiederlo a un indirizzo qualunque
        letto nella pagina sarebbe gia' una richiesta guidata da contenuto non
        fidato (SPEC 11).

        L'ultimo controllo evita una promessa falsa: se una deroga rendesse
        l'host consentito dal robots ma nessuno lo avesse aggiunto agli host
        ammessi del client, `fetch_permitted=True` annuncerebbe un download
        che poi verrebbe rifiutato.
        """
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            # `urlsplit` accetta l'URL ma `.port` solleva su una porta non
            # numerica: un href della scheda puo' contenerla, e la scheda non
            # e' contenuto fidato.
            return False, f"indirizzo non richiedibile: {url!r}"

        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https" or not host:
            return False, f"indirizzo non richiedibile: {url!r}"
        if port not in (None, 443):
            # Senza questo controllo `https://www.inpa.gov.it:8443/...` — un
            # href che l'host giusto rende credibile — risulterebbe
            # prelevabile: l'host e' fra quelli ammessi e il robots di quel
            # nome verrebbe interrogato. Il download poi fallirebbe comunque
            # in `validate_url`, ma l'allegato resterebbe archiviato come
            # `fetch_permitted=True`, cioe' un permesso mai concesso scritto
            # nei dati. La porta fa parte dell'origine, e un'origine diversa
            # e' un'altra fonte.
            return False, f"porta non ammessa ({port}): {url!r}"

        allowed_hosts = self.client.config.allowed_hosts
        if host not in allowed_hosts and host != ATTACHMENT_HOST:
            return False, (
                f"host fuori dal perimetro verificato ({host}): le sue "
                "condizioni di accesso non sono state valutate"
            )

        try:
            decision = self.client.robots.check(url)
        except Exception as exc:  # noqa: BLE001 - vedi sotto
            # Nessun guasto nella lettura delle condizioni deve far fallire
            # l'intera scheda: senza verdetto l'allegato resta non prelevabile,
            # che e' il rifiuto prudente gia' adottato da `robots.py`.
            return False, f"condizioni di accesso non verificabili: {exc}"

        if not decision.allowed:
            return False, decision.rule
        if host not in allowed_hosts:
            return False, (
                f"robots consente ({decision.rule}) ma {host} non e' fra gli "
                "host ammessi dal client"
            )
        return True, None

    # -- allegati ---------------------------------------------------------

    def fetch_attachment(self, ref: AttachmentRef) -> AttachmentPayload:
        """Scarica un allegato, se le condizioni osservate lo consentono.

        Nessun esito di questo metodo e' un'eccezione. Un documento non
        prelevabile o non recuperato e' un dato dell'acquisizione (SPEC 8.2):
        far fallire la scansione per un allegato illeggibile farebbe perdere
        anche i bandi letti correttamente.
        """
        if not ref.fetch_permitted:
            # Nessuna richiesta di rete, nemmeno per provare. La condizione era
            # gia' stata valutata quando il riferimento e' stato osservato, e
            # riprovarci sperando in un esito diverso e' l'elusione che SPEC
            # 3.1 vieta. Il motivo viaggia nel payload, non in un log.
            return AttachmentPayload(
                ref=ref,
                state=ExtractionState.FETCH_BLOCKED,
                anomalies=(Anomaly(
                    code="allegato_non_prelevabile",
                    message="Allegato non scaricato: le condizioni di accesso "
                            "dichiarate dalla fonte non lo consentono",
                    severity=AnomalySeverity.WARNING,
                    context=ref.fetch_block_reason or UNEVALUATED_PERMISSION,
                ),),
            )

        try:
            response = self.client.get(ref.url, accept="*/*")
        except RobotsDisallowedError as exc:
            # Il robots.txt puo' essere cambiato fra l'osservazione della
            # scheda e il download: lo stato resta FETCH_BLOCKED, perche' e'
            # una condizione della fonte e non un guasto.
            return AttachmentPayload(
                ref=ref,
                state=ExtractionState.FETCH_BLOCKED,
                anomalies=(Anomaly(
                    code="allegato_non_prelevabile",
                    message=f"Allegato vietato al momento del download: {exc}",
                    severity=AnomalySeverity.WARNING,
                    context=exc.rule,
                ),),
            )
        except (TransportError, HttpStatusError, ResponseTooLargeError,
                UnsafeUrlError) as exc:
            return AttachmentPayload(
                ref=ref,
                state=ExtractionState.FETCH_FAILED,
                anomalies=(Anomaly(
                    code="allegato_non_recuperato",
                    message=f"Allegato non recuperato: {exc}",
                    severity=AnomalySeverity.WARNING,
                    # La classe dell'errore resta nel dato: trasporto, stato
                    # HTTP e limite di dimensione sono tre cose diverse e
                    # SPEC 5.3 chiede di non confonderle.
                    context=type(exc).__name__,
                ),),
            )

        data = response.body
        declared = response.content_type or None
        detected = detect_mime(data)

        anomalies: list[Anomaly] = []
        if (declared is not None and detected is not None
                and not _mime_consistent(declared, detected)):
            anomalies.append(Anomaly(
                code="mime_incoerente",
                message=f"Tipo dichiarato '{declared}' e firma dei byte "
                        f"'{detected}' non coincidono: il contenuto non e' "
                        "quello annunciato. Il file resta com'e' arrivato, non "
                        "viene rinominato in base al tipo dichiarato "
                        "(SPEC 11, SPEC A.1)",
                severity=AnomalySeverity.WARNING,
                context=ref.filename or ref.url,
            ))

        # Anche sul corpo vuoto si conservano hash e dimensione: sono
        # osservazioni, e lo sha256 di zero byte distingue "scaricato vuoto"
        # da "non scaricato".
        return AttachmentPayload(
            ref=ref,
            state=ExtractionState.EMPTY if not data else ExtractionState.OK,
            content=data,
            sha256=document_hash(data),
            size_bytes=len(data),
            declared_mime=declared,
            detected_mime=detected,
            # `ETag` e `Last-Modified` si registrano solo se ci sono: SPEC 4.2
            # avverte di non darli per garantiti.
            http_etag=response.headers.get("etag"),
            http_last_modified=response.headers.get("last-modified"),
            fetched_at=response.fetched_at,
            anomalies=tuple(anomalies),
        )
