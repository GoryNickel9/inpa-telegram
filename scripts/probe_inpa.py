"""Prova riproducibile del livello di acquisizione M0 (consegna di SPEC 3.5).

Comando esatto della prova, quello citato nei rapporti (M0-09):

    python scripts/probe_inpa.py --out evidence/ --limit 5

Lo scopo non e' riassumere a parole che l'acquisizione funziona: e' lasciare
sul disco i corpi grezzi scaricati, una riga per ogni richiesta fatta e un
riepilogo degli esiti, cosi' che chi legge i rapporti possa rifare la stessa
verifica e confrontare i file invece di fidarsi del testo.

Cinque passi, nell'ordine in cui le condizioni vanno accertate:

1. condizioni di accesso: `robots.txt` dei due host coinvolti e decisione
   calcolata per un URL rappresentativo di ciascuno (SPEC 3.1, M0-10);
2. scoperta: indice delle sitemap, sitemap dei bandi, conteggio delle voci;
3. paginazione: due pagine consecutive di `search()` e scansione fino in
   fondo, con la sovrapposizione degli id (M0-02);
4. schede: apertura di `--limit` schede, campi estratti e anomalie (M0-04);
5. allegati: esito della valutazione delle condizioni di accesso, allegato
   per allegato, senza scaricarne nessuno.

Cosa questa prova NON fa, di proposito: non scarica allegati.
`portale.inpa.gov.it`, che li ospita tutti, dichiara `Disallow: /` per ogni
agente. La deroga nel codice esiste
(`build_default_client(allow_attachment_host=...)`) ma non e' esposta come
opzione da riga di comando: richiede un'autorizzazione scritta del gestore,
discussa in `reports/access_conditions.md`, non un flag digitato in fretta.
L'unica richiesta che questa prova invia a quell'host e' il suo `robots.txt`,
cioe' il documento con cui l'host stesso dichiara il divieto.

I file prodotti contengono anche gli header di risposta: la cartella viene
creata con permessi ristretti dove il sistema operativo li applica, e il
contenuto va sanitizzato prima di essere condiviso (SPEC Appendice A).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# La prova deve funzionare su un repository appena clonato, prima di
# `pip install -e .`: e' il primo comando che esegue chi vuole verificare i
# rapporti, e chiedergli un'installazione riuscita sposterebbe il primo
# possibile fallimento lontano dalla fonte (M0-09).
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from inpa_bot.source.adapter import ATTACHMENT_HOST, InpaSitemapAdapter  # noqa: E402
from inpa_bot.source.detail import build_detail_url  # noqa: E402
from inpa_bot.source.errors import (  # noqa: E402
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
from inpa_bot.source.http import (  # noqa: E402
    HttpClient,
    HttpConfig,
    RequestRecord,
    Response,
)
from inpa_bot.source.models import (  # noqa: E402
    Anomaly,
    AttachmentRef,
    NoticeSnapshot,
    NoticeStub,
    SearchFilters,
    SearchPage,
    SourceDateTime,
    TerritoryValue,
)
from inpa_bot.source.robots import RobotsFile  # noqa: E402
from inpa_bot.source.sitemap import SITEMAP_INDEX_URL, parse_sitemap_index  # noqa: E402

COMANDO_DI_RIFERIMENTO = "python scripts/probe_inpa.py --out evidence/ --limit 5"

CAMPIONE_SCHEDA = "d3d8d8d7a82e40afbd75a26841bc9070"
"""Scheda usata per calcolare la decisione robots su `www.inpa.gov.it`.

E' il campione locale gia' registrato in SPEC 2.4: serve un URL della forma
reale, non uno inventato, e sceglierne uno documentato rende la decisione
confrontabile con le verifiche precedenti. La scheda non viene scaricata in
questo passo, solo valutata.
"""

CAMPIONE_ALLEGATO = "https://portale.inpa.gov.it/api/media/83f5fa66-9038-4e7d-b09f-11e86f3956be"
"""Allegato usato per calcolare la decisione robots su `portale.inpa.gov.it`.

E' il PDF di SPEC 2.3/A.1, l'unico documento di quell'host di cui la
specifica registri un'apertura riuscita. Anche qui si valuta soltanto: il
download non avviene in nessun caso.
"""

PAGINA_PROVA = 10
"""Dimensione di pagina usata per la prova di paginazione.

Piu' piccola del default dell'adattatore (100) per una ragione di leggibilita'
delle prove: due pagine da 10 id stanno in un `summary.json` che una persona
puo' rileggere. Non cambia il numero di richieste, perche' la paginazione e'
locale (vedi `adapter.InpaSitemapAdapter.search`).
"""

NESSUN_RISULTATO = "nessun_risultato"
"""Esito senza record, tenuto distinto da qualunque errore (SPEC 5.3).

Un elenco vuoto non e' un guasto della fonte e non e' un documento
illeggibile: M0-08 chiede esattamente che le tre condizioni non collassino
in un unico messaggio.
"""

AVVISO_SANITIZZAZIONE = (
    "I file di questa cartella contengono header di risposta e documenti "
    "integrali della fonte: sanitizzarli prima di condividerli o allegarli "
    "a un rapporto (SPEC Appendice A). La cartella e' esclusa dal "
    "versionamento in .gitignore."
)

_NON_SICURI = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# Classificazione degli esiti
# --------------------------------------------------------------------------

_CLASSI_ERRORE: tuple[tuple[type[BaseException], str], ...] = (
    # L'ordine conta: le sottoclassi piu' specifiche prima di `SourceError`.
    (RobotsDisallowedError, "vietato_da_robots"),
    (TransportError, "errore_di_trasporto"),
    (HttpStatusError, "errore_http"),
    (ContentTypeError, "contenuto_inatteso"),
    (ResponseTooLargeError, "risposta_troppo_grande"),
    (UnsafeUrlError, "url_rifiutato"),
    (ParseError, "documento_non_interpretabile"),
    (IncompleteAcquisitionError, "acquisizione_incompleta"),
    (SourceError, "errore_di_fonte"),
)


def classifica_errore(exc: BaseException) -> dict[str, Any]:
    """Traduce un'eccezione nella classe di errore che le compete (SPEC 5.3).

    Le classi restano separate perche' le azioni che richiedono sono diverse:
    un errore di trasporto si ritenta, un `403` interrompe il percorso, un
    divieto di robots.txt non va ritentato affatto. Appiattirle in "errore
    della fonte" e' il primo passo verso il falso "nessun concorso" vietato
    da M0-08, e la specifica (1.3) vieta in particolare di riclassificare un
    guasto DNS come rifiuto della fonte.
    """
    classe = "errore_imprevisto"
    for tipo, nome in _CLASSI_ERRORE:
        if isinstance(exc, tipo):
            classe = nome
            break

    dato: dict[str, Any] = {
        "classe": classe,
        "eccezione": type(exc).__name__,
        "messaggio": str(exc),
    }
    if isinstance(exc, HttpStatusError):
        dato["stato_http"] = exc.status
        dato["url"] = exc.url
        if exc.retry_after is not None:
            dato["retry_after_s"] = exc.retry_after
    if isinstance(exc, RobotsDisallowedError):
        dato["url"] = exc.url
        dato["regola"] = exc.rule
    return dato


@dataclass
class Passo:
    """Esito di un passo della prova, come finira' in `summary.json`."""

    numero: int
    nome: str
    ok: bool = True
    dettaglio: dict[str, Any] = field(default_factory=dict)
    problemi: list[dict[str, Any]] = field(default_factory=list)
    note: list[str] = field(default_factory=list)

    def fallisci(self, dato: dict[str, Any]) -> None:
        self.ok = False
        self.problemi.append(dato)

    def come_json(self) -> dict[str, Any]:
        return {
            "numero": self.numero,
            "nome": self.nome,
            "esito": "ok" if self.ok else "fallito",
            "dettaglio": self.dettaglio,
            "problemi": self.problemi,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# Cartella delle prove e client che conserva i corpi
# --------------------------------------------------------------------------


def prepara_cartella(percorso: Path) -> str:
    """Crea la cartella delle prove nel modo piu' chiuso che il SO consente.

    `umask` va impostata prima di qualunque scrittura, non solo prima della
    `mkdir`: vale per ogni file creato dopo, compresi i corpi scaricati, che
    sono cio' che davvero puo' contenere header e cookie (SPEC Appendice A).

    Su Windows i bit POSIX non descrivono i permessi reali: dichiararlo e'
    piu' utile che chiamare `chmod` e lasciar credere a una protezione che
    non c'e'.
    """
    os.umask(0o077)
    percorso.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        return ("permessi POSIX non applicabili su questo sistema: la cartella "
                "eredita le ACL del profilo utente, da verificare a mano")
    try:
        percorso.chmod(0o700)
        modo = stat.S_IMODE(percorso.stat().st_mode)
    except OSError as exc:
        return f"permessi ristretti non applicati: {exc}"
    return f"permessi applicati: {modo:#o}"


def _sicuro(testo: str) -> str:
    return _NON_SICURI.sub("-", testo).strip("-")


def nome_prova(indice: int, url: str) -> str:
    """Nome di file leggibile e deterministico per un corpo salvato.

    Il numero d'ordine davanti serve a ricostruire la sequenza delle
    richieste guardando la cartella, senza aprire `requests.jsonl`. Le schede
    prendono il proprio `concorso_id` perche' e' l'identificativo con cui
    compaiono ovunque nei rapporti.
    """
    parti = urllib.parse.urlsplit(url)
    identificativi = urllib.parse.parse_qs(parti.query).get("concorso_id", [])
    if len(set(identificativi)) == 1:
        return f"{indice:02d}-scheda-{_sicuro(identificativi[0])}.html"
    coda = parti.path.rsplit("/", 1)[-1] or "risposta"
    return f"{indice:02d}-{_sicuro(parti.hostname or 'host')}-{_sicuro(coda)}"


class ClientConProve(HttpClient):
    """`HttpClient` che conserva su disco il corpo e gli header di ogni risposta.

    Sta in questo script e non in `http.py` perche' scrivere file e' un
    bisogno della prova, non dell'acquisizione: il client usato dal bot non
    deve avere un ramo di codice che tocca il disco.

    Il vantaggio di ereditare invece di duplicare le richieste e' misurabile
    in traffico verso la fonte: anche le richieste fatte dall'adattatore
    lasciano una copia grezza, quindi la prova non deve riscaricare la
    sitemap solo per poterla allegare.
    """

    def __init__(self, cartella: Path, config: HttpConfig | None = None) -> None:
        super().__init__(config)
        self.cartella = cartella
        self.salvati: list[dict[str, Any]] = []
        self._per_url: dict[str, Path] = {}

    def get(self, url: str, *, accept: str = "*/*",
            check_robots: bool = True) -> Response:
        risposta = super().get(url, accept=accept, check_robots=check_robots)
        nome = nome_prova(len(self.salvati) + 1, risposta.final_url)
        corpo = self.cartella / nome
        corpo.write_bytes(risposta.body)
        intestazioni = self.cartella / f"{nome}.headers.json"
        scrivi_json(intestazioni, {
            "url_richiesto": risposta.url,
            "url_finale": risposta.final_url,
            "stato": risposta.status,
            "redirect": list(risposta.redirects),
            "header": risposta.headers,
        })
        self.salvati.append({
            "url_richiesto": risposta.url,
            "url_finale": risposta.final_url,
            "stato": risposta.status,
            "byte": len(risposta.body),
            "file_corpo": nome,
            "file_header": intestazioni.name,
        })
        # Registrato sotto entrambi gli URL: dopo un redirect chi cerca la
        # prova conosce l'indirizzo richiesto, non quello finale.
        self._per_url[risposta.url] = corpo
        self._per_url[risposta.final_url] = corpo
        return risposta

    def prova_per(self, url: str) -> Path | None:
        return self._per_url.get(url)


# --------------------------------------------------------------------------
# Serializzazione dei tipi del contratto
# --------------------------------------------------------------------------


def scrivi_json(percorso: Path, dato: Any) -> None:
    # `newline=""` disattiva la traduzione dei fine riga: su Windows i file di
    # prova sarebbero altrimenti diversi, byte per byte, da quelli prodotti
    # altrove, e confrontarli fra due macchine e' proprio il punto di M0-09.
    percorso.write_text(
        json.dumps(dato, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8", newline="",
    )


def _istante(momento: datetime | None) -> str | None:
    return momento.isoformat() if momento is not None else None


def _data(valore: SourceDateTime | None) -> dict[str, Any] | None:
    """Rappresenta una data della fonte senza completarla.

    `istante` resta `None` quando la fonte pubblica la sola data: e' il punto
    in cui SPEC 4.1 vieta di inventare le 23:59, e un riepilogo che lo
    nascondesse renderebbe invisibile proprio il caso da sorvegliare.
    """
    if valore is None:
        return None
    return {
        "grezza": valore.raw,
        "data": valore.date_only.isoformat() if valore.date_only else None,
        "istante": _istante(valore.instant),
        "orario_esplicito": valore.has_explicit_time,
        "fuso_assunto": valore.tz_assumption,
        "certezza": valore.certainty.value,
        "nota": valore.note,
    }


def _anomalie(anomalie: tuple[Anomaly, ...]) -> list[dict[str, Any]]:
    return [{
        "codice": a.code,
        "severita": a.severity.value,
        "messaggio": a.message,
        "contesto": a.context,
    } for a in anomalie]


def _territorio(valore: TerritoryValue) -> dict[str, Any]:
    return {
        "grezzo": valore.raw,
        "ambito": valore.scope.value,
        "nazionale": valore.national,
        "regione": valore.region_name,
        "codice_regione": valore.region_code,
        "provincia": valore.province_name,
        "codice_provincia": valore.province_code,
        "normalizzato": valore.normalized,
        "certezza": valore.certainty.value,
    }


def _stub(stub: NoticeStub) -> dict[str, Any]:
    return {
        "id": stub.notice_id,
        "url": stub.source_url,
        "lastmod": _data(stub.source_last_modified),
    }


def _riga_richiesta(record: RequestRecord, origine: str,
                    file_corpo: str | None) -> dict[str, Any]:
    """Una riga di `requests.jsonl`: cosa e' stato chiesto e come e' andata."""
    return {
        "origine": origine,
        "iniziata_il": _istante(record.started_at),
        "metodo": record.method,
        "url": record.url,
        "stato": record.status,
        "errore": record.error,
        "durata_s": round(record.elapsed_s, 4),
        "byte": record.bytes_read,
        "content_type": record.content_type,
        "regola_robots": record.robots_rule,
        "tentativo": record.attempt,
        "file_corpo": file_corpo,
    }


# --------------------------------------------------------------------------
# Passo 1 - condizioni di accesso
# --------------------------------------------------------------------------


def passo_condizioni(client: ClientConProve, cartella: Path,
                     ) -> tuple[Passo, list[RequestRecord]]:
    """Legge e registra il `robots.txt` dei due host, con la decisione calcolata.

    Il `robots.txt` viene chiesto tramite `RobotsPolicy`, che e' la stessa
    strada percorsa da ogni richiesta successiva: interrogarlo per conto
    proprio darebbe una prova di un documento diverso da quello che il client
    usera' davvero. La contropartita e' che `HttpClient` non registra quella
    richiesta fra le proprie, perche' avviene sotto il livello che tiene il
    registro: la prova la cronometra e la aggiunge a `requests.jsonl` con
    `origine: "prova"`, cosi' che il conteggio delle richieste inviate alla
    fonte resti completo.

    `portale.inpa.gov.it` non e' fra gli host ammessi del client e non deve
    diventarlo: il suo `robots.txt` si legge per registrarne il divieto, che
    e' l'unico modo onesto di documentare perche' gli allegati non si
    scaricano (SPEC 3.1, M0-10).
    """
    passo = Passo(1, "condizioni di accesso (robots.txt)")
    richieste: list[RequestRecord] = []

    bersagli = (
        ("www.inpa.gov.it", build_detail_url(CAMPIONE_SCHEDA), True),
        (ATTACHMENT_HOST, CAMPIONE_ALLEGATO, False),
    )

    esiti: list[dict[str, Any]] = []
    for host, rappresentativo, deve_essere_consentito in bersagli:
        url_robots = f"https://{host}/robots.txt"
        iniziata = datetime.now(timezone.utc)
        avvio = time.monotonic()
        robots: RobotsFile = client.robots.robots_for(url_robots)
        durata = time.monotonic() - avvio

        file_robots = cartella / f"robots-{_sicuro(host)}.txt"
        file_robots.write_text(robots.raw, encoding="utf-8", newline="")

        richieste.append(RequestRecord(
            url=url_robots, method="GET", status=robots.status,
            error=None if robots.reachable else "robots non recuperato",
            elapsed_s=durata, bytes_read=len(robots.raw.encode("utf-8")),
            started_at=iniziata, content_type="text/plain",
            robots_rule="richiesta esente: e' il robots.txt stesso",
        ))

        decisione = client.robots.check(rappresentativo)
        ritardo = client.robots.crawl_delay(url_robots)

        esito = {
            "host": host,
            "robots_txt": url_robots,
            "stato_http": robots.status,
            "raggiungibile": robots.reachable,
            "byte": len(robots.raw.encode("utf-8")),
            "file_prova": file_robots.name,
            "crawl_delay_dichiarato_s": ritardo,
            "sitemap_dichiarate": list(robots.sitemaps),
            "gruppi_user_agent": sorted(robots.groups),
            "url_valutato": rappresentativo,
            "decisione": {
                "consentito": decisione.allowed,
                "regola": decisione.rule,
                "origine": decisione.origin,
                "letto_il": _istante(decisione.fetched_at),
            },
        }
        esiti.append(esito)

        if not robots.reachable:
            # Nessuno stato HTTP significa guasto sotto il livello applicativo:
            # non e' un rifiuto della fonte (SPEC 1.3).
            passo.fallisci({
                "classe": "errore_di_trasporto",
                "messaggio": f"{url_robots} non recuperato: senza le condizioni "
                             "dichiarate dall'host la prova non puo' proseguire "
                             "su quell'host",
            })
        elif robots.status != 200:
            passo.fallisci({
                "classe": "errore_http",
                "stato_http": robots.status,
                "messaggio": f"{url_robots} ha risposto {robots.status}: la "
                             "verifica delle condizioni non e' conclusa",
            })
        elif deve_essere_consentito and not decisione.allowed:
            passo.fallisci({
                "classe": "vietato_da_robots",
                "regola": decisione.rule,
                "messaggio": f"{rappresentativo} risulta vietato: la rotta di "
                             "acquisizione adottata non e' piu' praticabile e "
                             "va fermata, non aggirata (SPEC 3.1)",
            })

        if not deve_essere_consentito and decisione.allowed:
            # Nessun fallimento: sarebbe una condizione migliore di quella
            # osservata. Va comunque notata, perche' cambierebbe le conclusioni
            # dei rapporti sugli allegati.
            passo.note.append(
                f"{host} non risulta piu' vietato per {rappresentativo}: "
                "rileggere reports/access_conditions.md prima di trarne "
                "conseguenze, la valutazione delle condizioni non si esaurisce "
                "nel robots.txt"
            )

    passo.dettaglio["host"] = esiti
    passo.note.append(
        "Il download degli allegati resta disattivato in questa prova "
        "indipendentemente dall'esito qui sopra: si attiva solo con "
        "build_default_client(allow_attachment_host=...), cioe' con "
        "un'autorizzazione documentata del gestore."
    )
    return passo, richieste


# --------------------------------------------------------------------------
# Passo 2 - scoperta dalla sitemap
# --------------------------------------------------------------------------


def passo_sitemap(adapter: InpaSitemapAdapter, client: ClientConProve,
                  anteprima: int) -> tuple[Passo, SearchPage | None]:
    """Legge indice e sitemap dei bandi e riporta conteggio e prime voci.

    L'elenco delle sitemap si rilegge dal file gia' scritto in `--out` e non
    dalla risposta in memoria: se il riepilogo divergesse dal file allegato,
    la prova avrebbe documentato qualcosa che nessuno puo' piu' controllare.
    """
    passo = Passo(2, "scoperta: indice delle sitemap e sitemap dei bandi")

    try:
        pagina = adapter.search(SearchFilters())
    except SourceError as exc:
        passo.fallisci(classifica_errore(exc))
        return passo, None

    dettaglio: dict[str, Any] = {
        "indice": SITEMAP_INDEX_URL,
        "richieste_effettuate": [
            {"url": s["url_finale"], "stato": s["stato"], "byte": s["byte"],
             "file_corpo": s["file_corpo"]}
            for s in client.salvati
        ],
        "voci_dichiarate_dalla_sitemap": pagina.declared_total,
        "stub_nella_prima_pagina": len(pagina.records),
        "pagina_completa": pagina.complete,
        "filtri_non_applicati": list(pagina.unapplied_filters),
        "anomalie": _anomalie(pagina.anomalies),
    }

    file_indice = client.prova_per(SITEMAP_INDEX_URL)
    if file_indice is None:
        dettaglio["sitemap_elencate"] = None
        passo.note.append(
            "Indice non ritrovato fra le prove salvate: l'adattatore potrebbe "
            "essere stato configurato con un indice diverso da quello atteso"
        )
    else:
        testo = file_indice.read_text(encoding="utf-8", errors="replace")
        try:
            dettaglio["sitemap_elencate"] = list(parse_sitemap_index(testo))
        except ParseError as exc:
            dettaglio["sitemap_elencate"] = None
            passo.fallisci(classifica_errore(exc))

    dettaglio["prime_voci"] = [_stub(s) for s in pagina.records[:anteprima]]
    passo.dettaglio = dettaglio

    if not pagina.records:
        # Tre condizioni separate, e questa e' la terza: nessun errore, nessun
        # documento illeggibile, semplicemente nessun record (SPEC 5.3).
        passo.fallisci({
            "classe": NESSUN_RISULTATO,
            "messaggio": "La sitemap dei bandi non ha prodotto nessuno stub. "
                         "Non e' un errore della fonte ne' un documento "
                         "illeggibile: e' un elenco vuoto, e come tale non "
                         "dimostra la scoperta richiesta da M0-01",
        })
    if not pagina.complete:
        passo.fallisci({
            "classe": "acquisizione_incompleta",
            "messaggio": "La scoperta riporta almeno un'anomalia bloccante: "
                         "l'elenco non puo' essere considerato completo e un "
                         "punto di ripresa non va avanzato (SPEC 5.2)",
        })
    return passo, pagina


# --------------------------------------------------------------------------
# Passo 3 - paginazione
# --------------------------------------------------------------------------


def passo_paginazione(adapter: InpaSitemapAdapter, client: ClientConProve) -> Passo:
    """Due pagine consecutive davvero diverse, poi la scansione fino in fondo.

    Oltre al criterio di M0-02 la prova registra quante richieste sono state
    inviate durante la paginazione. Il numero atteso e' zero, perche' la
    sitemap arriva in un'unica risposta e l'adattatore pagina sulla copia in
    memoria: se un giorno diventasse diverso da zero, la cadenza di
    sincronizzazione andrebbe ricalcolata e questa riga sarebbe il primo
    posto in cui accorgersene.
    """
    passo = Passo(3, "paginazione di search() (M0-02)")
    filtri = SearchFilters()
    prima_delle_richieste = len(client.records)

    try:
        prima = adapter.search(filtri)
        if prima.next_cursor is None:
            passo.dettaglio = {
                "id_prima_pagina": [s.notice_id for s in prima.records],
                "cursore_successivo": None,
            }
            passo.fallisci({
                "classe": NESSUN_RISULTATO,
                "messaggio": f"L'elenco pubblicato sta in una pagina sola "
                             f"({len(prima.records)} voci con pagine da "
                             f"{adapter.page_size}): non esiste una seconda "
                             "pagina da confrontare, quindi la prova di M0-02 "
                             "non e' producibile. Non e' un errore della fonte",
            })
            return passo

        seconda = adapter.search(filtri, prima.next_cursor)
    except (SourceError, ValueError) as exc:
        passo.fallisci(classifica_errore(exc))
        return passo

    id_prima = [s.notice_id for s in prima.records]
    id_seconda = [s.notice_id for s in seconda.records]
    sovrapposizione = sorted(set(id_prima) & set(id_seconda))

    # Scansione completa: l'unico modo di dimostrare che la paginazione
    # termina e non riconsegna sempre le stesse voci (M0-02). Non costa
    # richieste, quindi puo' arrivare fino in fondo invece di campionare.
    visti: list[str] = []
    pagine_lette = 0
    cursore: str | None = None
    tetto = 2 + (prima.declared_total or 0) // max(adapter.page_size, 1) + 2
    troncata = False
    while True:
        corrente = adapter.search(filtri, cursore)
        pagine_lette += 1
        visti.extend(s.notice_id for s in corrente.records)
        cursore = corrente.next_cursor
        if cursore is None:
            break
        if pagine_lette >= tetto:
            troncata = True
            break

    duplicati = sorted(x for x, quante in Counter(visti).items() if quante > 1)
    richieste_durante = len(client.records) - prima_delle_richieste

    passo.dettaglio = {
        "dimensione_pagina": adapter.page_size,
        "id_prima_pagina": id_prima,
        "id_seconda_pagina": id_seconda,
        "cursore_prima_pagina": prima.next_cursor,
        "cursore_seconda_pagina": seconda.next_cursor,
        "pagine_diverse": id_prima != id_seconda,
        "sovrapposizione_id": sovrapposizione,
        "scansione_completa": {
            "pagine_lette": pagine_lette,
            "id_letti": len(visti),
            "id_distinti": len(set(visti)),
            "duplicati": duplicati,
            "totale_dichiarato": prima.declared_total,
            "interrotta_al_tetto": troncata,
        },
        "richieste_durante_la_paginazione": richieste_durante,
        "anomalie_prima_pagina": _anomalie(prima.anomalies),
    }

    if not id_seconda:
        passo.fallisci({
            "classe": NESSUN_RISULTATO,
            "messaggio": "La seconda pagina e' vuota pur avendo un cursore: "
                         "l'elenco e' cambiato durante la lettura oppure il "
                         "cursore non e' coerente. Va investigato, non assunto "
                         "come fine dell'elenco (SPEC 5.2)",
        })
    elif id_prima == id_seconda:
        passo.fallisci({
            "classe": "paginazione_non_dimostrata",
            "messaggio": "Le due pagine consecutive contengono gli stessi id: "
                         "M0-02 chiede due pagine realmente differenti",
        })
    if sovrapposizione:
        passo.fallisci({
            "classe": "sovrapposizione_fra_pagine",
            "messaggio": f"{len(sovrapposizione)} id compaiono in entrambe le "
                         "pagine: puo' derivare da voci saltate o da un "
                         "aggiornamento concorrente, e in entrambi i casi va "
                         "investigato (SPEC A.2)",
        })
    if duplicati:
        passo.fallisci({
            "classe": "id_duplicati_nella_scansione",
            "messaggio": f"{len(duplicati)} id sono stati letti piu' di una "
                         "volta nella scansione completa",
        })
    if troncata:
        passo.fallisci({
            "classe": "acquisizione_incompleta",
            "messaggio": f"Scansione interrotta al tetto di {tetto} pagine "
                         "senza raggiungere la fine: la paginazione non "
                         "termina come previsto",
        })
    if richieste_durante:
        passo.note.append(
            f"La paginazione ha prodotto {richieste_durante} richieste di rete: "
            "l'adattatore non sta piu' paginando sulla copia in memoria e il "
            "costo di una scansione completa verso la fonte va rimisurato"
        )
    return passo


# --------------------------------------------------------------------------
# Passo 4 - schede
# --------------------------------------------------------------------------


def scegli_schede(pagina: SearchPage | None, richiesti: list[str],
                  limite: int) -> tuple[list[str], list[dict[str, Any]], str]:
    """Decide quali schede aprire, senza inventare identificativi.

    Gli id passati con `--notice-id` sostituiscono la selezione dalla sitemap
    e non sono soggetti a `--limit`: chi li elenca a mano sta chiedendo
    esattamente quelli, e troncarli silenziosamente renderebbe la prova
    diversa dal comando digitato. Un id di forma diversa da quella osservata
    viene rifiutato qui e non trasformato in una richiesta (SPEC 2.2).
    """
    if richiesti:
        buoni: list[str] = []
        scartati: list[dict[str, Any]] = []
        for identificativo in richiesti:
            try:
                build_detail_url(identificativo)
            except ValueError as exc:
                scartati.append({
                    "classe": "identificativo_rifiutato",
                    "messaggio": str(exc),
                })
                continue
            buoni.append(identificativo)
        return buoni, scartati, "--notice-id"

    if pagina is None:
        return [], [], "nessuna (scoperta non riuscita)"
    return [s.notice_id for s in pagina.records[:limite]], [], "sitemap"


def _riassunto_scheda(scheda: NoticeSnapshot) -> dict[str, Any]:
    """Campi estratti da una scheda, con i vuoti lasciati vuoti (SPEC 4.1)."""
    descrizione = scheda.description or ""
    return {
        "id": scheda.notice_id,
        "url": scheda.source_url,
        "osservata_il": _istante(scheda.observed_at),
        "titolo": scheda.title,
        "stato_originale": scheda.status_original,
        "stato_normalizzato": scheda.status_normalized.value,
        "enti": list(scheda.enti),
        "tipo_procedura": scheda.procedure_type,
        "posti_totali": scheda.total_positions,
        "apertura": _data(scheda.opening_at),
        "chiusura": _data(scheda.closing_at),
        "pubblicazione": _data(scheda.published_at),
        "aggiornamento_fonte": _data(scheda.source_updated_at),
        "territori": [_territorio(t) for t in scheda.territories],
        "territori_normalizzati": sum(1 for t in scheda.territories if t.normalized),
        "profili": len(scheda.positions),
        "allegati": len(scheda.attachments),
        "link_pa": scheda.pa_link,
        "descrizione_caratteri": len(descrizione),
        "descrizione_inizio": descrizione[:160] or None,
        # Le etichette non mappate sono il segnale che la fonte ha cambiato
        # la scheda: senza questa riga un campo nuovo passerebbe inosservato.
        "etichette_non_mappate": sorted(scheda.extra_fields),
        "metadata_hash": scheda.metadata_hash,
        "raw_document_hash": scheda.raw_document_hash,
        "anomalie": _anomalie(scheda.anomalies),
    }


def passo_schede(adapter: InpaSitemapAdapter, identificativi: list[str],
                 origine: str, scartati: list[dict[str, Any]],
                 ) -> tuple[Passo, list[NoticeSnapshot]]:
    """Apre le schede richieste e riassume campi e anomalie di ciascuna."""
    passo = Passo(4, "schede di dettaglio")
    for scarto in scartati:
        passo.fallisci(scarto)

    schede: list[NoticeSnapshot] = []
    riassunti: list[dict[str, Any]] = []

    for identificativo in identificativi:
        try:
            scheda = adapter.fetch_notice(identificativo)
        except SourceError as exc:
            dato = classifica_errore(exc)
            dato["notice_id"] = identificativo
            passo.fallisci(dato)
            continue
        schede.append(scheda)
        riassunti.append(_riassunto_scheda(scheda))
        if scheda.has_blocking_anomaly:
            passo.fallisci({
                "classe": "scheda_incompleta",
                "notice_id": identificativo,
                "messaggio": "La scheda porta un'anomalia bloccante: e' stata "
                             "acquisita e conservata, ma non puo' essere "
                             "considerata completa (M0-08)",
            })

    conteggio_anomalie: dict[str, int] = {}
    etichette_nuove: set[str] = set()
    for scheda in schede:
        for anomalia in scheda.anomalies:
            conteggio_anomalie[anomalia.code] = (
                conteggio_anomalie.get(anomalia.code, 0) + 1
            )
        etichette_nuove.update(scheda.extra_fields)

    passo.dettaglio = {
        "origine_della_selezione": origine,
        "richieste": len(identificativi),
        "lette": len(schede),
        "schede": riassunti,
        "anomalie_per_codice": dict(sorted(conteggio_anomalie.items())),
        "etichette_non_mappate_incontrate": sorted(etichette_nuove),
    }

    if not identificativi:
        passo.fallisci({
            "classe": NESSUN_RISULTATO,
            "messaggio": "Nessuna scheda da aprire: senza id dalla scoperta e "
                         "senza --notice-id il passo non ha nulla da provare",
        })
    if etichette_nuove:
        passo.note.append(
            "Etichette non previste incontrate nelle schede: conservate in "
            "extra_fields e non interpretate. Vanno mappate prima di poterle "
            "usare, non indovinate"
        )
    return passo, schede


# --------------------------------------------------------------------------
# Passo 5 - allegati
# --------------------------------------------------------------------------


def _riferimento(ref: AttachmentRef) -> dict[str, Any]:
    return {
        "notice_id": ref.notice_id,
        "url": ref.url,
        "nome_file": ref.filename,
        "estensione_dichiarata": ref.declared_extension,
        "pubblicato": _data(ref.published),
        "prelevabile": ref.fetch_permitted,
        "motivo_del_blocco": ref.fetch_block_reason,
    }


def passo_allegati(adapter: InpaSitemapAdapter, client: ClientConProve,
                   schede: list[NoticeSnapshot]) -> Passo:
    """Registra l'esito della valutazione del permesso, allegato per allegato.

    Il permesso viene chiesto a `fetch_attachment`, cioe' alla stessa
    funzione che userebbe il bot, invece di essere riletto da
    `ref.fetch_permitted`: cosi' la prova documenta il comportamento reale del
    codice e non una sua descrizione. Su un riferimento non consentito quella
    funzione non tocca la rete, e il conteggio delle richieste prima e dopo e'
    la verifica di questa affermazione: e' l'unico modo di *dimostrare* che
    nulla e' stato scaricato, invece di scriverlo (SPEC 3.1).
    """
    passo = Passo(5, "allegati: valutazione delle condizioni di accesso")

    riferimenti = [ref for scheda in schede for ref in scheda.attachments]
    prima_delle_richieste = len(client.records)

    esiti: list[dict[str, Any]] = []
    per_estensione: dict[str, int] = {}
    per_stato: dict[str, int] = {}
    host_coinvolti: dict[str, int] = {}

    for ref in riferimenti:
        dato = _riferimento(ref)
        esito = adapter.fetch_attachment(ref)
        dato["esito"] = esito.state.value
        dato["anomalie"] = _anomalie(esito.anomalies)
        if esito.size_bytes is not None:
            dato["byte_scaricati"] = esito.size_bytes
            dato["sha256"] = esito.sha256
        esiti.append(dato)

        chiave = ref.declared_extension or "(senza estensione nel nome)"
        per_estensione[chiave] = per_estensione.get(chiave, 0) + 1
        per_stato[esito.state.value] = per_stato.get(esito.state.value, 0) + 1
        host = (urllib.parse.urlsplit(ref.url).hostname or "(host assente)").lower()
        host_coinvolti[host] = host_coinvolti.get(host, 0) + 1

    richieste_durante = len(client.records) - prima_delle_richieste

    passo.dettaglio = {
        "schede_esaminate": len(schede),
        "allegati_trovati": len(riferimenti),
        "prelevabili": sum(1 for r in riferimenti if r.fetch_permitted),
        "per_estensione_dichiarata": dict(sorted(per_estensione.items())),
        "per_esito": dict(sorted(per_stato.items())),
        "per_host": dict(sorted(host_coinvolti.items())),
        "richieste_di_rete_generate": richieste_durante,
        "allegati": esiti,
    }

    vietati = [r for r in riferimenti if not r.fetch_permitted]
    if vietati and richieste_durante:
        passo.fallisci({
            "classe": "download_non_previsto",
            "messaggio": f"{richieste_durante} richieste di rete durante la "
                         "valutazione di allegati non prelevabili: la prova "
                         "doveva limitarsi a registrare il divieto",
        })

    if not schede:
        # Senza schede lette non c'e' nulla da valutare: dirlo e' diverso dal
        # dire che gli allegati non ci sono (SPEC 5.3).
        passo.note.append(
            "Nessuna scheda letta al passo 4: la valutazione degli allegati "
            "non ha avuto nulla su cui lavorare, il che non dice niente sugli "
            "allegati dei bandi"
        )
    elif not riferimenti:
        # Nessun allegato non e' un fallimento: e' un'osservazione. Fra i
        # campioni noti almeno una scheda non ne ha alcuno.
        passo.note.append(
            "Nessun allegato nelle schede aperte: e' un'osservazione, non un "
            "errore e non una prova che i bandi non ne abbiano"
        )
    else:
        passo.note.append(
            f"Nessun allegato scaricato: {len(vietati)} riferimenti su "
            f"{len(riferimenti)} risultano non prelevabili e la prova non "
            "tenta il download in nessun caso (vedi la docstring del file)"
        )
    return passo


# --------------------------------------------------------------------------
# Riga di comando e riepilogo
# --------------------------------------------------------------------------


def costruisci_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_inpa.py",
        description="Prova riproducibile del livello di acquisizione inPA (M0). "
                    "Non scarica allegati: l'host che li ospita li vieta.",
        epilog=f"Comando della prova citata nei rapporti: {COMANDO_DI_RIFERIMENTO}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", default="evidence", metavar="CARTELLA",
        help="cartella in cui scrivere le prove (default: evidence). Viene "
             "creata con permessi ristretti dove il sistema operativo lo "
             "consente; il contenuto va sanitizzato prima di condividerlo",
    )
    parser.add_argument(
        "--limit", type=int, default=5, metavar="N",
        help="quante schede aprire prendendole dalla sitemap (default: 5). "
             "Non si applica agli id passati con --notice-id",
    )
    parser.add_argument(
        "--notice-id", action="append", default=[], metavar="ID",
        dest="notice_ids",
        help="apre questa scheda invece di sceglierla dalla sitemap. "
             "Ripetibile. Accetta solo la forma osservata (32 cifre "
             "esadecimali): gli id non si enumerano",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, metavar="SECONDI",
        help="intervallo minimo fra richieste allo stesso host (default: 2.0, "
             "come SPEC 5.3). Un Crawl-delay dichiarato dalla fonte, se "
             "maggiore, prevale comunque su questo valore",
    )
    parser.add_argument(
        "--json", action="store_true", dest="come_json",
        help="stampa il riepilogo su stdout in JSON invece che in forma "
             "leggibile. Il banner e l'avanzamento restano su stderr",
    )
    return parser


def _avviso(testo: str) -> None:
    """Banner e avanzamento vanno su stderr.

    Cosi' `--json` lascia su stdout un documento interamente analizzabile,
    senza che la prova debba tacere proprio le informazioni sul ritmo che
    chi la esegue deve vedere.
    """
    print(testo, file=sys.stderr, flush=True)


def stampa_banner(config: HttpConfig, cartella: Path, permessi: str,
                  richieste_previste: int) -> None:
    durata = richieste_previste * config.min_interval
    _avviso(
        "\n"
        "===================================================================\n"
        " Prova M0 - acquisizione dati inPA\n"
        "-------------------------------------------------------------------\n"
        f" User-Agent  : {config.user_agent}\n"
        f" Ritmo       : 1 richiesta per volta, >= {config.min_interval:g} s fra "
        "richieste allo stesso host\n"
        f" Host ammessi: {', '.join(sorted(config.allowed_hosts))}\n"
        f" Inoltre     : un solo GET a {ATTACHMENT_HOST}/robots.txt, per "
        "registrarne il divieto\n"
        f" Richieste   : ~{richieste_previste} previste, almeno ~{durata:.0f} s "
        "di attese volute\n"
        " Allegati    : nessun download, in nessun caso\n"
        f" Prove in    : {cartella} ({permessi})\n"
        "-------------------------------------------------------------------\n"
        f" {AVVISO_SANITIZZAZIONE}\n"
        "==================================================================="
    )


def stampa_riepilogo(riepilogo: dict[str, Any]) -> None:
    righe = ["", "Esiti della prova:"]
    for passo in riepilogo["passi"]:
        segno = "OK     " if passo["esito"] == "ok" else "FALLITO"
        righe.append(f"  [{segno}] {passo['numero']}. {passo['nome']}")
        for problema in passo["problemi"]:
            righe.append(f"            {problema['classe']}: {problema['messaggio']}")
        for nota in passo["note"]:
            righe.append(f"            nota: {nota}")

    richieste = riepilogo["richieste"]
    righe.extend([
        "",
        f"Richieste inviate : {richieste['totali']} "
        f"({richieste['byte_totali']} byte, "
        f"{richieste['durata_totale_s']:.1f} s di risposta)",
        f"Prove scritte in  : {riepilogo['cartella_prove']['percorso']}",
        f"Esito complessivo : {riepilogo['esito'].upper()}",
        "",
    ])
    print("\n".join(righe))


def _passo_saltato(numero: int, nome: str, motivo: str) -> Passo:
    """Un passo non eseguito e' un fallimento dichiarato, non un successo.

    Registrarlo come riuscito perche' non ha sollevato eccezioni sarebbe il
    modo piu' rapido di far passare M0 senza averlo provato.
    """
    passo = Passo(numero, nome, ok=False)
    passo.fallisci({"classe": "passo_non_eseguito", "messaggio": motivo})
    return passo


def scrivi_richieste(percorso: Path, client: ClientConProve,
                     della_prova: list[RequestRecord]) -> None:
    """Una riga JSON per ogni richiesta inviata alla fonte.

    Le due origini restano distinte: `client` sono le richieste registrate da
    `HttpClient`, `prova` le letture di `robots.txt` che avvengono sotto quel
    registro. Unirle senza dirlo farebbe sembrare che il client abbia
    contabilizzato tutto.
    """
    righe: list[dict[str, Any]] = []
    for record in client.records:
        prova = client.prova_per(record.url)
        righe.append(_riga_richiesta(record, "client",
                                     prova.name if prova else None))
    for record in della_prova:
        righe.append(_riga_richiesta(record, "prova", None))

    righe.sort(key=lambda riga: riga["iniziata_il"] or "")
    for numero, riga in enumerate(righe, start=1):
        riga["seq"] = numero

    percorso.write_text(
        "".join(json.dumps(riga, ensure_ascii=False) + "\n" for riga in righe),
        encoding="utf-8", newline="",
    )


def componi_riepilogo(argomenti: argparse.Namespace, config: HttpConfig,
                      cartella: Path, permessi: str, client: ClientConProve,
                      della_prova: list[RequestRecord],
                      passi: list[Passo]) -> dict[str, Any]:
    tutte = list(client.records) + list(della_prova)
    falliti = [p.numero for p in passi if not p.ok]

    return {
        "prova": "M0 - livello di acquisizione dati inPA",
        "generato_il": datetime.now(timezone.utc).isoformat(),
        "comando_di_riferimento": COMANDO_DI_RIFERIMENTO,
        "comando_eseguito": " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
        "argomenti": {
            "out": str(cartella),
            "limit": argomenti.limit,
            "notice_id": list(argomenti.notice_ids),
            "interval": argomenti.interval,
        },
        "ambiente": {
            "python": sys.version.split()[0],
            "piattaforma": sys.platform,
            "adattatore": InpaSitemapAdapter.name,
        },
        "cortesia": {
            "user_agent": config.user_agent,
            "intervallo_minimo_s": config.min_interval,
            "host_ammessi": sorted(config.allowed_hosts),
            "host_letti_solo_per_robots": [ATTACHMENT_HOST],
            "allegati_scaricati": 0,
        },
        "cartella_prove": {
            "percorso": str(cartella),
            "permessi": permessi,
            "avviso": AVVISO_SANITIZZAZIONE,
            "corpi_salvati": client.salvati,
        },
        "passi": [p.come_json() for p in passi],
        "richieste": {
            "totali": len(tutte),
            "dal_client": len(client.records),
            "dalla_prova": len(della_prova),
            "byte_totali": sum(r.bytes_read for r in tutte),
            "durata_totale_s": round(sum(r.elapsed_s for r in tutte), 3),
            "registro": "requests.jsonl",
        },
        "passi_falliti": falliti,
        "esito": "ok" if not falliti else "fallito",
    }


def main(argv: list[str] | None = None) -> int:
    parser = costruisci_parser()
    argomenti = parser.parse_args(argv)

    if argomenti.limit < 0:
        parser.error("--limit non puo' essere negativo")
    if argomenti.interval < 0:
        parser.error("--interval non puo' essere negativo")

    cartella = Path(argomenti.out).expanduser()
    permessi = prepara_cartella(cartella)

    config = HttpConfig(min_interval=argomenti.interval)
    client = ClientConProve(cartella, config)
    adapter = InpaSitemapAdapter(client, page_size=PAGINA_PROVA)

    schede_previste = len(argomenti.notice_ids) or argomenti.limit
    stampa_banner(config, cartella, permessi, 2 + 2 + schede_previste)
    if argomenti.interval < 2.0:
        _avviso(
            f" ATTENZIONE: intervallo di {argomenti.interval:g} s, sotto il "
            "default prudente di 2 s proposto da SPEC 5.3. Non e' un limite "
            "ufficiale della fonte, ma abbassarlo aumenta il carico su un "
            "sito pubblico: usarlo solo con una ragione.\n"
        )

    passi: list[Passo] = []
    richieste_della_prova: list[RequestRecord] = []

    try:
        _avviso("[1/5] robots.txt dei due host...")
        passo, richieste = passo_condizioni(client, cartella)
        passi.append(passo)
        richieste_della_prova.extend(richieste)

        _avviso("[2/5] indice delle sitemap e sitemap dei bandi...")
        passo_due, pagina = passo_sitemap(adapter, client, max(argomenti.limit, 1))
        passi.append(passo_due)

        _avviso("[3/5] paginazione di search()...")
        if pagina is None:
            passi.append(_passo_saltato(
                3, "paginazione di search() (M0-02)",
                "la scoperta non ha prodotto un catalogo da paginare",
            ))
        else:
            passi.append(passo_paginazione(adapter, client))

        identificativi, scartati, origine = scegli_schede(
            pagina, argomenti.notice_ids, argomenti.limit
        )
        _avviso(f"[4/5] {len(identificativi)} schede ({origine})...")
        passo_quattro, schede = passo_schede(adapter, identificativi, origine,
                                             scartati)
        passi.append(passo_quattro)

        _avviso("[5/5] condizioni di accesso agli allegati...")
        passi.append(passo_allegati(adapter, client, schede))

    except KeyboardInterrupt:
        passi.append(_passo_saltato(
            0, "prova interrotta",
            "interruzione richiesta dall'operatore: i passi non eseguiti non "
            "sono ne' riusciti ne' falliti",
        ))
    except Exception as exc:  # noqa: BLE001 - vedi sotto
        # Anche un guasto imprevisto deve lasciare un summary.json: una prova
        # che non scrive nulla quando va male e' proprio quella che non si
        # riesce a diagnosticare (M0-09).
        passo_errore = Passo(0, "errore imprevisto della prova", ok=False)
        passo_errore.fallisci(classifica_errore(exc))
        passi.append(passo_errore)

    riepilogo = componi_riepilogo(argomenti, config, cartella, permessi,
                                  client, richieste_della_prova, passi)
    scrivi_json(cartella / "summary.json", riepilogo)
    scrivi_richieste(cartella / "requests.jsonl", client, richieste_della_prova)

    if argomenti.come_json:
        print(json.dumps(riepilogo, ensure_ascii=False, indent=2))
    else:
        stampa_riepilogo(riepilogo)

    return 0 if riepilogo["esito"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
