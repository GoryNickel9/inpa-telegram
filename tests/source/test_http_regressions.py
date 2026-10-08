"""Regressioni trovate provando il client contro il sito reale.

Ogni test qui nasce da un difetto realmente osservato, non da un'ipotesi.
Il commento dice quale, perche' un test di regressione senza la storia che lo
ha generato e' il primo che viene cancellato al primo refactoring.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from inpa_bot.source import robots as R
from inpa_bot.source.http import HttpClient, HttpConfig

ROBOTS_CON_CRAWL_DELAY = "User-agent: *\nCrawl-delay: 5\nDisallow: /privato/\n"


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._buffer = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CountingOpener:
    """Opener finto che conta quante volte viene chiesto ogni URL."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if url not in self.routes:
            raise urllib.error.URLError(f"nessuna rotta per {url}")
        status, headers, body = self.routes[url]
        return _FakeResponse(status, headers, body)

    def count(self, url: str) -> int:
        return sum(1 for call in self.calls if call == url)


def _client(routes, **config):
    client = HttpClient(HttpConfig(min_interval=0.0, max_retries=0, **config),
                        sleep=lambda _: None)
    client._opener = _CountingOpener(routes)
    return client


def test_recupero_del_robots_non_richiama_se_stesso():
    """Scaricare robots.txt non deve rileggere robots.txt per il Crawl-delay.

    Difetto osservato il 19/09/2026 contro il sito reale: `_respect_interval`
    consultava `crawl_delay()`, che per rispondere scaricava robots.txt, che
    chiamava di nuovo `_respect_interval`. La cache si popola solo a fine
    scaricamento, quindi la ricorsione non si fermava e il client restava
    appeso emettendo richieste a raffica verso `/robots.txt`.

    Non e' solo un blocco: senza questa protezione un errore di programmazione
    si traduce in un piccolo attacco al sito che si voleva interrogare con
    garbo.
    """
    routes = {
        "https://www.inpa.gov.it/robots.txt":
            (200, {"Content-Type": "text/plain"}, ROBOTS_CON_CRAWL_DELAY.encode()),
        "https://www.inpa.gov.it/pagina":
            (200, {"Content-Type": "text/html"}, b"<html></html>"),
    }
    client = _client(routes)

    client.get("https://www.inpa.gov.it/pagina")

    assert client._opener.count("https://www.inpa.gov.it/robots.txt") == 1


def test_robots_letto_una_volta_sola_per_piu_richieste():
    """La cache per host regge fra richieste successive."""
    routes = {
        "https://www.inpa.gov.it/robots.txt":
            (200, {"Content-Type": "text/plain"}, ROBOTS_CON_CRAWL_DELAY.encode()),
        "https://www.inpa.gov.it/a": (200, {"Content-Type": "text/html"}, b"a"),
        "https://www.inpa.gov.it/b": (200, {"Content-Type": "text/html"}, b"b"),
    }
    client = _client(routes)

    client.get("https://www.inpa.gov.it/a")
    client.get("https://www.inpa.gov.it/b")

    assert client._opener.count("https://www.inpa.gov.it/robots.txt") == 1


def test_crawl_delay_dichiarato_prevale_sull_intervallo_configurato():
    """Un Crawl-delay piu' alto del nostro default va rispettato, non ignorato."""
    routes = {
        "https://www.inpa.gov.it/robots.txt":
            (200, {"Content-Type": "text/plain"}, ROBOTS_CON_CRAWL_DELAY.encode()),
        "https://www.inpa.gov.it/a": (200, {"Content-Type": "text/html"}, b"a"),
        "https://www.inpa.gov.it/b": (200, {"Content-Type": "text/html"}, b"b"),
    }
    attese: list[float] = []
    client = HttpClient(HttpConfig(min_interval=2.0, max_retries=0),
                        sleep=attese.append)
    client._opener = _CountingOpener(routes)

    client.get("https://www.inpa.gov.it/a")
    client.get("https://www.inpa.gov.it/b")

    # La soglia e' 4.9 e non 5.0 di proposito: il client attende il delay MENO
    # il tempo gia' trascorso dalla richiesta precedente, quindi il valore
    # osservato e' appena sotto i 5 s. Pretendere 5.0 esatti verificherebbe
    # l'aritmetica dell'orologio invece del comportamento che interessa.
    assert any(attesa >= 4.9 for attesa in attese), (
        f"atteso un intervallo vicino ai 5 s dal Crawl-delay, ottenuto {attese}"
    )


def test_rientranza_nella_policy_non_diventa_permesso():
    """Se la policy viene richiamata mentre sta scaricando, nega.

    Seconda barriera al difetto sopra, indipendente dal client: un fetcher che
    interroghi la policy durante il proprio lavoro riceve un rifiuto prudente
    invece di un permesso implicito o di una ricorsione.
    """
    visite: list[str] = []

    def fetcher_rientrante(url: str) -> tuple[int, str]:
        visite.append(url)
        # Durante il recupero, qualcosa chiede di nuovo le direttive.
        policy.check("https://esempio.test/qualsiasi")
        return 200, "User-agent: *\nDisallow: /\n"

    policy = RobotsPolicy_factory(fetcher_rientrante)
    decisione = policy.check("https://esempio.test/pagina")

    assert len(visite) == 1
    assert decisione.allowed is False


def RobotsPolicy_factory(fetcher):
    return R.RobotsPolicy(fetcher=fetcher, user_agent="Test/1.0")


@pytest.mark.parametrize("schema", ["http", "ftp", "file"])
def test_solo_https(schema):
    """Nessuno schema oltre https, nemmeno per i redirect."""
    from inpa_bot.source.errors import UnsafeUrlError

    client = _client({})
    with pytest.raises(UnsafeUrlError):
        client.validate_url(f"{schema}://www.inpa.gov.it/x")
