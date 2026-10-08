"""Regressioni emerse dalla revisione critica del 19-20/09/2026.

Ognuno di questi test corrisponde a un difetto reale, riprodotto prima di
essere corretto. Il commento dice quale e perche' contava: un test di
regressione senza la sua storia e' il primo a essere cancellato durante un
refactoring, ed e' esattamente quello che non deve succedere a questi.

Il filo comune e' uno solo, ed e' il cuore di M0: nessun guasto -- della
fonte, della rete o del nostro codice -- deve poter essere scambiato per un
contenuto. Un robots.txt che risponde 503 non e' un permesso, un corpo
troncato non e' un bando svuotato, una sitemap illeggibile non e' "nessun
concorso".
"""

from __future__ import annotations

import io
import pathlib
import threading
from datetime import datetime, timezone

import pytest

from inpa_bot.source import robots as R
from inpa_bot.source.adapter import InpaSitemapAdapter
from inpa_bot.source.detail import parse_detail
from inpa_bot.source.errors import TransportError
from inpa_bot.source.http import HttpClient, HttpConfig
from inpa_bot.source.models import SearchFilters
from inpa_bot.source.robots import RobotsDecision
from inpa_bot.source.snapshot import SnapshotStore, diff_snapshots

UA = "InpaTelegramBot-Probe/0.1 (+contatto: configurare prima dell'uso)"
QUANDO = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------
# robots.txt: una non risposta non e' un permesso
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stato", [401, 403, 429, 500, 502, 503, 301, 302])
def test_un_robots_che_non_risponde_200_non_concede_nulla(stato):
    """Uno stato diverso da 200/404 non autorizza (era il difetto piu' grave).

    Il codice trattava come "nessuna direttiva applicabile" qualunque risposta
    priva di gruppi, quindi un `503` momentaneo di `portale.inpa.gov.it`
    rendeva consentito il download degli allegati -- cioe' faceva cadere
    proprio la condizione su cui si regge tutto il perimetro del progetto.
    Qui si e' deliberatamente piu' prudenti di RFC 9309, che sui 4xx
    consentirebbe di procedere.
    """
    robots = R.parse_robots("", "portale.inpa.gov.it", status=stato)

    decisione = R.evaluate(robots, "https://portale.inpa.gov.it/api/media/x", UA)

    assert decisione.allowed is False
    assert decisione.origin == "unreachable"
    assert str(stato) in decisione.rule


@pytest.mark.parametrize("stato", [404, 410])
def test_un_robots_che_non_esiste_non_vieta_nulla(stato):
    """L'assenza vera del file resta assenza di divieti."""
    robots = R.parse_robots("", "esempio.test", status=stato)

    assert R.evaluate(robots, "https://esempio.test/x", UA).allowed is True


def test_il_gruppo_si_sceglie_sul_product_token_non_per_sottostringa():
    """Il nostro token contiene 'TelegramBot' e assorbiva le regole altrui.

    La selezione del gruppo avveniva per sottostringa, quindi un
    `User-agent: TelegramBot` scritto per un altro programma diventava il
    nostro gruppo. Con un `Allow` altrui ci saremmo attribuiti un permesso
    mai concesso.
    """
    testo = "User-agent: TelegramBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    robots = R.parse_robots(testo, "esempio.test")

    decisione = R.evaluate(robots, "https://esempio.test/pagina", UA)

    assert decisione.allowed is True
    assert "[*]" in decisione.rule


def test_il_gruppo_scritto_per_noi_viene_invece_applicato():
    """L'uguaglianza sul token deve comunque riconoscere il nostro nome."""
    testo = ("User-agent: InpaTelegramBot-Probe\nDisallow: /privato/\n\n"
             "User-agent: *\nAllow: /\n")
    robots = R.parse_robots(testo, "esempio.test")

    decisione = R.evaluate(robots, "https://esempio.test/privato/x", UA)

    assert decisione.allowed is False
    assert "inpatelegrambot-probe" in decisione.rule


def test_il_robots_reale_dei_due_host_non_cambia_verdetto():
    """Rete di sicurezza: le correzioni non devono spostare i casi veri."""
    www = R.parse_robots((FIXTURES / "robots-www.txt").read_text(encoding="utf-8"),
                         "www.inpa.gov.it")
    portale = R.parse_robots((FIXTURES / "robots-portale.txt").read_text(encoding="utf-8"),
                             "portale.inpa.gov.it")

    assert R.evaluate(www, "https://www.inpa.gov.it/bandi-e-avvisi/x", UA).allowed
    assert R.evaluate(www, "https://www.inpa.gov.it/sitemap-bandi.xml", UA).allowed
    assert not R.evaluate(www, "https://www.inpa.gov.it/wp-json/x", UA).allowed
    assert R.evaluate(www, "https://www.inpa.gov.it/wp-admin/admin-ajax.php", UA).allowed
    assert not R.evaluate(portale, "https://portale.inpa.gov.it/api/media/x", UA).allowed


# --------------------------------------------------------------------------
# Corpo troncato: errore di trasporto, non contenuto
# --------------------------------------------------------------------------


class _Flusso:
    def __init__(self, corpo: bytes, dichiarati: int | str | None) -> None:
        self._buffer = io.BytesIO(corpo)
        self.headers = {} if dichiarati is None else {"content-length": str(dichiarati)}

    def read(self, quanti: int = -1) -> bytes:
        return self._buffer.read(quanti)


def test_un_corpo_piu_corto_del_dichiarato_e_un_errore_di_trasporto():
    """`HTTPResponse.read` non segnala i corpi tronchi: lo facciamo noi.

    Senza questo controllo una scheda interrotta a meta' arrivava al parser
    come documento valido. Siccome le etichette stanno in cima al corpo, ne
    usciva un bando con titolo giusto e tutto il resto vuoto: scadenza
    sparita, posti azzerati, allegati "rimossi". Il diff lo presentava come
    aggiornamento significativo, cioe' un guasto di rete travestito da
    notifica (M0-08).
    """
    client = HttpClient(HttpConfig(min_interval=0.0))

    with pytest.raises(TransportError, match="corpo incompleto"):
        client._read_limited(_Flusso(b"x" * 400, 5000), "https://www.inpa.gov.it/x")


@pytest.mark.parametrize("dichiarati", [None, "abc", ""])
def test_senza_content_length_valido_non_si_inventa_un_errore(dichiarati):
    """Una risposta chunked non dichiara la lunghezza: non e' un guasto."""
    client = HttpClient(HttpConfig(min_interval=0.0))

    corpo = client._read_limited(_Flusso(b"x" * 400, dichiarati), "https://www.inpa.gov.it/x")

    assert len(corpo) == 400


# --------------------------------------------------------------------------
# Entita' HTML: decodificate una volta sola
# --------------------------------------------------------------------------


def _scheda_con_ente(frammento: str) -> str:
    return ('<html><body><h1>Titolo</h1>'
            '<span class="concorso-label">Ente di riferimento: </span>'
            f'<span class="concorso-content">{frammento}</span></body></html>')


def test_le_entita_html_non_vengono_decodificate_due_volte():
    """`HTMLParser` decodifica gia': un secondo giro corrompe il testo.

    Nel sorgente `&amp;lt;b&amp;gt;` rappresenta il testo letterale
    `&lt;b&gt;`. Con due decodifiche diventava `<b>`, cioe' markup vero: oltre
    a essere un dato sbagliato, quel titolo inviato a Telegram in modalita'
    HTML verrebbe interpretato come formattazione.
    """
    snapshot = parse_detail(_scheda_con_ente("Tizio &amp;amp; Caio &amp;lt;b&amp;gt;"),
                            notice_id="a" * 32, source_url="u", observed_at=QUANDO)

    assert snapshot.enti[0] == "Tizio &amp; Caio &lt;b&gt;"


def test_le_entita_normali_restano_decodificate():
    """La correzione non deve lasciare entita' grezze nel testo del bando."""
    snapshot = parse_detail(_scheda_con_ente("Universit&agrave; &ndash; sede"),
                            notice_id="a" * 32, source_url="u", observed_at=QUANDO)

    assert snapshot.enti[0] == "Università – sede"


# --------------------------------------------------------------------------
# Sitemap illeggibile: non e' "nessun bando"
# --------------------------------------------------------------------------


_INDICE = ('<?xml version="1.0"?>'
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://www.inpa.gov.it/sitemap-bandi.xml</loc></sitemap>'
           '</sitemapindex>')


def _urlset(buone: int, rotte: int) -> str:
    righe = [
        '<url><loc>https://inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/'
        f'?concorso_id={i:032x}</loc></url>' for i in range(buone)
    ]
    righe += [f'<url><loc>https://inpa.gov.it/altro/{i}/</loc></url>' for i in range(rotte)]
    return ('<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(righe) + "</urlset>")


class _RispostaFinta:
    content_type = "application/xml"
    status = 200

    def __init__(self, url: str, corpo: str) -> None:
        self.url = self.final_url = url
        self._corpo = corpo

    def text(self, fallback: str = "utf-8") -> str:
        return self._corpo


def _adattatore(buone: int, rotte: int) -> InpaSitemapAdapter:
    corpo = _urlset(buone, rotte)

    class ClientFinto:
        records: list[object] = []

        def get(self, url, accept=None, **kwargs):
            return _RispostaFinta(url, _INDICE if url.endswith("sitemap.xml") else corpo)

    return InpaSitemapAdapter(client=ClientFinto())


def test_una_sitemap_di_cui_nessuna_voce_e_leggibile_non_e_zero_bandi():
    """Trenta voci illeggibili davano "0 record, scansione completa".

    Le conversioni fallite erano segnalate una per una come WARNING: prese
    singolarmente innocue, tutte insieme producevano il falso "nessun
    concorso" che M0-08 vieta. Il problema si vede solo guardando l'insieme,
    quindi l'anomalia bloccante va emessa li'.
    """
    pagina = _adattatore(buone=0, rotte=30).search(SearchFilters())

    assert pagina.records == ()
    assert pagina.complete is False
    assert any(a.blocking for a in pagina.anomalies)
    assert any(a.code == "sitemap_nessun_id_utilizzabile" for a in pagina.anomalies)


def test_una_perdita_massiva_si_segnala_senza_buttare_il_leggibile():
    """Meta' perimetro perso e' un sintomo, ma cio' che si e' letto vale."""
    pagina = _adattatore(buone=5, rotte=30).search(SearchFilters())

    assert len(pagina.records) == 5
    assert pagina.complete is True
    assert any(a.code == "sitemap_perdita_massiva" for a in pagina.anomalies)


def test_una_sitemap_sana_non_produce_allarmi():
    pagina = _adattatore(buone=30, rotte=0).search(SearchFilters())

    assert len(pagina.records) == 30
    assert pagina.complete is True
    assert pagina.anomalies == ()


# --------------------------------------------------------------------------
# Riordino: l'impronta e il confronto devono dire la stessa cosa
# --------------------------------------------------------------------------


def _scheda_multiregionale(sostituzione: tuple[str, str] | None = None):
    percorso = FIXTURES / "detail" / "01c11fcf96d04a7092d039e5f1a66224.html"
    html = percorso.read_text(encoding="utf-8")
    if sostituzione is not None:
        html = html.replace(*sostituzione, 1)
    return parse_detail(html, notice_id="01c11fcf96d04a7092d039e5f1a66224",
                        source_url="u", observed_at=QUANDO)


def test_riordinare_i_territori_non_e_un_aggiornamento():
    """`metadata_hash` ordinava, il diff no: si contraddicevano.

    Su un bando con quattordici regioni bastava che il backend le
    restituisse in ordine diverso perche' l'impronta dicesse "identico" e il
    confronto "aggiornato". All'utente sarebbe arrivata una notifica per
    nulla.
    """
    prima = _scheda_multiregionale()
    dopo = _scheda_multiregionale(("Lazio, Campania,", "Campania, Lazio,"))

    assert prima.metadata_hash == dopo.metadata_hash
    differenza = diff_snapshots(prima, dopo)
    assert differenza.metadata_changed is False
    assert differenza.significant is False


def test_togliere_una_regione_resta_un_aggiornamento():
    """L'insensibilita' all'ordine non deve nascondere un cambiamento vero."""
    prima = _scheda_multiregionale()
    dopo = _scheda_multiregionale(("Lazio, Campania,", "Campania,"))

    differenza = diff_snapshots(prima, dopo)

    assert differenza.metadata_changed is True
    assert any(c.field == "territories" for c in differenza.changes)


# --------------------------------------------------------------------------
# Allegati: la porta fa parte dell'origine
# --------------------------------------------------------------------------


class _RobotsPermissivo:
    def __init__(self) -> None:
        self.consultati: list[str] = []

    def check(self, url: str) -> RobotsDecision:
        self.consultati.append(url)
        return RobotsDecision(True, "nessuna regola", url, None, "rules")

    def crawl_delay(self, url: str) -> float | None:
        return None


def _adattatore_con_robots_permissivo():
    class ClientFinto:
        records: list[object] = []
        config = HttpConfig()
        robots = _RobotsPermissivo()

        def get(self, *args, **kwargs):
            raise AssertionError("nessuna richiesta attesa in questo test")

    client = ClientFinto()
    return InpaSitemapAdapter(client=client), client


@pytest.mark.parametrize("url", [
    "https://www.inpa.gov.it:8443/api/media/x",
    "https://www.inpa.gov.it:80/api/media/x",
])
def test_una_porta_non_standard_non_e_un_allegato_prelevabile(url):
    """L'host giusto rendeva credibile un href su porta arbitraria.

    Il download sarebbe comunque fallito in `validate_url`, ma l'allegato
    restava archiviato con `fetch_permitted=True`: un permesso mai concesso
    scritto nei dati. La porta fa parte dell'origine, e un'origine diversa e'
    un'altra fonte.
    """
    adattatore, _ = _adattatore_con_robots_permissivo()

    consentito, motivo = adattatore._attachment_permission(url)

    assert consentito is False
    assert "porta non ammessa" in motivo


def test_una_porta_non_numerica_non_fa_esplodere_il_parser():
    """Un href della scheda non e' contenuto fidato: `.port` puo' sollevare."""
    adattatore, _ = _adattatore_con_robots_permissivo()

    consentito, motivo = adattatore._attachment_permission(
        "https://www.inpa.gov.it:abc/api/media/x")

    assert consentito is False
    assert "non richiedibile" in motivo


def test_la_porta_predefinita_resta_valida():
    adattatore, _ = _adattatore_con_robots_permissivo()

    assert adattatore._attachment_permission(
        "https://www.inpa.gov.it:443/api/media/x") == (True, None)


# --------------------------------------------------------------------------
# Archivio: una chiusura concorrente non e' un crash
# --------------------------------------------------------------------------


def test_una_chiusura_concorrente_non_rompe_le_letture():
    """Le letture non prendevano il lock che `close()` invece prende.

    Con `check_same_thread=False` la connessione attraversa i thread per
    scelta, quindi una `close()` che cada fra il recupero della connessione e
    l'esecuzione della query faceva fallire la lettura con un errore del
    driver: un guasto del processo al posto di un "archivio chiuso".
    """
    store = SnapshotStore()
    inattesi: list[str] = []

    def legge() -> None:
        for _ in range(400):
            try:
                store.latest("a" * 32)
            except RuntimeError:
                pass  # atteso: lo store e' stato chiuso
            except Exception as exc:  # noqa: BLE001 - e' cio' che il test cerca
                inattesi.append(type(exc).__name__)

    lettore = threading.Thread(target=legge)
    lettore.start()
    store.close()
    lettore.join()

    assert inattesi == []
