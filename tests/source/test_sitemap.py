"""Lettura delle sitemap XML: la rotta di scoperta del progetto.

I documenti usati sono quelli scaricati dalla fonte il 19/09/2026:
`sitemap-index.xml` (l'indice con tre sitemap) e `sitemap-bandi.xml`
ridotto a 12 voci. Cio' che i test devono dimostrare non e' che
ElementTree sappia leggere l'XML, ma che una voce inutilizzabile diventi
un'anomalia visibile invece di sparire in silenzio (M0-08).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from inpa_bot.source import sitemap as sitemap_mod
from inpa_bot.source.errors import ParseError
from inpa_bot.source.sitemap import (
    SitemapEntry,
    entries_to_stubs,
    extract_notice_id,
    parse_sitemap_index,
    parse_urlset,
)

from .conftest import OSSERVATO_IL

ID_PRIMA_VOCE = "df4a037fb81c4a9d94e5dfac92dedf4e"
URL_SITEMAP_BANDI = "https://www.inpa.gov.it/sitemap-bandi.xml"


def _voce(loc: str, lastmod: str | None = "2026-09-18T21:59:00Z") -> SitemapEntry:
    return SitemapEntry(loc=loc, lastmod_raw=lastmod, changefreq="daily",
                        priority="1.0")


# --------------------------------------------------------------------------
# Indice e urlset
# --------------------------------------------------------------------------


def test_indice_reale_elenca_le_tre_sitemap_pubblicate(testo_fixture):
    """L'indice osservato rimanda a pages, posts e bandi."""
    locations = parse_sitemap_index(testo_fixture("sitemap-index.xml"))

    assert len(locations) == 3
    assert any(loc.endswith("sitemap-bandi.xml") for loc in locations)


def test_urlset_reale_legge_loc_lastmod_e_metadati(testo_fixture):
    """Le voci restano stringhe grezze: interpretarle qui perderebbe l'originale."""
    entries = parse_urlset(testo_fixture("sitemap-bandi.xml"))

    assert len(entries) == 12
    prima = entries[0]
    # L'host pubblicato e' senza `www`: la riscrittura non avviene qui.
    assert prima.loc.startswith("https://inpa.gov.it/bandi-e-avvisi/")
    assert prima.lastmod_raw == "2026-09-18T21:59:00Z"
    assert prima.changefreq == "daily"
    assert prima.priority == "1.0"


def test_un_urlset_non_e_un_indice_e_viceversa(testo_fixture):
    """Radice diversa da quella attesa significa nessuna voce, non un errore.

    Dedurre una sitemap figlia da un documento che non e' un indice sarebbe
    un valore inventato.
    """
    assert parse_sitemap_index(testo_fixture("sitemap-bandi.xml")) == ()
    assert parse_urlset(testo_fixture("sitemap-index.xml")) == ()


def test_url_senza_loc_viene_saltato():
    """La posizione e' il contenuto della voce: senza, non resta nulla."""
    documento = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><lastmod>2026-09-18T21:59:00Z</lastmod></url>"
        f"<url><loc>https://inpa.gov.it/x?concorso_id={ID_PRIMA_VOCE}</loc></url>"
        "</urlset>"
    )

    entries = parse_urlset(documento)

    assert len(entries) == 1


def test_doctype_rifiutato_senza_interpretare_il_documento():
    """Difesa dall'espansione delle entita': una sitemap non ha DTD (SPEC 11)."""
    with pytest.raises(ParseError):
        parse_urlset('<!DOCTYPE urlset [<!ENTITY a "b">]><urlset/>')


def test_xml_malformato_diventa_ParseError_e_non_ParseError_di_elementtree():
    """A valle si distingue "documento non letto" da "nessun risultato"."""
    with pytest.raises(ParseError):
        parse_urlset("<urlset><url>")


def test_documento_oltre_il_tetto_non_viene_interpretato(monkeypatch):
    """Il limite di caratteri protegge la memoria del parser (SPEC 11)."""
    monkeypatch.setattr(sitemap_mod, "MAX_SITEMAP_CHARS", 32)

    with pytest.raises(ParseError):
        parse_urlset("<urlset>" + " " * 64 + "</urlset>")


# --------------------------------------------------------------------------
# Identificativo
# --------------------------------------------------------------------------


def test_id_letto_solo_dal_parametro_concorso_id():
    """L'id e' una stringa opaca restituita dalla fonte (SPEC 2.2)."""
    loc = f"https://inpa.gov.it/bandi-e-avvisi/x/?concorso_id={ID_PRIMA_VOCE}"

    assert extract_notice_id(loc) == ID_PRIMA_VOCE


@pytest.mark.parametrize(
    "loc",
    [
        "https://inpa.gov.it/bandi-e-avvisi/",
        "https://inpa.gov.it/x?concorso_id=troppo-corto",
        f"https://inpa.gov.it/x/{ID_PRIMA_VOCE}",
    ],
)
def test_id_non_ricavato_dal_percorso_ne_da_forme_diverse(loc):
    """Ricavarlo altrove produrrebbe identita' di comodo."""
    assert extract_notice_id(loc) is None


def test_due_concorso_id_diversi_nello_stesso_link_sono_ambigui():
    """Scegliere il primo significherebbe indovinare a quale bando punta."""
    loc = (f"https://inpa.gov.it/x?concorso_id={ID_PRIMA_VOCE}"
           "&concorso_id=01c11fcf96d04a7092d039e5f1a66224")

    assert extract_notice_id(loc) is None


# --------------------------------------------------------------------------
# Conversione in stub
# --------------------------------------------------------------------------


def test_stub_dalla_sitemap_reale_con_host_riscritto(testo_fixture):
    """Le `loc` usano `inpa.gov.it`, le schede si servono su `www`.

    La riscrittura evita un redirect per ogni bando, ma resta verificabile:
    il `loc` originale rimane nel `locator` della provenienza.
    """
    entries = parse_urlset(testo_fixture("sitemap-bandi.xml"))

    stubs, anomalies = entries_to_stubs(
        entries, source_url=URL_SITEMAP_BANDI, observed_at=OSSERVATO_IL
    )

    assert anomalies == ()
    assert len(stubs) == 12
    primo = stubs[0]
    assert primo.notice_id == ID_PRIMA_VOCE
    assert primo.source_url.startswith("https://www.inpa.gov.it/bandi-e-avvisi/")
    assert primo.provenance.source_url == URL_SITEMAP_BANDI
    assert primo.provenance.locator.startswith("https://inpa.gov.it/")
    assert primo.provenance.note is not None and "riscritto" in primo.provenance.note
    assert primo.provenance.observed_at == OSSERVATO_IL
    # La sitemap non pubblica titoli: uno stub che ne avesse uno lo avrebbe
    # inventato.
    assert primo.title is None


def test_lastmod_diventa_un_istante_utc(testo_fixture):
    """Il `lastmod` dichiara il proprio offset: nessuna assunzione di fuso."""
    entries = parse_urlset(testo_fixture("sitemap-bandi.xml"))

    stubs, _ = entries_to_stubs(entries, source_url=URL_SITEMAP_BANDI,
                                observed_at=OSSERVATO_IL)

    momento = stubs[0].source_last_modified
    assert momento is not None
    assert momento.instant == datetime(2026, 9, 18, 21, 59, tzinfo=timezone.utc)
    assert momento.tz_assumption is None


def test_loc_senza_id_produce_un_anomalia_e_viene_saltata():
    """Una scoperta che ha saltato delle voci non e' una scoperta riuscita."""
    voci = [_voce(f"https://inpa.gov.it/x?concorso_id={ID_PRIMA_VOCE}"),
            _voce("https://inpa.gov.it/bandi-e-avvisi/")]

    stubs, anomalies = entries_to_stubs(voci, source_url=URL_SITEMAP_BANDI,
                                         observed_at=OSSERVATO_IL)

    assert len(stubs) == 1
    assert [a.code for a in anomalies] == ["sitemap_loc_senza_id"]
    assert anomalies[0].context == "https://inpa.gov.it/bandi-e-avvisi/"


def test_id_duplicato_viene_segnalato_tenendo_la_prima_occorrenza():
    """Un duplicato non e' un bando in piu': va notato, non contato due volte."""
    loc = f"https://inpa.gov.it/x?concorso_id={ID_PRIMA_VOCE}"

    stubs, anomalies = entries_to_stubs([_voce(loc), _voce(loc)],
                                         source_url=URL_SITEMAP_BANDI,
                                         observed_at=OSSERVATO_IL)

    assert len(stubs) == 1
    assert [a.code for a in anomalies] == ["sitemap_id_duplicato"]


def test_host_estraneo_non_viene_spostato_su_quello_canonico():
    """Spostarlo fabbricherebbe un URL che la fonte non ha mai pubblicato."""
    loc = f"https://esempio.test/x?concorso_id={ID_PRIMA_VOCE}"

    stubs, _ = entries_to_stubs([_voce(loc)], source_url=URL_SITEMAP_BANDI,
                                 observed_at=OSSERVATO_IL)

    assert stubs[0].source_url == loc
    assert stubs[0].provenance.note is None


def test_voce_senza_lastmod_non_riceve_una_data_inventata():
    """Manca la data: resta assente, non diventa l'istante dell'osservazione."""
    loc = f"https://inpa.gov.it/x?concorso_id={ID_PRIMA_VOCE}"

    stubs, _ = entries_to_stubs([_voce(loc, lastmod=None)],
                                 source_url=URL_SITEMAP_BANDI,
                                 observed_at=OSSERVATO_IL)

    assert stubs[0].source_last_modified is None
