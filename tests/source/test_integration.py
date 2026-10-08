"""Prova di integrazione contro inPA: opt-in, minima, mai automatica.

Questi test escono davvero in rete. Girano solo con `INPA_INTEGRATION=1`
nell'ambiente e sono marcati `integration`, quindi una CI che esegua
`pytest` senza variabili d'ambiente li salta: la specifica (3.5) chiede
test offline *e* integrazione opt-in, non un'integrazione che parte per
sbaglio a ogni commit.

    INPA_INTEGRATION=1 python -m pytest tests/source/test_integration.py -q

Il perimetro e' volutamente piccolo e fisso, perche' anche una prova deve
rispettare le condizioni della fonte (SPEC 3.1, 5.3): una sola pagina di
ricerca, una sola scheda, l'intervallo di cortesia di 2 secondi, nessun
allegato scaricato. Le richieste totali restano meno di dieci.

Le verifiche sono deliberatamente deboli sui *valori* e forti sulla
*forma*: i bandi pubblicati cambiano ogni giorno, quindi pretendere un
titolo o un conteggio preciso renderebbe rossi questi test senza che nulla
si sia rotto. Cio' che deve restare vero e' il contratto.
"""

from __future__ import annotations

import os
import urllib.parse

import pytest

from inpa_bot.source.adapter import ATTACHMENT_HOST, InpaSitemapAdapter, build_default_client
from inpa_bot.source.models import ExtractionState, SearchFilters
from inpa_bot.source.sitemap import NOTICE_ID_RE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INPA_INTEGRATION") != "1",
        reason="test di rete: attivare esplicitamente con INPA_INTEGRATION=1",
    ),
]

VOCI_DA_LEGGERE = 3
"""Dimensione della pagina: il minimo che permette comunque di vedere una
paginazione. La prova non deve scaricare l'intero perimetro."""


@pytest.fixture(scope="module")
def client():
    """Client predefinito: due host, robots rispettato, 2 s fra le richieste."""
    return build_default_client()


@pytest.fixture(scope="module")
def adattatore(client) -> InpaSitemapAdapter:
    return InpaSitemapAdapter(client, page_size=VOCI_DA_LEGGERE)


@pytest.fixture(scope="module")
def prima_pagina(adattatore):
    """Una sola ricerca per l'intero modulo: il catalogo resta in cache."""
    return adattatore.search(SearchFilters())


@pytest.fixture(scope="module")
def scheda_reale(adattatore, prima_pagina):
    """Una sola scheda, la prima dell'elenco."""
    return adattatore.fetch_notice(prima_pagina.records[0].notice_id)


def test_il_ritmo_configurato_e_quello_concordato(client):
    """Default prudente della specifica: 2 secondi fra richieste allo stesso host."""
    assert client.config.min_interval >= 2.0
    assert ATTACHMENT_HOST not in client.config.allowed_hosts


def test_la_ricerca_reale_restituisce_bandi_identificabili(prima_pagina):
    """La rotta di scoperta funziona e gli ID sono nella forma osservata."""
    assert prima_pagina.records
    assert len(prima_pagina.records) <= VOCI_DA_LEGGERE
    assert prima_pagina.declared_total and prima_pagina.declared_total > 0
    for stub in prima_pagina.records:
        assert NOTICE_ID_RE.fullmatch(stub.notice_id)
        assert stub.source_url.startswith("https://www.inpa.gov.it/bandi-e-avvisi/")
    # Con piu' voci del totale di pagina ci deve essere un seguito: se
    # mancasse, la paginazione starebbe perdendo silenziosamente dei bandi.
    if prima_pagina.declared_total > len(prima_pagina.records):
        assert prima_pagina.next_cursor is not None


def test_una_scheda_reale_viene_letta_senza_credenziali(scheda_reale):
    """M0-01: ricerca e dettaglio ottenuti senza identita' digitale."""
    assert scheda_reale.title
    assert scheda_reale.description
    assert scheda_reale.metadata_hash and scheda_reale.raw_document_hash
    assert scheda_reale.status_original is not None
    assert scheda_reale.has_blocking_anomaly is False
    # Anche dal vivo: la data di apertura non diventa una pubblicazione.
    assert scheda_reale.published_at is None


def test_gli_allegati_reali_restano_non_prelevabili(adattatore, client, scheda_reale):
    """Il vincolo di `portale.inpa.gov.it` va verificato sulla fonte vera.

    Se un giorno quell'host smettesse di dichiarare `Disallow: /`, questo
    test cambierebbe esito ed e' giusto che lo faccia: sarebbe un
    cambiamento delle condizioni di accesso, cioe' una notizia.
    """
    if not scheda_reale.attachments:
        pytest.skip("la scheda letta non pubblica allegati")

    riferimento = scheda_reale.attachments[0]
    assert riferimento.fetch_permitted is False

    richieste_prima = len(client.records)
    payload = adattatore.fetch_attachment(riferimento)

    assert payload.state is ExtractionState.FETCH_BLOCKED
    assert len(client.records) == richieste_prima


def test_le_richieste_allo_stesso_host_sono_distanziate(client):
    """M0-07 in piccolo: il ritmo dichiarato e' anche quello praticato."""
    per_host: dict[str, list[float]] = {}
    for record in client.records:
        host = urllib.parse.urlsplit(record.url).hostname or ""
        per_host.setdefault(host, []).append(record.started_at.timestamp())

    for host, istanti in per_host.items():
        distanze = [b - a for a, b in zip(istanti, istanti[1:])]
        # Tolleranza sul limite: l'attesa e' calcolata al netto del tempo
        # gia' trascorso, quindi il valore osservato sfiora i 2 s da sotto.
        assert all(d >= 1.5 for d in distanze), f"{host}: {distanze}"
