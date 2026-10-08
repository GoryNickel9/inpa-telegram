"""Composizione della rotta verificata: sitemap, scheda, allegati (SPEC 4).

L'adattatore non aggiunge conoscenza sulla fonte, quindi i test non
verificano di nuovo il parser: verificano le tre decisioni che
l'adattatore prende da solo, e che sono scelte di progetto prima che
codice.

1. Un filtro che non si puo' applicare viene dichiarato, non simulato:
   `unapplied_filters` esiste perche' un filtro ignorato in silenzio e'
   peggio di un filtro assente (M0-03).
2. Un allegato non consentito non produce nessuna richiesta. Il test lo
   misura contando le richieste del client finto: devono essere zero.
3. La paginazione e' locale, quindi leggere tre pagine deve costare due
   richieste in tutto, non sei.

Il client finto serve i documenti scaricati il 19/09/2026 e non parla con
la rete; le condizioni di accesso arrivano dai robots.txt campionati.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from inpa_bot.source.adapter import (
    ATTACHMENT_HOST,
    InpaSitemapAdapter,
    build_default_client,
    detect_mime,
)
from inpa_bot.source.detail import build_detail_url
from inpa_bot.source.errors import (
    ContentTypeError,
    ParseError,
    RobotsDisallowedError,
    TransportError,
)
from inpa_bot.source.http import HttpConfig, Response
from inpa_bot.source.models import (
    AttachmentRef,
    ExtractionState,
    NoticeStatus,
    Provenance,
    SearchFilters,
)
from inpa_bot.source.robots import RobotsPolicy
from inpa_bot.source.sitemap import SITEMAP_INDEX_URL

from .conftest import (
    OSSERVATO_IL,
    SCHEDA_LOCALE,
    SCHEDA_SENZA_ALLEGATI,
    fetcher_robots_fixture,
    leggi_fixture,
    leggi_scheda,
)

URL_SITEMAP_BANDI = "https://www.inpa.gov.it/sitemap-bandi.xml"
XML = "application/xml"
HTML = "text/html"


class ClientFinto:
    """Sosia di `HttpClient` che serve i campioni e conta le richieste.

    Espone solo cio' che l'adattatore usa davvero: `get`, `config` e
    `robots`. Se un giorno l'adattatore cominciasse a usare altro, questo
    doppio smetterebbe di funzionare invece di nasconderlo.
    """

    def __init__(self, rotte: dict[str, object],
                 *, config: HttpConfig | None = None) -> None:
        self.rotte = rotte
        self.config = config or HttpConfig()
        self.robots = RobotsPolicy(fetcher=fetcher_robots_fixture,
                                   user_agent=self.config.user_agent)
        self.richieste: list[str] = []

    def get(self, url: str, *, accept: str = "*/*",
            check_robots: bool = True) -> Response:
        self.richieste.append(url)
        esito = self.rotte.get(url)
        if esito is None:
            raise TransportError(f"nessun campione per {url}")
        if isinstance(esito, Exception):
            raise esito
        content_type, corpo = esito
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
            content_type = f"{content_type}; charset=utf-8"
        return Response(
            url=url, final_url=url, status=200,
            headers={"content-type": content_type}, body=corpo,
            elapsed_s=0.01, fetched_at=OSSERVATO_IL,
        )


def _rotte_di_base() -> dict[str, object]:
    return {
        SITEMAP_INDEX_URL: (XML, leggi_fixture("sitemap-index.xml")),
        URL_SITEMAP_BANDI: (XML, leggi_fixture("sitemap-bandi.xml")),
        build_detail_url(SCHEDA_LOCALE): (HTML, leggi_scheda(SCHEDA_LOCALE)),
        build_detail_url(SCHEDA_SENZA_ALLEGATI):
            (HTML, leggi_scheda(SCHEDA_SENZA_ALLEGATI)),
    }


@pytest.fixture
def client() -> ClientFinto:
    return ClientFinto(_rotte_di_base())


@pytest.fixture
def adattatore(client: ClientFinto) -> InpaSitemapAdapter:
    return InpaSitemapAdapter(client, page_size=5)


# --------------------------------------------------------------------------
# Ricerca e paginazione
# --------------------------------------------------------------------------


def test_la_ricerca_restituisce_stub_non_bandi(adattatore):
    """La sitemap da' ID, URL e `lastmod`: aprire la scheda resta obbligatorio.

    Uno stub con un titolo lo avrebbe inventato, e sarebbe indistinguibile
    da uno letto davvero.
    """
    pagina = adattatore.search(SearchFilters())

    assert len(pagina.records) == 5
    assert pagina.declared_total == 12
    assert pagina.complete is True
    assert pagina.anomalies == ()
    primo = pagina.records[0]
    assert primo.notice_id == "df4a037fb81c4a9d94e5dfac92dedf4e"
    assert primo.source_url.startswith("https://www.inpa.gov.it/")
    assert primo.title is None


def test_la_paginazione_scorre_tutto_l_elenco_senza_duplicati(adattatore):
    """Tre pagine, dodici voci distinte, ultimo cursore assente."""
    visti: list[str] = []
    cursore: str | None = None
    pagine = 0

    while True:
        pagina = adattatore.search(SearchFilters(), cursore)
        visti.extend(stub.notice_id for stub in pagina.records)
        pagine += 1
        cursore = pagina.next_cursor
        if cursore is None:
            break

    assert pagine == 3
    assert len(visti) == 12
    assert len(set(visti)) == 12
    assert pagina.exhausted is True


def test_l_elenco_si_scarica_una_volta_sola(adattatore, client):
    """Paginare in memoria evita traffico inutile verso la fonte (SPEC 5.3)."""
    adattatore.search(SearchFilters())
    adattatore.search(SearchFilters(), "5")
    adattatore.search(SearchFilters(), "10")

    assert client.richieste == [SITEMAP_INDEX_URL, URL_SITEMAP_BANDI]


def test_refresh_obbliga_a_rileggere_la_sitemap(adattatore, client):
    """La cadenza di riacquisizione appartiene a chi orchestra (SPEC 5.2)."""
    adattatore.search(SearchFilters())
    adattatore.refresh()
    adattatore.search(SearchFilters())

    assert client.richieste.count(URL_SITEMAP_BANDI) == 2


def test_un_cursore_non_riconosciuto_non_riparte_da_zero(adattatore):
    """Ripartire in silenzio riconsegnerebbe pagine gia' lette come nuove."""
    with pytest.raises(ValueError):
        adattatore.search(SearchFilters(), "pagina-2")
    with pytest.raises(ValueError):
        adattatore.search(SearchFilters(), "-1")


# --------------------------------------------------------------------------
# Filtri
# --------------------------------------------------------------------------


def test_i_filtri_non_applicabili_vengono_elencati(adattatore):
    """Stato, territorio e testo stanno nella scheda, non nella sitemap.

    Restituirli come applicati, o restituire zero risultati, lascerebbe
    credere al chiamante che il filtro abbia funzionato (M0-03).
    """
    filtri = SearchFilters(text="istruttore",
                           statuses=(NoticeStatus.OPEN,),
                           region_codes=("IT-88",),
                           province_codes=("SS",))

    pagina = adattatore.search(filtri)

    assert pagina.unapplied_filters == ("text", "statuses", "region_codes",
                                        "province_codes")
    assert [a.code for a in pagina.anomalies] == ["filtri_non_applicati"]
    # I record non sono stati filtrati di nascosto: sono tutti quelli letti.
    assert len(pagina.records) == 5
    assert pagina.declared_total == 12


def test_updated_since_e_l_unico_filtro_applicabile(adattatore):
    """Il `lastmod` c'e' davvero, quindi questo filtro non e' una finzione."""
    soglia = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)

    pagina = adattatore.search(SearchFilters(updated_since=soglia), page_cursor=None)

    assert pagina.unapplied_filters == ()
    assert len(pagina.records) == 5  # le 4 voci delle 21:59 piu' quella delle 20:00
    assert pagina.next_cursor is None
    # Il totale resta quello pubblicato dalla fonte, non il numero dei record.
    assert pagina.declared_total == 12


def test_una_voce_senza_lastmod_confrontabile_resta_dentro_e_viene_contata(client):
    """Escluderla la direbbe vecchia sulla base di un dato non pubblicato."""
    sitemap = leggi_fixture("sitemap-bandi.xml").replace(
        "<lastmod>2026-09-18T21:59:00Z</lastmod>", "", 1
    )
    client.rotte[URL_SITEMAP_BANDI] = (XML, sitemap)
    adattatore = InpaSitemapAdapter(client, page_size=100)

    pagina = adattatore.search(SearchFilters(
        updated_since=datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc)
    ))

    assert "df4a037fb81c4a9d94e5dfac92dedf4e" in {s.notice_id for s in pagina.records}
    assert any(a.code == "lastmod_non_confrontabile" for a in pagina.anomalies)


# --------------------------------------------------------------------------
# Scoperta guasta
# --------------------------------------------------------------------------


def test_una_sitemap_dei_bandi_scomparsa_non_e_zero_bandi(client):
    """Due condizioni diverse che M0-08 vieta di confondere."""
    client.rotte[SITEMAP_INDEX_URL] = (XML, leggi_fixture("sitemap-index.xml").replace(
        "sitemap-bandi.xml", "sitemap-altro.xml"
    ))

    with pytest.raises(ParseError):
        InpaSitemapAdapter(client).search(SearchFilters())


def test_un_elenco_vuoto_non_puo_essere_dichiarato_completo(client):
    """Documento non `<urlset>` ed elenco vuoto sono indistinguibili da qui."""
    client.rotte[URL_SITEMAP_BANDI] = (XML, '<urlset xmlns="http://www.sitemaps.org/'
                                             'schemas/sitemap/0.9"></urlset>')

    pagina = InpaSitemapAdapter(client).search(SearchFilters())

    assert pagina.records == ()
    assert pagina.complete is False
    assert any(a.blocking for a in pagina.anomalies)


def test_le_anomalie_del_catalogo_accompagnano_ogni_pagina(client):
    """Chi legge la seconda pagina deve sapere che l'elenco era incompleto."""
    sitemap = leggi_fixture("sitemap-bandi.xml").replace(
        "?concorso_id=07bd3da8a1664e6f92325541b0cc92e5", "", 1
    )
    client.rotte[URL_SITEMAP_BANDI] = (XML, sitemap)
    adattatore = InpaSitemapAdapter(client, page_size=5)

    seconda = adattatore.search(SearchFilters(), "5")

    assert any(a.code == "sitemap_loc_senza_id" for a in seconda.anomalies)


# --------------------------------------------------------------------------
# Scheda
# --------------------------------------------------------------------------


def test_la_scheda_viene_letta_con_le_condizioni_di_accesso_gia_valutate(adattatore):
    """Gli allegati nascono non prelevabili, con la regola che lo impedisce.

    E' il robots.txt reale di `portale.inpa.gov.it` a deciderlo, non una
    costante di comodo.
    """
    snapshot = adattatore.fetch_notice(SCHEDA_LOCALE)

    assert snapshot.notice_id == SCHEDA_LOCALE
    assert snapshot.status_normalized is NoticeStatus.CLOSED
    assert snapshot.observed_at == OSSERVATO_IL
    assert len(snapshot.attachments) == 2
    for allegato in snapshot.attachments:
        assert allegato.fetch_permitted is False
        assert "Disallow: /" in allegato.fetch_block_reason


def test_una_risposta_che_non_e_html_non_diventa_un_bando(client):
    """Una pagina di cortesia servita con stato 200 non e' una scheda (M0-08)."""
    client.rotte[build_detail_url(SCHEDA_LOCALE)] = ("application/json", '{"x":1}')

    with pytest.raises(ContentTypeError):
        InpaSitemapAdapter(client).fetch_notice(SCHEDA_LOCALE)


def test_list_attachments_espone_gli_stessi_riferimenti_della_scheda(adattatore):
    """Non esiste un indirizzo per i soli allegati: costa una scheda intera."""
    riferimenti = adattatore.list_attachments(SCHEDA_LOCALE)

    assert [r.filename for r in riferimenti] == [
        "Bando operatore TD Vibo Valentia_signed.pdf",
        "Allegato A bando Operatore_.docx",
    ]


def test_una_scheda_senza_allegati_non_ne_inventa(adattatore):
    assert adattatore.list_attachments(SCHEDA_SENZA_ALLEGATI) == ()


# --------------------------------------------------------------------------
# Allegati
# --------------------------------------------------------------------------


def _riferimento(url: str = "https://portale.inpa.gov.it/api/media/uno", *,
                 permesso: bool, motivo: str | None = None) -> AttachmentRef:
    return AttachmentRef(
        notice_id=SCHEDA_LOCALE, url=url,
        provenance=Provenance(source_url="https://www.inpa.gov.it/scheda",
                              observed_at=OSSERVATO_IL),
        filename="Bando.pdf", declared_extension="pdf",
        fetch_permitted=permesso, fetch_block_reason=motivo,
    )


def test_un_allegato_non_consentito_non_produce_nessuna_richiesta(adattatore, client):
    """Riprovare sperando in un esito diverso e' l'elusione vietata (SPEC 3.1).

    Il conteggio delle richieste e' la prova: zero, nemmeno un tentativo.
    """
    riferimento = _riferimento(permesso=False, motivo="[*] Disallow: /")

    payload = adattatore.fetch_attachment(riferimento)

    assert client.richieste == []
    assert payload.state is ExtractionState.FETCH_BLOCKED
    assert payload.content is None
    assert payload.ref is riferimento
    assert payload.anomalies[0].code == "allegato_non_prelevabile"
    assert payload.anomalies[0].context == "[*] Disallow: /"


def test_un_allegato_consentito_viene_scaricato_con_hash_e_firma():
    """Con una deroga documentata il download avviene e resta verificabile."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto({url: ("application/pdf", b"%PDF-1.7\nfinto")},
                          config=config)

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    assert payload.state is ExtractionState.OK
    assert payload.size_bytes == len(b"%PDF-1.7\nfinto")
    assert payload.sha256 and len(payload.sha256) == 64
    assert payload.detected_mime == "application/pdf"
    assert payload.declared_mime == "application/pdf"
    assert payload.anomalies == ()


def test_una_pagina_html_servita_al_posto_del_pdf_viene_segnalata():
    """Il caso di SPEC A.1: non rinominare un errore HTML in documento."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto(
        {url: ("application/pdf", b"<html><body>Errore</body></html>")},
        config=config,
    )

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    # Lo stato resta OK: i byte sono arrivati. Cio' che non torna e' il tipo,
    # e va detto nel dato invece che nel nome del file.
    assert payload.state is ExtractionState.OK
    assert payload.detected_mime == "text/html"
    assert [a.code for a in payload.anomalies] == ["mime_incoerente"]


def test_un_docx_dichiarato_per_esteso_non_e_una_contraddizione():
    """Gli OOXML sono archivi ZIP: la firma non puo' vedere altro."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto({url: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04qualcosa",
    )}, config=config)

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    assert payload.anomalies == ()


def test_un_guasto_nel_download_non_fa_fallire_l_acquisizione():
    """Un documento illeggibile non deve far perdere i bandi letti bene."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto({url: TransportError("connessione interrotta")},
                          config=config)

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    assert payload.state is ExtractionState.FETCH_FAILED
    assert payload.anomalies[0].context == "TransportError"


def test_un_divieto_sopraggiunto_resta_una_condizione_non_un_guasto():
    """Il robots.txt puo' cambiare fra l'osservazione e il download."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto({url: RobotsDisallowedError(url, "[*] Disallow: /")},
                          config=config)

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    assert payload.state is ExtractionState.FETCH_BLOCKED
    assert payload.anomalies[0].context == "[*] Disallow: /"


def test_un_corpo_vuoto_si_distingue_da_un_documento_non_scaricato():
    """Lo sha256 di zero byte e' comunque un'osservazione."""
    url = "https://portale.inpa.gov.it/api/media/uno"
    config = HttpConfig(allowed_hosts=HttpConfig().allowed_hosts | {ATTACHMENT_HOST})
    client = ClientFinto({url: ("application/pdf", b"")}, config=config)

    payload = InpaSitemapAdapter(client).fetch_attachment(
        _riferimento(url, permesso=True)
    )

    assert payload.state is ExtractionState.EMPTY
    assert payload.size_bytes == 0
    assert payload.sha256 is not None


# --------------------------------------------------------------------------
# Riconoscimento del tipo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("byte_iniziali", "atteso"),
    [
        (b"%PDF-1.7\n...", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"{\\rtf1\\ansi", "application/rtf"),
        (b"\x30\x82\x0a\x1b", "application/pkcs7-mime"),
        (b"<!DOCTYPE html><html>", "text/html"),
        (b"\xef\xbb\xbf  <html>", "text/html"),
    ],
)
def test_detect_mime_riconosce_le_firme_osservate(byte_iniziali, atteso):
    """Serve a confrontare cio' che il server dichiara con cio' che ha mandato."""
    assert detect_mime(byte_iniziali) == atteso


@pytest.mark.parametrize("dati", [b"", b"testo semplice", b"\x00\x01\x02"])
def test_una_firma_ignota_resta_un_dato_assente(dati):
    """Un tipo "probabile" contraddirebbe il dichiarato senza motivo."""
    assert detect_mime(dati) is None


# --------------------------------------------------------------------------
# Costruzione del client
# --------------------------------------------------------------------------


def test_il_client_predefinito_non_parla_con_l_host_degli_allegati():
    """L'unico perimetro per cui le condizioni sono state verificate."""
    client = build_default_client()

    assert ATTACHMENT_HOST not in client.config.allowed_hosts
    assert "www.inpa.gov.it" in client.config.allowed_hosts


def test_una_deroga_senza_motivazione_e_rifiutata():
    """Chi la legge fra sei mesi non saprebbe distinguerla da una svista."""
    with pytest.raises(ValueError):
        build_default_client(allow_attachment_host="   ")


def test_una_deroga_documentata_allarga_il_perimetro():
    """Non e' un interruttore: e' il testo dell'autorizzazione ricevuta."""
    client = build_default_client(
        allow_attachment_host="autorizzazione via email di <ufficio>, 20/09/2026"
    )

    assert ATTACHMENT_HOST in client.config.allowed_hosts


def test_page_size_deve_essere_almeno_uno():
    with pytest.raises(ValueError):
        InpaSitemapAdapter(ClientFinto({}), page_size=0)
