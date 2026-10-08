"""Client HTTP: sicurezza dell'URL, ritmo, redirect ed errori (SPEC 5.3, 11).

Nessun test tocca la rete. Al posto del trasporto c'e' un opener finto che
conta le richieste, al posto dell'attesa una funzione che registra quanto le
e' stato chiesto di dormire: cosi' l'intervallo di cortesia si verifica in
millisecondi invece che in secondi reali, e si verifica davvero, perche' il
valore richiesto resta ispezionabile.

`allow_private_addresses=True` compare in tutte le configurazioni di prova:
senza, `validate_url` risolverebbe il nome via DNS e i test offline
dipenderebbero dalla rete proprio nel punto in cui devono esserne
indipendenti.
"""

from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

from inpa_bot.source.errors import (
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    TransportError,
    UnsafeUrlError,
)
from inpa_bot.source.http import HttpClient, HttpConfig, _NoRedirect, _parse_retry_after
from inpa_bot.source.robots import RobotsPolicy

from .conftest import fetcher_robots_fixture

PAGINA = "https://www.inpa.gov.it/bandi-e-avvisi/"
ALTRA_PAGINA = "https://www.inpa.gov.it/faq-domande-e-risposte/"
HTML = {"Content-Type": "text/html; charset=utf-8"}


# --------------------------------------------------------------------------
# Doppi
# --------------------------------------------------------------------------


class _RispostaFinta:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._buffer = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> _RispostaFinta:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class OpenerFinto:
    """Trasporto finto: serve risposte preparate e conta le richieste.

    Ogni URL ha una coda di esiti; l'ultimo resta valido per le chiamate
    successive, cosi' si puo' descrivere sia "429 e poi 200" sia "sempre
    guasto" senza due classi diverse.
    """

    def __init__(self, rotte: dict[str, list[object]]) -> None:
        self.rotte = {url: list(esiti) for url, esiti in rotte.items()}
        self.chiamate: list[str] = []

    def open(self, request, timeout=None):  # noqa: ANN001 - firma di urllib
        url = request.full_url
        self.chiamate.append(url)
        esiti = self.rotte.get(url)
        if not esiti:
            raise urllib.error.URLError(f"nessuna rotta preparata per {url}")
        esito = esiti.pop(0) if len(esiti) > 1 else esiti[0]
        if isinstance(esito, Exception):
            raise esito
        status, headers, body = esito
        return _RispostaFinta(status, headers, body)

    def conta(self, url: str) -> int:
        return sum(1 for chiamata in self.chiamate if chiamata == url)


def errore_http(url: str, codice: int, intestazioni: dict[str, str] | None = None,
                corpo: bytes = b"") -> urllib.error.HTTPError:
    testata = email.message.Message()
    for chiave, valore in (intestazioni or {}).items():
        testata[chiave] = valore
    return urllib.error.HTTPError(url, codice, f"HTTP {codice}", testata,
                                  io.BytesIO(corpo))


def costruisci(rotte: dict[str, list[object]], *, robots_txt: str | None = None,
               **parametri) -> tuple[HttpClient, OpenerFinto, list[float]]:
    """Client isolato: trasporto finto, attese registrate, orologio fermo.

    L'orologio fermo e' voluto: con il tempo che non avanza, l'intervallo fra
    due richieste non trascorre mai da solo e il client e' costretto a
    chiederne l'attesa, che e' proprio cio' che si vuole osservare.
    """
    attese: list[float] = []
    configurazione = HttpConfig(allow_private_addresses=True, **parametri)
    if robots_txt is None:
        policy = RobotsPolicy(fetcher=fetcher_robots_fixture,
                              user_agent=configurazione.user_agent)
    else:
        policy = RobotsPolicy(fetcher=lambda url: (200, robots_txt),
                              user_agent=configurazione.user_agent)
    client = HttpClient(configurazione, robots=policy,
                        sleep=attese.append, clock=lambda: 0.0)
    opener = OpenerFinto(rotte)
    client._opener = opener  # noqa: SLF001 - sostituzione del trasporto
    return client, opener, attese


# --------------------------------------------------------------------------
# validate_url
# --------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["http", "ftp", "file"])
def test_solo_https_e_accettato(schema):
    """Anche un link inviato da un utente passa di qui (SPEC 9)."""
    client, _, _ = costruisci({})

    with pytest.raises(UnsafeUrlError):
        client.validate_url(f"{schema}://www.inpa.gov.it/x")


@pytest.mark.parametrize(
    "url",
    [
        "https://esempio.test/x",
        "https://www.inpa.gov.it.esempio.test/x",
        "https://portale.inpa.gov.it/api/media/x",
        "https://localhost/x",
    ],
)
def test_host_fuori_elenco_rifiutato(url):
    """L'elenco chiuso e' la barriera primaria contro gli indirizzi interni.

    Anche `portale.inpa.gov.it` e' fuori: aggiungerlo richiede una verifica
    esplicita delle condizioni, non una riga di configurazione.
    """
    client, _, _ = costruisci({})

    with pytest.raises(UnsafeUrlError):
        client.validate_url(url)


def test_porta_diversa_da_443_rifiutata():
    """Un servizio su una porta insolita non e' il sito pubblico."""
    client, _, _ = costruisci({})

    with pytest.raises(UnsafeUrlError):
        client.validate_url("https://www.inpa.gov.it:8443/x")


def test_url_senza_host_rifiutato():
    client, _, _ = costruisci({})

    with pytest.raises(UnsafeUrlError):
        client.validate_url("https:///percorso")


def test_host_ammesso_viene_normalizzato_in_minuscolo():
    client, _, _ = costruisci({})

    assert client.validate_url("https://WWW.INPA.GOV.IT/x") == "www.inpa.gov.it"


# --------------------------------------------------------------------------
# Redirect
# --------------------------------------------------------------------------


def test_l_opener_predefinito_non_segue_i_redirect_da_solo():
    """La disattivazione e' nel trasporto, non solo nella logica del client."""
    client = HttpClient(HttpConfig(allow_private_addresses=True))

    gestori = [h for h in client._opener.handlers if isinstance(h, _NoRedirect)]

    assert gestori
    assert gestori[0].redirect_request(None, None, 302, "", {},
                                       "https://esempio.test/") is None


def test_ogni_salto_e_una_richiesta_decisa_dal_client():
    """Il redirect si segue a mano, e resta tracciato dove si e' passati."""
    client, opener, _ = costruisci({
        PAGINA: [(302, {"Location": ALTRA_PAGINA}, b"")],
        ALTRA_PAGINA: [(200, HTML, b"<html>ok</html>")],
    })

    risposta = client.get(PAGINA)

    assert opener.chiamate == [PAGINA, ALTRA_PAGINA]
    assert risposta.status == 200
    assert risposta.url == PAGINA
    assert risposta.final_url == ALTRA_PAGINA
    assert risposta.redirects == (PAGINA,)


def test_un_redirect_verso_un_host_non_ammesso_viene_fermato():
    """E' il modo tipico di finire su un host mai valutato (SPEC 11)."""
    client, opener, _ = costruisci({
        PAGINA: [(302, {"Location": "https://esempio.test/altrove"}, b"")],
    })

    with pytest.raises(UnsafeUrlError):
        client.get(PAGINA)

    assert opener.conta("https://esempio.test/altrove") == 0


def test_catena_di_redirect_troppo_lunga_interrotta():
    """Un ciclo di redirect non deve diventare un ciclo di richieste."""
    client, opener, _ = costruisci(
        {PAGINA: [(302, {"Location": PAGINA}, b"")]}, max_redirects=2,
    )

    with pytest.raises(UnsafeUrlError):
        client.get(PAGINA)

    assert opener.conta(PAGINA) == 3


def test_redirect_senza_location_e_un_errore_di_stato():
    """Non c'e' nulla da seguire: non si indovina la destinazione."""
    client, _, _ = costruisci({PAGINA: [(301, {}, b"")]})

    with pytest.raises(HttpStatusError):
        client.get(PAGINA)


# --------------------------------------------------------------------------
# Stati HTTP
# --------------------------------------------------------------------------


@pytest.mark.parametrize("codice", [401, 403])
def test_accesso_negato_interrompe_senza_ritentare(codice):
    """Nessun tentativo con credenziali, nessun cambio di agente (SPEC 3.1)."""
    client, opener, attese = costruisci(
        {PAGINA: [errore_http(PAGINA, codice, corpo=b"negato")]}, max_retries=3,
    )

    with pytest.raises(HttpStatusError) as errore:
        client.get(PAGINA)

    assert errore.value.status == codice
    assert opener.conta(PAGINA) == 1
    assert attese == []
    assert client.records[-1].error == f"HTTP {codice}"


def test_retry_after_viene_rispettato():
    """Su 429 si attende quanto dichiarato, non il backoff calcolato."""
    client, opener, attese = costruisci(
        {PAGINA: [errore_http(PAGINA, 429, {"Retry-After": "7"}),
                  (200, HTML, b"<html>ok</html>")]},
        max_retries=2, min_interval=0.0,
    )

    risposta = client.get(PAGINA)

    assert risposta.status == 200
    assert opener.conta(PAGINA) == 2
    assert attese == [7.0]


def test_un_retry_after_oltre_il_tetto_ferma_invece_di_ritentare_prima():
    """Se la fonte chiede piu' attesa di quanta se ne accetti, si smette.

    Il comportamento precedente era `min(retry_after, max_backoff)`: con un
    `Retry-After: 600` il client dormiva 60 secondi e ribussava, cioe'
    ignorava una richiesta esplicita del server. SPEC 5.3 impone di rispettare
    `Retry-After`, e SPEC 3.1 vieta di aggirare i blocchi.

    Le uniche due risposte coerenti sono aspettare davvero o fermarsi. Questo
    livello si ferma e riporta l'attesa dichiarata: riprogrammare il lavoro e'
    una decisione che spetta a chi ha chiamato, non al client HTTP.
    """
    client, opener, attese = costruisci(
        {PAGINA: [errore_http(PAGINA, 503, {"Retry-After": "600"}),
                  (200, HTML, b"ok")]},
        max_retries=2, max_backoff=60.0, min_interval=0.0,
    )

    with pytest.raises(HttpStatusError) as errore:
        client.get(PAGINA)

    assert errore.value.retry_after == 600.0
    assert attese == []
    assert opener.conta(PAGINA) == 1


def test_un_retry_after_entro_il_tetto_viene_rispettato_alla_lettera():
    """Sotto il tetto, l'attesa e' quella dichiarata: non il backoff nostro."""
    client, _, attese = costruisci(
        {PAGINA: [errore_http(PAGINA, 503, {"Retry-After": "30"}),
                  (200, HTML, b"ok")]},
        max_retries=2, max_backoff=60.0, min_interval=0.0,
    )

    client.get(PAGINA)

    assert attese == [30.0]


def test_uno_stato_non_ritentabile_non_viene_ritentato():
    """Un 404 non migliora riprovando: si segnala e basta."""
    client, opener, _ = costruisci({PAGINA: [errore_http(PAGINA, 404)]},
                                    max_retries=3)

    with pytest.raises(HttpStatusError) as errore:
        client.get(PAGINA)

    assert errore.value.status == 404
    assert opener.conta(PAGINA) == 1


def test_un_guasto_di_trasporto_non_diventa_un_errore_di_stato():
    """Un fallimento DNS non e' un 403 ne' un blocco della fonte (SPEC 1.3)."""
    client, opener, attese = costruisci(
        {PAGINA: [urllib.error.URLError("Temporary failure in name resolution")]},
        max_retries=2, min_interval=0.0,
    )

    with pytest.raises(TransportError):
        client.get(PAGINA)

    assert opener.conta(PAGINA) == 3
    # Attese crescenti fra un tentativo e l'altro, non a raffica (SPEC 5.3).
    assert attese == [2.0, 4.0]
    assert all(record.status is None for record in client.records)


# --------------------------------------------------------------------------
# Limiti e ritmo
# --------------------------------------------------------------------------


def test_il_tetto_di_byte_scatta_durante_la_lettura():
    """Il limite non dipende dalla buona fede del `Content-Length`."""
    client, _, _ = costruisci({PAGINA: [(200, HTML, b"x" * 5000)]}, max_bytes=1024)

    with pytest.raises(ResponseTooLargeError):
        client.get(PAGINA)


def test_fra_due_richieste_allo_stesso_host_viene_chiesta_l_attesa_minima():
    """Una richiesta per volta, con intervallo minimo (SPEC 5.3).

    Si ispeziona l'attesa richiesta e non il tempo trascorso: quest'ultimo
    misurerebbe la velocita' della macchina, non il comportamento del client.
    """
    client, _, attese = costruisci(
        {PAGINA: [(200, HTML, b"a")], ALTRA_PAGINA: [(200, HTML, b"b")]},
        min_interval=2.0,
    )

    client.get(PAGINA)
    assert attese == []

    client.get(ALTRA_PAGINA)
    assert attese == [2.0]


def test_ogni_richiesta_lascia_una_traccia_con_la_regola_robots():
    """I rapporti di M0-09 si costruiscono da qui, non da un log di testo."""
    client, _, _ = costruisci({PAGINA: [(200, HTML, b"<html>ok</html>")]})

    client.get(PAGINA)

    traccia = client.records[-1]
    assert traccia.url == PAGINA
    assert traccia.method == "GET"
    assert traccia.status == 200
    assert traccia.bytes_read == len(b"<html>ok</html>")
    assert traccia.robots_rule is not None


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def test_un_percorso_vietato_non_diventa_una_richiesta():
    """La condizione della fonte si applica prima del trasporto, non dopo."""
    client, opener, _ = costruisci(
        {"https://www.inpa.gov.it/privato/x": [(200, HTML, b"segreto")]},
        robots_txt="User-agent: *\nDisallow: /privato/\n",
    )

    with pytest.raises(RobotsDisallowedError) as errore:
        client.get("https://www.inpa.gov.it/privato/x")

    assert opener.chiamate == []
    assert "Disallow: /privato/" in errore.value.rule
    assert client.records[-1].error == "robots_disallowed"


def test_un_percorso_consentito_passa():
    """Il rifiuto deve restare l'eccezione, non il comportamento predefinito."""
    client, opener, _ = costruisci(
        {PAGINA: [(200, HTML, b"<html>ok</html>")]},
        robots_txt="User-agent: *\nDisallow: /privato/\n",
    )

    assert client.get(PAGINA).status == 200
    assert opener.conta(PAGINA) == 1


# --------------------------------------------------------------------------
# Lettura della risposta
# --------------------------------------------------------------------------


def test_il_corpo_viene_decodificato_con_il_charset_dichiarato():
    """Una scheda latin-1 letta come UTF-8 perderebbe gli accenti."""
    client, _, _ = costruisci({
        PAGINA: [(200, {"Content-Type": "text/html; charset=iso-8859-1"},
                  "città".encode("iso-8859-1"))],
    })

    risposta = client.get(PAGINA)

    assert risposta.content_type == "text/html"
    assert risposta.charset == "iso-8859-1"
    assert risposta.text() == "città"


def test_un_charset_impossibile_non_fa_fallire_la_lettura():
    """Meglio un carattere sostituito che un'acquisizione persa."""
    client, _, _ = costruisci({
        PAGINA: [(200, {"Content-Type": "text/html; charset=inesistente-9"},
                  b"testo")],
    })

    assert client.get(PAGINA).text() == "testo"


# `_parse_retry_after` e' privata, ma le sue due forme vengono dal protocollo
# e non dal nostro codice: vale la pena fissarle qui invece di dedurle da un
# test di piu' alto livello che ne mostrerebbe solo una.
def test_retry_after_accetta_secondi_e_data_http():
    assert _parse_retry_after("7") == 7.0
    assert _parse_retry_after("-3") == 0.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("domani") is None
    assert _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0
