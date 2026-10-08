"""Regressioni della prospettiva «robustezza» (revisione del 20/09/2026).

Sono gli undici rilievi la cui verifica non era stata eseguita quando la
revisione si e' interrotta. Verificati e corretti in un secondo momento: uno
era gia' risolto da una correzione precedente, gli altri dieci sono qui.

Il tema e' quello che attraversa M0: la fonte, la rete e il nostro stesso
codice possono guastarsi in modi che *somigliano* a un contenuto valido. Un
robots.txt che risponde 503, una sitemap dimezzata, una scheda il cui tema e'
cambiato, un corpo decodificato male: nessuno di questi deve poter passare per
un dato.
"""

from __future__ import annotations

import io
import pathlib
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from inpa_bot.source import sitemap as SM
from inpa_bot.source.adapter import InpaSitemapAdapter
from inpa_bot.source.detail import parse_detail
from inpa_bot.source.errors import (
    ParseError,
    ResponseTooLargeError,
    TransportError,
    UnsafeUrlError,
)
from inpa_bot.source.http import HttpClient, HttpConfig, Response
from inpa_bot.source.models import SearchFilters
from inpa_bot.source.robots import FAILURE_TTL, RobotsPolicy, _glob_matches, _normalised_pattern
from inpa_bot.source.snapshot import SnapshotStore

QUANDO = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SCHEDA = FIXTURES / "detail" / "d3d8d8d7a82e40afbd75a26841bc9070.html"


# --------------------------------------------------------------------------
# URL malformati: rifiuto, non eccezione fuori tassonomia
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.inpa.gov.it:99999/x",   # porta fuori intervallo
    "https://www.inpa.gov.it:abc/x",     # porta non numerica
    "https://[incompleto/x",             # IPv6 mai chiuso
])
def test_un_url_malformato_e_un_rifiuto_non_un_crash(url):
    """`validate_url` riceve anche i link scritti dagli utenti (SPEC 9).

    `urlsplit` e `.port` sollevano `ValueError` su questi indirizzi. Uscendo
    dalla tassonomia di `errors.py`, quell'errore avrebbe fatto cadere il bot
    su un messaggio invece di rispondere "questo link non va bene".
    """
    client = HttpClient(HttpConfig(allow_private_addresses=True, min_interval=0.0))

    with pytest.raises(UnsafeUrlError):
        client.validate_url(url)


# --------------------------------------------------------------------------
# Retry-After: rispettarlo significa anche smettere
# --------------------------------------------------------------------------


class _Risposta:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers = status, headers
        self._buffer = io.BytesIO(body)

    def read(self, quanti: int = -1) -> bytes:
        return self._buffer.read(quanti)

    def __enter__(self) -> _Risposta:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _client(rotte: dict[str, object], **configurazione):
    attese: list[float] = []
    client = HttpClient(
        HttpConfig(allow_private_addresses=True, min_interval=0.0, **configurazione),
        sleep=attese.append,
    )

    class Opener:
        def __init__(self) -> None:
            self.chiamate: list[str] = []

        def open(self, richiesta, timeout=None):  # noqa: ANN001 - firma di urllib
            url = richiesta.full_url
            self.chiamate.append(url)
            esito = rotte.get(url)
            if esito is None:
                raise urllib.error.URLError(f"nessuna rotta per {url}")
            if isinstance(esito, Exception):
                raise esito
            return _Risposta(*esito)

    client._opener = Opener()  # noqa: SLF001 - sostituzione del trasporto
    client.robots = type("P", (), {  # noqa: SLF001
        "check": lambda self, url: type("D", (), {"allowed": True, "rule": "ok"})(),
        "crawl_delay": lambda self, url: None,
    })()
    return client, attese


URL = "https://www.inpa.gov.it/x"


# --------------------------------------------------------------------------
# robots.txt: un guasto momentaneo non e' un fermo di un giorno
# --------------------------------------------------------------------------


def test_un_guasto_su_robots_non_blocca_per_ventiquattro_ore():
    """Il rifiuto prudente va tenuto poco, non quanto un permesso.

    Con il TTL pieno, un singolo errore di rete su robots.txt bloccava ogni
    richiesta a quell'host per 24 ore senza mai riprovare: un guasto di pochi
    secondi trasformato in un fermo di un giorno.
    """
    adesso = [QUANDO]
    tentativi: list[str] = []
    guasto = {"attivo": True}

    def fetcher(url: str) -> tuple[int, str]:
        tentativi.append(url)
        if guasto["attivo"]:
            raise TransportError("rete non disponibile")
        return 200, "User-agent: *\nAllow: /\n"

    policy = RobotsPolicy(fetcher=fetcher, user_agent="Test/1.0",
                          clock=lambda: adesso[0])

    assert policy.check("https://esempio.test/x").allowed is False
    adesso[0] += timedelta(minutes=1)
    assert policy.check("https://esempio.test/x").allowed is False
    assert len(tentativi) == 1, "entro il TTL breve non si ritenta"

    adesso[0] += FAILURE_TTL
    guasto["attivo"] = False
    assert policy.check("https://esempio.test/x").allowed is True
    assert len(tentativi) == 2


# --------------------------------------------------------------------------
# Pattern robots: costo lineare, non esponenziale
# --------------------------------------------------------------------------


@pytest.mark.parametrize("jolly", [12, 24, 48])
def test_molti_jolly_non_mandano_in_stallo_la_decisione(jolly):
    """Il confronto era una regex: 12 jolly costavano 18 secondi.

    `evaluate` viene invocata per ogni URL, quindi un solo pattern del genere
    in un robots.txt -- documento della fonte, non nostro -- avrebbe fermato
    l'intera acquisizione. Il confronto e' ora a due indici, senza
    backtracking.
    """
    pattern = _normalised_pattern("/" + "a*" * jolly + "X$")
    percorso = "/" + "a" * 40

    import time
    inizio = time.monotonic()
    esito = _glob_matches(pattern, percorso)
    durata = time.monotonic() - inizio

    assert esito is False
    assert durata < 0.5, f"{durata:.2f} s per {jolly} jolly: sembra backtracking"


@pytest.mark.parametrize(("pattern", "percorso", "atteso"), [
    ("/wp-admin/", "/wp-admin/options.php", True),
    ("/wp-admin/", "/bandi-e-avvisi/", False),
    ("/*.pdf$", "/documento.pdf", True),
    ("/*.pdf$", "/documento.pdf?x=1", False),
    ("/", "/qualsiasi", True),
    ("/a*b", "/a123b456", True),
    ("/a*b$", "/a123b456", False),
    ("/x**y", "/x_y", True),
])
def test_il_confronto_con_i_jolly_resta_quello_di_prima(pattern, percorso, atteso):
    """La sostituzione non doveva cambiare un solo verdetto."""
    assert _glob_matches(_normalised_pattern(pattern), percorso) is atteso


# --------------------------------------------------------------------------
# Sitemap: voci mutile, crolli improvvisi
# --------------------------------------------------------------------------


def test_le_voci_senza_loc_vengono_contate_non_solo_saltate():
    """Tacere gli scarti falsa il totale.

    Il conteggio sarebbe stato calcolato su cio' che e' sopravvissuto: una
    sitemap con meta' delle voci mutile sarebbe sembrata una sitemap piccola
    e sana.
    """
    xml = ('<?xml version="1.0"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><lastmod>2026-09-18T00:00:00Z</lastmod></url>'
           '<url><loc>https://inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/'
           f'?concorso_id={"a" * 32}</loc></url>'
           '</urlset>')

    voci, saltate = SM.parse_urlset_detailed(xml)

    assert len(voci) == 1
    assert saltate == 1


_INDICE = ('<?xml version="1.0"?>'
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://www.inpa.gov.it/sitemap-bandi.xml</loc></sitemap>'
           '</sitemapindex>')


def _urlset(quante: int) -> str:
    righe = ''.join(
        '<url><loc>https://inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/'
        f'?concorso_id={i:032x}</loc></url>' for i in range(quante))
    return ('<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + righe + '</urlset>')


class _RispostaXml:
    content_type = "application/xml"
    status = 200

    def __init__(self, url: str, corpo: str) -> None:
        self.url = self.final_url = url
        self._corpo = corpo

    def text(self, fallback: str = "utf-8") -> str:
        return self._corpo

    def decoded(self, fallback: str = "utf-8") -> tuple[str, bool]:
        return self._corpo, False


def _adattatore_variabile(stato: dict[str, int]) -> InpaSitemapAdapter:
    class Client:
        records: list[object] = []

        def get(self, url, accept=None, **kwargs):
            corpo = _INDICE if url.endswith("sitemap.xml") else _urlset(stato["quante"])
            return _RispostaXml(url, corpo)

    return InpaSitemapAdapter(client=Client(), page_size=10)


def test_una_sitemap_che_crolla_non_e_un_perimetro_valido():
    """Da 1680 a 3 voci non e' la scadenza di 1677 bandi, e' un guasto.

    Senza anomalia bloccante chi sincronizza avrebbe preso quella lettura per
    buona e concluso che migliaia di bandi erano spariti, mentre SPEC 4.2
    avverte che un record assente da un elenco non e' automaticamente
    revocato.
    """
    stato = {"quante": 1680}
    adattatore = _adattatore_variabile(stato)
    assert adattatore.search(SearchFilters()).complete is True

    stato["quante"] = 3
    adattatore.refresh()
    pagina = adattatore.search(SearchFilters())

    assert pagina.complete is False
    assert any(a.code == "sitemap_crollo_improvviso" for a in pagina.anomalies)


def test_una_variazione_fisiologica_non_allarma():
    """Un calo modesto e' normale: i bandi scadono."""
    stato = {"quante": 1680}
    adattatore = _adattatore_variabile(stato)
    adattatore.search(SearchFilters())

    stato["quante"] = 1500
    adattatore.refresh()

    assert adattatore.search(SearchFilters()).complete is True


# --------------------------------------------------------------------------
# Scheda: un cambio di tema non e' un bando svuotato
# --------------------------------------------------------------------------


def test_se_cambia_la_classe_dei_valori_la_scheda_non_e_povera_ma_illeggibile():
    """Rinominare i contenitori di valore svuotava ogni bando.

    Il parser trovava le etichette, nessun valore, e restituiva uno snapshot
    con titolo giusto e tutto il resto a `None`, senza anomalie bloccanti. Il
    confronto con la versione precedente avrebbe annunciato scadenza sparita,
    posti azzerati e allegati rimossi: un cambio di tema del sito trasformato
    in notifiche (M0-08).

    Il segnale e' il rapporto: sulle schede reali le etichette valorizzate
    sono tutte, qui sarebbe una su dieci.
    """
    html = SCHEDA.read_text(encoding="utf-8")
    rinominato = (html.replace("concorso-content", "valore-bando")
                      .replace("concorso-status", "stato-bando"))

    snapshot = parse_detail(rinominato, notice_id="d" * 32, source_url="u",
                            observed_at=QUANDO)

    assert snapshot.has_blocking_anomaly is True
    assert any(a.code == "valori_non_estratti" for a in snapshot.anomalies)


def test_un_bando_con_pochi_campi_resta_valido():
    """La soglia non deve punire una scheda genuinamente scarna."""
    html = ('<html><body><h1>Titolo</h1>'
            '<span class="concorso-label">Numero di posti: </span>'
            '<span class="concorso-content">1</span></body></html>')

    snapshot = parse_detail(html, notice_id="a" * 32, source_url="u",
                            observed_at=QUANDO)

    assert snapshot.has_blocking_anomaly is False
    assert snapshot.total_positions == 1


@pytest.mark.parametrize("percorso", sorted(
    (FIXTURES / "detail").glob("*.html")), ids=lambda p: p.stem[:10])
def test_nessuna_scheda_reale_viene_dichiarata_illeggibile(percorso):
    """Rete di sicurezza: la soglia non deve scattare sui campioni veri."""
    snapshot = parse_detail(percorso.read_text(encoding="utf-8"),
                            notice_id=percorso.stem, source_url="u",
                            observed_at=QUANDO)

    assert snapshot.has_blocking_anomaly is False


# --------------------------------------------------------------------------
# Archivio: una riga illeggibile non e' un bando nuovo
# --------------------------------------------------------------------------


def test_una_versione_archiviata_illeggibile_e_un_errore_tipizzato():
    """`json` non deve risalire fino a interrompere l'acquisizione.

    E restituire `None` sarebbe peggio del guasto: `record()` lo leggerebbe
    come "mai visto prima", dichiarerebbe il bando nuovo e rigenererebbe una
    notifica gia' inviata.
    """
    store = SnapshotStore()
    identificativo = "b" * 32
    with store._write() as conn:  # noqa: SLF001 - si prepara uno stato corrotto
        conn.execute(
            "INSERT INTO notices (notice_id, first_seen_at, last_seen_at, source_url)"
            " VALUES (?,?,?,?)",
            (identificativo, "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00", "u"))
        conn.execute(
            "INSERT INTO notice_versions (notice_id, version_no, observed_at,"
            " metadata_hash, raw_document_hash, payload) VALUES (?,?,?,?,?,?)",
            (identificativo, 1, "2026-01-01T00:00:00+00:00", "m", "d", "{non-json"))

    with pytest.raises(ParseError):
        store.latest(identificativo)

    store.close()


# --------------------------------------------------------------------------
# Tracce: cio' che non compare nel registro non compare nei rapporti
# --------------------------------------------------------------------------


def test_un_url_rifiutato_lascia_una_traccia():
    """`records` alimenta i rapporti M0-09: un rifiuto e' un esito."""
    client, _ = _client({})

    with pytest.raises(UnsafeUrlError):
        client.get("https://evil.example.com/x")

    assert len(client.records) == 1
    assert "unsafe_url" in client.records[0].error


def test_una_risposta_troppo_grande_lascia_una_traccia():
    client, _ = _client({URL: (200, {"Content-Type": "text/html"}, b"x" * 50_000)},
                        max_bytes=1000, max_retries=0)

    with pytest.raises(ResponseTooLargeError):
        client.get(URL)

    assert len(client.records) == 1
    assert "troppo grande" in client.records[0].error


def test_un_corpo_troncato_viene_registrato_e_ritentato():
    """E' un guasto di trasporto: si ritenta come un timeout."""
    client, _ = _client(
        {URL: (200, {"Content-Type": "text/html", "content-length": "5000"}, b"x" * 400)},
        max_retries=2)

    with pytest.raises(TransportError):
        client.get(URL)

    assert [r.attempt for r in client.records] == [1, 2, 3]


# --------------------------------------------------------------------------
# Decodifica: una sostituzione silenziosa non e' testo valido
# --------------------------------------------------------------------------


def test_una_decodifica_con_sostituzioni_viene_dichiarata():
    """Il testo si legge, ma non e' quello pubblicato.

    Senza indicatore, una differenza introdotta dalla decodifica sarebbe
    indistinguibile da una modifica del bando.
    """
    risposta = Response(
        url="u", final_url="u", status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body="Università".encode("latin-1"),
        elapsed_s=0.0, fetched_at=QUANDO)

    testo, sostituito = risposta.decoded()

    assert sostituito is True
    assert "�" in testo


def test_un_corpo_valido_non_viene_segnalato():
    risposta = Response(
        url="u", final_url="u", status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body="Università".encode("utf-8"),
        elapsed_s=0.0, fetched_at=QUANDO)

    testo, sostituito = risposta.decoded()

    assert sostituito is False
    assert testo == "Università"
