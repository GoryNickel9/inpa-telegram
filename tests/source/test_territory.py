"""Anagrafica territoriale e filtro geografico (SPEC 6).

Un errore qui non si manifesta come eccezione: si manifesta come un bando
dell'altra parte del paese recapitato a un utente che lo prende per buono.
Per questo i test insistono su due comportamenti scomodi ma corretti: il
riconoscimento solo per valore intero e il rifiuto di dedurre una provincia
da una regione.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from inpa_bot.source.models import Certainty, Provenance, TerritoryScope
from inpa_bot.source.territory import (
    ANAGRAFICA_VERSION,
    find_province,
    find_region,
    matches_filter,
    normalize_key,
    parse_area_geografica,
)

AREA_MULTIREGIONALE = (
    "Lazio, Campania, Liguria, Puglia, Sardegna, Toscana, Veneto, "
    "Trentino Alto Adige, Marche, Umbria, Emilia Romagna, "
    "Friuli Venezia Giulia, Piemonte, Lombardia"
)
"""Il valore reale della scheda con 1100 posti: 14 regioni separate da ", "."""

PROVENIENZA = Provenance(
    source_url="https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/",
    observed_at=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
    locator="Area geografica:",
)


def _territori(area: str):
    return parse_area_geografica(
        area, scope=TerritoryScope.UNSPECIFIED, provenance=PROVENIENZA
    )


# --------------------------------------------------------------------------
# Riconoscimento
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scritto_dalla_fonte", "codice"),
    [
        ("Emilia Romagna", "IT-45"),
        ("Trentino Alto Adige", "IT-32"),
        ("Friuli Venezia Giulia", "IT-36"),
        ("Valle d'Aosta", "IT-23"),
    ],
)
def test_le_forme_senza_trattino_della_fonte_sono_riconosciute(scritto_dalla_fonte,
                                                               codice):
    """La fonte scrive "Emilia Romagna" dove l'ISO scrive "Emilia-Romagna".

    Riconoscerle non significa sostituirle: il nome ufficiale arriva
    dall'anagrafica, il testo pubblicato resta nel `TerritoryValue`.
    """
    regione = find_region(scritto_dalla_fonte)

    assert regione is not None
    assert regione.code == codice


def test_il_riconoscimento_non_avviene_per_sottostringa():
    """"Regione Lombardia e Provincia di Como" non e' la Lombardia.

    Indovinarlo produrrebbe un dato falso indistinguibile da uno vero, che
    e' esattamente il modo in cui SPEC 6 vieta di risolvere un territorio.
    """
    assert find_region("Regione Lombardia e Provincia di Como") is None
    assert find_province("Selezione per la PA di Milano") is None


def test_la_sigla_provinciale_vale_solo_come_valore_intero():
    """"SS" e' Sassari; "PA" dentro un titolo non e' Palermo."""
    assert find_province("SS").code == "SS"
    assert find_province("Sassari").code == "SS"
    assert find_province("posti presso la PA") is None


def test_normalize_key_appiattisce_punteggiatura_e_accenti():
    """Serve a evitare un alias per ogni variante di trattino e apostrofo."""
    assert normalize_key("Emilia-Romagna") == normalize_key("Emilia Romagna")
    assert normalize_key("Valle d’Aosta") == normalize_key("Valle d'Aosta")


def test_anagrafica_dichiara_la_propria_data():
    """I dati territoriali sono versionati: un riordino deve risultare scaduto."""
    assert ANAGRAFICA_VERSION.startswith("2026-")


# --------------------------------------------------------------------------
# Lettura del campo "Area geografica"
# --------------------------------------------------------------------------


def test_area_multiregionale_reale_produce_quattordici_regioni():
    """Il campo e' un elenco separato da ", ": il campione reale ne ha 14."""
    territori = _territori(AREA_MULTIREGIONALE)

    assert len(territori) == 14
    assert all(t.region_code is not None for t in territori)
    assert all(t.certainty is Certainty.DERIVED for t in territori)
    # Nessuna provincia: la fonte ha pubblicato regioni, e dedurre una
    # provincia da una regione e' vietato (SPEC 6).
    assert all(t.province_code is None for t in territori)
    assert territori[0].scope is TerritoryScope.UNSPECIFIED


def test_valore_ignoto_resta_ambiguo_senza_codici():
    """Meglio un "territorio da verificare" che una regione plausibile."""
    (territorio,) = _territori("Contea di Atlantide")

    assert territorio.raw == "Contea di Atlantide"
    assert territorio.certainty is Certainty.AMBIGUOUS
    assert territorio.region_code is None and territorio.province_code is None
    assert territorio.normalized is False


def test_nazionale_e_una_categoria_a_se_non_una_regione():
    """Un bando nazionale non e' "di quella regione" ne' un dato mancante."""
    (territorio,) = _territori("Nazionale")

    assert territorio.national is True
    assert territorio.region_code is None
    assert territorio.certainty is Certainty.OBSERVED


def test_una_provincia_porta_con_se_la_propria_regione():
    """Sassari e' in Sardegna: qui non si deduce nulla, si risale."""
    (territorio,) = _territori("Sassari")

    assert territorio.province_code == "SS"
    assert territorio.region_code == "IT-88"
    assert territorio.certainty is Certainty.DERIVED


def test_la_provenienza_viaggia_con_ogni_territorio():
    """Ogni valore porta con se' da dove viene (SPEC 4.1)."""
    territori = _territori("Lazio, Atlantide")

    assert all(t.provenance is PROVENIENZA for t in territori)


# --------------------------------------------------------------------------
# Filtro utente
# --------------------------------------------------------------------------


def test_una_regione_nota_non_soddisfa_la_richiesta_di_una_provincia():
    """Sapere "Sardegna" non dice che i posti siano a Sassari.

    E' il comportamento conservativo di SPEC 6: i posti di un bando
    regionale non vanno assegnati indistintamente a una provincia.
    """
    territori = _territori("Sardegna")

    assert matches_filter(territori, province_codes=["SS"],
                          include_unresolved=False, include_national=False) is False


def test_una_provincia_nota_soddisfa_la_richiesta_della_sua_regione():
    """Sassari e' in Sardegna in ogni caso: qui la deduzione e' lecita."""
    territori = _territori("Sassari")

    assert matches_filter(territori, region_codes=["IT-88"],
                          include_national=False) is True


def test_un_territorio_irrisolto_passa_solo_se_richiesto():
    """L'interruttore esiste perche' la scelta spetta all'utente (SPEC 6)."""
    territori = _territori("Contea di Atlantide")

    assert matches_filter(territori, region_codes=["IT-62"],
                          include_unresolved=True, include_national=False) is True
    assert matches_filter(territori, region_codes=["IT-62"],
                          include_unresolved=False, include_national=False) is False


def test_assenza_di_territori_e_trattata_come_territorio_irrisolto():
    """Zero elementi e un elemento non riconosciuto sono la stessa ignoranza."""
    assert matches_filter((), region_codes=["IT-62"],
                          include_unresolved=True) is True
    assert matches_filter((), region_codes=["IT-62"],
                          include_unresolved=False) is False


def test_i_bandi_nazionali_hanno_un_interruttore_separato():
    """Ambito noto e ampio: cosa diversa da un ambito ignoto."""
    territori = _territori("Nazionale")

    assert matches_filter(territori, region_codes=["IT-62"],
                          include_national=True) is True
    assert matches_filter(territori, region_codes=["IT-62"],
                          include_national=False, include_unresolved=False) is False


def test_senza_filtri_passa_tutto():
    """Nessuna preferenza geografica non significa nessun risultato."""
    assert matches_filter(_territori("Sardegna")) is True
    assert matches_filter(()) is True


def test_il_bando_multiregionale_passa_per_una_qualunque_delle_sue_regioni():
    """Il caso reale: 14 regioni, un utente che ne ha scelta una."""
    territori = _territori(AREA_MULTIREGIONALE)

    assert matches_filter(territori, region_codes=["IT-88"],
                          include_national=False) is True
    # Il Molise non e' fra le 14: il bando non deve comparire.
    assert matches_filter(territori, region_codes=["IT-67"],
                          include_national=False, include_unresolved=False) is False
