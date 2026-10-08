"""Lettura delle sitemap XML di inPA: la rotta di scoperta del progetto.

Perche' la sitemap e non la ricerca del portale: `portale.inpa.gov.it`
pubblica `Disallow: /` per ogni user-agent, quindi l'endpoint `search-better`
indicato dalla specifica (2.1) non e' utilizzabile da un processo automatico.
`www.inpa.gov.it` invece consente la lettura e dichiara esso stesso la propria
sitemap, che rimanda a `sitemap-bandi.xml`. E' l'unica scoperta massiva
consentita dalle condizioni della fonte (SPEC 3.1, 3.3).

Cosa la sitemap NON fornisce, e va detto qui perche' a valle non si veda:

* nessun titolo, nessuno stato, nessun territorio. Un `NoticeStub` prodotto da
  questo modulo ha ID, URL e `lastmod`: aprire la scheda resta obbligatorio.
* nessuno storico. Nel campione del 19/09/2026 le 1680 voci presenti erano
  tutte in stato "Aperto" e le due schede chiuse note non comparivano. Un ID
  che sparisce dalla sitemap non e' quindi ne' revocato ne' eliminato
  (SPEC 4.2): e' solo uscito dal perimetro pubblicato.
* nessuna garanzia di completezza istantanea. La conta delle voci lette va
  confrontata con il `SyncRun`, non assunta.

Gli URL pubblicati usano l'host `inpa.gov.it`, mentre le schede si servono su
`www.inpa.gov.it`: la riscrittura avviene in `entries_to_stubs`, non al
momento della richiesta, cosi' da non pagare un redirect per ogni bando.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .errors import ParseError
from .models import Anomaly, AnomalySeverity, Certainty, NoticeStub, Provenance
from .normalize import parse_iso_datetime

SITEMAP_INDEX_URL = "https://www.inpa.gov.it/sitemap.xml"

NOTICE_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
"""Forma dell'identificativo `concorso_id` osservata su tutte le voci.

Serve a *riconoscere* un id, non a costruirlo: la specifica (2.2) impone di
usare gli id restituiti dalla fonte come stringhe opache e vieta di
enumerarli. Le maiuscole sono accettate ma non normalizzate, perche'
cambiare la stringa significherebbe cambiare l'identita' del bando.
"""

MAX_SITEMAP_CHARS = 16 * 1024 * 1024
"""Tetto al testo accettato dal parser.

`sitemap-bandi.xml` pesava circa 420 KB con 1680 voci: il limite lascia una
quarantina di volte quel volume ed e' molto sotto il massimo del protocollo
(50 MB), quindi non e' un vincolo pratico sulla crescita della fonte.
"""

_DOCTYPE_RE = re.compile(r"<!\s*DOCTYPE", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """Una voce `<url>` come e' scritta nel documento.

    I campi restano stringhe grezze: interpretare `lastmod` qui significherebbe
    perdere il testo originale, che SPEC 4.1 chiede di conservare accanto al
    valore normalizzato.
    """

    loc: str
    lastmod_raw: str | None
    changefreq: str | None
    priority: str | None


def _local(tag: str) -> str:
    """Nome dell'elemento senza namespace.

    Il namespace `sitemaps.org/schemas/sitemap/0.9` c'e' nei documenti inPA ma
    non e' obbligatorio in tutti i feed: confrontare il tag completo renderebbe
    il parser fragile verso una variante che lo omette.
    """
    return tag.rpartition("}")[2]


def _first_child_text(element: ET.Element, name: str) -> str | None:
    """Testo del primo figlio con quel nome locale, o `None` se assente/vuoto."""
    for child in element:
        if _local(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _parse_document(xml_text: str) -> ET.Element:
    """Interpreta il documento imponendo i limiti di SPEC 11.

    Due difese, entrambe necessarie per motivi diversi:

    * il tetto di caratteri limita la memoria occupata dall'albero, che per
      ElementTree e' un multiplo della dimensione del testo;
    * il rifiuto di qualunque `<!DOCTYPE` toglie di mezzo la vulnerabilita'
      documentata di `xml.etree` all'espansione delle entita' ("billion
      laughs"), che richiede un DTD interno per dichiararle. Una sitemap non
      ha alcun motivo legittimo di portarne uno.

    Residuo consapevole: restano possibili la crescita quadratica su attributi
    molto lunghi e il costo di un albero profondo, entrambi limitati solo dal
    tetto sopra. L'input viene da un host `gov.it` su TLS, ma resta un
    documento non affidabile: nessun valore letto qui deve poter guidare una
    richiesta di rete senza passare dai controlli di `http.py`.
    """
    if len(xml_text) > MAX_SITEMAP_CHARS:
        raise ParseError(
            f"Sitemap oltre il limite accettato: {len(xml_text)} caratteri "
            f"su un massimo di {MAX_SITEMAP_CHARS}"
        )
    if _DOCTYPE_RE.search(xml_text):
        raise ParseError("Sitemap con DOCTYPE: rifiutata senza interpretarla")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        # La ParseError di ElementTree non deve uscire da questo livello:
        # a valle si distingue "documento non letto" da "nessun risultato"
        # (SPEC 5.3) e la distinzione passa dalla tassonomia di errors.py.
        raise ParseError(f"XML della sitemap non interpretabile: {exc}") from exc


def parse_sitemap_index(xml_text: str) -> tuple[str, ...]:
    """Restituisce le `<loc>` dei `<sitemap>` elencati nell'indice.

    Tupla vuota se la radice non e' un `<sitemapindex>`: un documento diverso
    non contiene sitemap figlie, e dedurne una per somiglianza sarebbe un
    valore inventato.
    """
    root = _parse_document(xml_text)
    if _local(root.tag) != "sitemapindex":
        return ()

    locations: list[str] = []
    for node in root:
        if _local(node.tag) != "sitemap":
            continue
        loc = _first_child_text(node, "loc")
        if loc is not None:
            locations.append(loc)
    return tuple(locations)


def parse_urlset_detailed(xml_text: str) -> tuple[tuple[SitemapEntry, ...], int]:
    """Come `parse_urlset`, ma dice anche quante voci sono state saltate.

    Il conteggio serve a chi deve giudicare la completezza. Un `<url>` senza
    `<loc>` non e' rappresentabile e va saltato, ma se lo si tace il
    chiamante calcola il totale su cio' che *e' sopravvissuto* e ottiene un
    numero coerente con se stesso e sbagliato rispetto al documento: una
    sitemap con meta' delle voci mutile sembrerebbe una sitemap piccola e
    sana.
    """
    root = _parse_document(xml_text)
    if _local(root.tag) != "urlset":
        return (), 0

    entries: list[SitemapEntry] = []
    skipped = 0
    for node in root:
        if _local(node.tag) != "url":
            continue
        loc = _first_child_text(node, "loc")
        if loc is None:
            skipped += 1
            continue
        entries.append(SitemapEntry(
            loc=loc,
            lastmod_raw=_first_child_text(node, "lastmod"),
            changefreq=_first_child_text(node, "changefreq"),
            priority=_first_child_text(node, "priority"),
        ))
    return tuple(entries), skipped


def parse_urlset(xml_text: str) -> tuple[SitemapEntry, ...]:
    """Legge i `<url>` di un `<urlset>`.

    Un `<url>` privo di `<loc>` utilizzabile viene saltato: la posizione *e'*
    il contenuto della voce, quindi non resta nulla da rappresentare. Radice
    diversa da `<urlset>` significa nessuna voce, non un errore.

    Chi deve valutare la completezza usi `parse_urlset_detailed`, che riporta
    anche quante voci sono state saltate.
    """
    entries, _ = parse_urlset_detailed(xml_text)
    return entries


def extract_notice_id(loc: str) -> str | None:
    """Estrae `concorso_id` dalla query string, o `None`.

    L'id si legge solo dal parametro: ricavarlo dal percorso, o accettare una
    forma diversa da quella osservata, produrrebbe identita' di comodo proprio
    dove SPEC 2.2 chiede una stringa opaca restituita dalla fonte.
    """
    try:
        query = urllib.parse.urlsplit(loc).query
    except ValueError:
        return None

    values = urllib.parse.parse_qs(query).get("concorso_id", [])
    # Piu' valori diversi per lo stesso parametro sono ambigui: scegliere il
    # primo sarebbe indovinare a quale bando punta il link.
    if len(set(values)) != 1:
        return None

    candidate = values[0]
    return candidate if NOTICE_ID_RE.fullmatch(candidate) else None


def _canonical_source_url(loc: str, canonical_host: str) -> tuple[str, bool]:
    """Porta `loc` sull'host canonico quando si tratta dello stesso sito.

    La riscrittura e' limitata al caso `inpa.gov.it` / `www.inpa.gov.it`: un
    host estraneo resta intatto, perche' spostarlo su `www.inpa.gov.it`
    fabbricherebbe un URL che la fonte non ha mai pubblicato. Se un giorno
    comparisse davvero, verrebbe fermato dall'elenco degli host ammessi di
    `http.py` invece di essere mascherato qui.
    """
    try:
        parts = urllib.parse.urlsplit(loc)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return loc, False

    target = canonical_host.lower()
    if not host or host == target:
        return loc, False
    if host.removeprefix("www.") != target.removeprefix("www."):
        return loc, False

    netloc = canonical_host if port is None else f"{canonical_host}:{port}"
    rewritten = urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    return rewritten, True


def entries_to_stubs(entries: Iterable[SitemapEntry], *, source_url: str,
                     observed_at: datetime,
                     canonical_host: str = "www.inpa.gov.it",
                     ) -> tuple[tuple[NoticeStub, ...], tuple[Anomaly, ...]]:
    """Converte le voci in `NoticeStub`, riportando cosa non ha funzionato.

    Le anomalie sono restituite invece che registrate: una scoperta che ha
    saltato delle voci non e' una scoperta riuscita, e chi la riceve deve
    poterlo sapere prima di avanzare un cursore (SPEC 5.2, M0-08).

    Nota sui due URL: `NoticeStub.source_url` e' la scheda del bando, mentre
    `Provenance.source_url` e' la sitemap da cui l'abbiamo saputo. Il `loc`
    originale resta in `Provenance.locator`, cosi' la riscrittura dell'host
    e' verificabile e non silenziosa.
    """
    stubs: list[NoticeStub] = []
    anomalies: list[Anomaly] = []
    seen: dict[str, str] = {}

    for entry in entries:
        notice_id = extract_notice_id(entry.loc)
        if notice_id is None:
            anomalies.append(Anomaly(
                code="sitemap_loc_senza_id",
                message="Voce di sitemap senza un concorso_id utilizzabile: "
                        "saltata senza attribuirle un identificativo",
                severity=AnomalySeverity.WARNING,
                context=entry.loc,
            ))
            continue

        if notice_id in seen:
            anomalies.append(Anomaly(
                code="sitemap_id_duplicato",
                message=f"Identificativo {notice_id} gia' presente nella stessa "
                        f"sitemap: tenuta la prima occorrenza ({seen[notice_id]})",
                severity=AnomalySeverity.WARNING,
                context=entry.loc,
            ))
            continue
        seen[notice_id] = entry.loc

        detail_url, rewritten = _canonical_source_url(entry.loc, canonical_host)

        stubs.append(NoticeStub(
            notice_id=notice_id,
            source_url=detail_url,
            provenance=Provenance(
                source_url=source_url,
                observed_at=observed_at,
                locator=entry.loc,
                certainty=Certainty.OBSERVED,
                note=("host riscritto su quello canonico delle schede"
                      if rewritten else None),
            ),
            source_last_modified=parse_iso_datetime(entry.lastmod_raw),
        ))

    return tuple(stubs), tuple(anomalies)
