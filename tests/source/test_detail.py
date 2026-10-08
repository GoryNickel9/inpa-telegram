"""Lettura della scheda pubblica sui sei campioni reali (SPEC 2.2, 4.1).

I valori attesi non sono stati scelti: sono quelli stampati sulle schede
scaricate il 19/09/2026. Dove il parser potrebbe "aggiustare" un dato
mancante -- la data di pubblicazione, un'estensione dedotta dall'URL, una
pagina che non e' una scheda -- il test verifica che non lo faccia.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from inpa_bot.source import detail as detail_mod
from inpa_bot.source.detail import (
    UNEVALUATED_PERMISSION,
    build_detail_url,
    parse_detail,
)
from inpa_bot.source.errors import ParseError
from inpa_bot.source.models import AnomalySeverity, Certainty, NoticeStatus

from .conftest import (
    OSSERVATO_IL,
    SCHEDA_ETICHETTA_RARA,
    SCHEDA_LOCALE,
    SCHEDA_MULTIREGIONALE,
    SCHEDA_RTF,
    SCHEDA_SENZA_ALLEGATI,
    SCHEDA_TRE_ALLEGATI,
    TUTTE_LE_SCHEDE,
)


def _campo(etichetta: str, valore: str) -> str:
    """Un'etichetta con il suo blocco valore, nella forma osservata."""
    return (f'<p class="mb-2"><span class="concorso-label fw-bold">{etichetta} </span>'
            f'<span class="concorso-content font-lora">{valore}</span></p>')


def _pagina(corpo: str, *, titolo: str = "<h1>Titolo del bando</h1>") -> str:
    return f"<html><body>{titolo}<div class='row'>{corpo}</div></body></html>"


def _leggi(html: str, notice_id: str = SCHEDA_LOCALE, **kwargs):
    return parse_detail(html, notice_id=notice_id,
                        source_url="https://www.inpa.gov.it/scheda",
                        observed_at=OSSERVATO_IL, **kwargs)


# --------------------------------------------------------------------------
# I sei campioni reali
# --------------------------------------------------------------------------


def test_scheda_multiregionale_con_millecento_posti(scheda):
    """Il campione con 14 regioni, 1100 posti, chiuso e due allegati."""
    snapshot = scheda(SCHEDA_MULTIREGIONALE)

    assert len(snapshot.territories) == 14
    assert {t.region_code for t in snapshot.territories} >= {"IT-88", "IT-32", "IT-45"}
    assert snapshot.total_positions == 1100
    assert snapshot.status_normalized is NoticeStatus.CLOSED
    assert snapshot.status_original == "Chiuso"
    assert len(snapshot.attachments) == 2
    assert snapshot.enti == ("Ministero della Difesa",)
    assert snapshot.procedure_type == "Per titoli ed esami"
    assert snapshot.pa_link == "https://www.difesa.it"
    assert snapshot.closing_at is not None
    assert snapshot.closing_at.date_only == date(2026, 1, 27)
    # I posti sono di tutto il bando: nessuna ripartizione per profilo viene
    # dedotta dalla prosa (SPEC 9.1).
    assert snapshot.positions == ()


def test_scheda_locale_calabrese_con_pdf_e_docx(scheda):
    """Un posto, una regione, due allegati di cui uno .docx."""
    snapshot = scheda(SCHEDA_LOCALE)

    assert [t.region_name for t in snapshot.territories] == ["Calabria"]
    assert snapshot.total_positions == 1
    assert snapshot.status_normalized is NoticeStatus.CLOSED
    assert len(snapshot.attachments) == 2
    assert [a.declared_extension for a in snapshot.attachments] == ["pdf", "docx"]
    assert snapshot.enti == ("Conservatorio Musica Vibo Valentia",)


def test_scheda_senza_allegati_ne_link_alla_pa(scheda):
    """Allegati e link alla PA sono opzionali: la loro assenza non e' un errore.

    Mancavano rispettivamente in 2 e in 12 schede su 26, quindi non
    producono anomalie di campo atteso.
    """
    snapshot = scheda(SCHEDA_SENZA_ALLEGATI)

    assert snapshot.attachments == ()
    assert snapshot.pa_link is None
    assert snapshot.status_normalized is NoticeStatus.OPEN
    assert [a.code for a in snapshot.anomalies] == []


def test_scheda_con_tre_allegati_di_cui_uno_firmato_p7m(scheda):
    """L'estensione .p7m viene letta com'e', non tradotta in .pdf."""
    snapshot = scheda(SCHEDA_TRE_ALLEGATI)

    assert len(snapshot.attachments) == 3
    assert [a.declared_extension for a in snapshot.attachments] == ["p7m", "pdf", "pdf"]
    assert snapshot.attachments[0].filename == "Bando di Concorso .pdf.p7m"
    assert snapshot.pa_link is None


def test_scheda_con_allegato_rtf(scheda):
    """Il terzo formato osservato nei nomi file, dopo .pdf e .p7m."""
    snapshot = scheda(SCHEDA_RTF)

    estensioni = [a.declared_extension for a in snapshot.attachments]
    assert "rtf" in estensioni
    assert snapshot.total_positions == 1


def test_etichetta_rara_finisce_in_extra_fields_con_una_nota(scheda):
    """"Corrispettivo minimo annuo:" compariva su 1 scheda su 26.

    Conservarla e' cio' che permette di accorgersi che la fonte ha aggiunto
    un campo, invece di perderlo in silenzio.
    """
    snapshot = scheda(SCHEDA_ETICHETTA_RARA)

    assert snapshot.extra_fields == {"Corrispettivo minimo annuo:": "23.139€"}
    non_mappate = [a for a in snapshot.anomalies if a.code == "etichetta_non_mappata"]
    assert len(non_mappate) == 1
    assert non_mappate[0].severity is AnomalySeverity.INFO


@pytest.mark.parametrize("notice_id", TUTTE_LE_SCHEDE)
def test_ogni_campione_produce_uno_snapshot_utilizzabile(notice_id, scheda):
    """Invarianti comuni alle sei schede: identita', titolo, hash, date.

    Nessuna delle sei deve produrre un'anomalia bloccante: sono schede
    valide, e se una lo diventasse significherebbe che il parser ha smesso
    di leggerle.
    """
    snapshot = scheda(notice_id)

    assert snapshot.notice_id == notice_id
    assert snapshot.title
    assert snapshot.description
    assert snapshot.metadata_hash and snapshot.raw_document_hash
    assert snapshot.observed_at == OSSERVATO_IL
    assert snapshot.has_blocking_anomaly is False
    assert snapshot.opening_at is not None and snapshot.opening_at.instant is not None
    assert snapshot.closing_at is not None and snapshot.closing_at.instant is not None


@pytest.mark.parametrize("notice_id", TUTTE_LE_SCHEDE)
def test_published_at_resta_assente_e_non_copia_l_apertura(notice_id, scheda):
    """"Data apertura candidature" non e' una data di pubblicazione.

    Nel JS della fonte quell'etichetta e' alimentata da `dataPubblicazione`:
    stesso dato, due significati. Copiarlo sarebbe la confusione vietata da
    SPEC 4.1.
    """
    snapshot = scheda(notice_id)

    assert snapshot.published_at is None
    assert snapshot.opening_at is not None


def test_le_date_della_scheda_sono_lette_come_orari_italiani(scheda):
    """"06 Agosto 2026  14:00" -> istante +02:00, testo originale conservato."""
    snapshot = scheda(SCHEDA_LOCALE)

    apertura = snapshot.opening_at
    assert apertura is not None and apertura.instant is not None
    assert apertura.instant.utcoffset() == timedelta(hours=2)
    assert apertura.tz_assumption == "Europe/Rome"
    assert apertura.certainty is Certainty.OBSERVED
    assert "Agosto" in apertura.raw


def test_la_data_chiusa_da_un_tag_sbagliato_viene_comunque_letta(scheda):
    """Nel campione il `<p>` di "Data chiusura candidature" e' chiuso da `</div>`.

    E' la ragione per cui il parser legge una sequenza di token invece di
    costruire un albero: con un albero rigido questo valore andrebbe perso o
    finirebbe attaccato al nodo sbagliato.
    """
    snapshot = scheda(SCHEDA_LOCALE)

    assert snapshot.closing_at is not None
    assert snapshot.closing_at.date_only == date(2026, 9, 5)
    # Il campo successivo non e' stato inghiottito dal blocco mal chiuso.
    assert snapshot.total_positions == 1


# --------------------------------------------------------------------------
# Allegati
# --------------------------------------------------------------------------


def test_estensione_dedotta_dal_nome_del_file_e_non_dall_URL(scheda):
    """`/api/media/<uuid>` non termina in `.pdf` pur servendo un PDF (SPEC 2.3)."""
    snapshot = scheda(SCHEDA_LOCALE)

    for allegato in snapshot.attachments:
        assert "/api/media/" in allegato.url
        assert "." not in allegato.url.rsplit("/", 1)[-1]
        assert allegato.declared_extension == allegato.filename.rsplit(".", 1)[-1].lower()


def test_estensione_non_inventata_quando_il_nome_non_ne_ha_una():
    """In "Comunicazione n. 3" il testo dopo il punto non e' un formato."""
    html = _pagina(
        _campo("Stato:", "Aperto")
        + '<p class="mb-2 d-inline-block"><span class="concorso-label fw-bold">'
          "Bando/Avviso e Allegati: </span></p>"
        '<p class="concorso-content font-lora">'
        '<a href="https://portale.inpa.gov.it/api/media/uno" '
        'title="Comunicazione n. 3">Comunicazione n. 3</a></p>'
    )

    (allegato,) = _leggi(html).attachments

    assert allegato.filename == "Comunicazione n. 3"
    assert allegato.declared_extension is None


def test_senza_valutazione_l_allegato_resta_non_prelevabile(scheda):
    """Un falso "si'" porta a una richiesta vietata; un falso "no" costa nulla."""
    snapshot = scheda(SCHEDA_LOCALE)

    assert all(a.fetch_permitted is False for a in snapshot.attachments)
    assert all(a.fetch_block_reason == UNEVALUATED_PERMISSION
               for a in snapshot.attachments)


def test_la_valutazione_delle_condizioni_arriva_dal_chiamante(scheda):
    """Il parser non conosce robots.txt: riceve una funzione gia' informata."""
    interrogati: list[str] = []

    def permesso(url: str) -> tuple[bool, str | None]:
        interrogati.append(url)
        return True, None

    snapshot = scheda(SCHEDA_LOCALE, permesso)

    assert len(interrogati) == 2
    assert all(a.fetch_permitted is True for a in snapshot.attachments)
    assert all(a.fetch_block_reason is None for a in snapshot.attachments)


def test_la_data_dell_allegato_viene_dal_testo_del_link(scheda):
    """"NOMEFILE (Pubblicato il 06 Agosto 2026  14:00)": la data e' li' dentro."""
    snapshot = scheda(SCHEDA_LOCALE)

    pubblicazione = snapshot.attachments[0].published
    assert pubblicazione is not None
    assert pubblicazione.date_only == date(2026, 8, 6)
    assert pubblicazione.instant is not None
    # Il nome del file non si porta dietro la coda "(Pubblicato il ...)".
    assert "Pubblicato" not in snapshot.attachments[0].filename


def test_link_senza_href_non_diventa_un_allegato():
    """Senza indirizzo non c'e' nulla da scaricare ne' da registrare."""
    html = _pagina(
        _campo("Stato:", "Aperto")
        + '<p class="mb-2 d-inline-block"><span class="concorso-label fw-bold">'
          "Bando/Avviso e Allegati: </span></p>"
        '<p class="concorso-content font-lora"><a title="Bando.pdf">Bando.pdf</a></p>'
    )

    assert _leggi(html).attachments == ()


# --------------------------------------------------------------------------
# Documenti che non sono una scheda
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "documento",
    [
        "<html><body><h1>Pagina non trovata</h1><p>Torna alla home</p></body></html>",
        "<html><body></body></html>",
        "",
    ],
)
def test_una_pagina_che_non_e_una_scheda_solleva_ParseError(documento):
    """Un bando con tutti i campi vuoti sarebbe il falso "nessun concorso".

    M0-08 chiede esattamente questa distinzione: errore di lettura e assenza
    di risultati non sono la stessa condizione.
    """
    with pytest.raises(ParseError):
        _leggi(documento)


def test_documento_oltre_il_tetto_non_viene_interpretato(monkeypatch):
    """Il limite di caratteri vale prima di ogni analisi (SPEC 11)."""
    monkeypatch.setattr(detail_mod, "MAX_DETAIL_CHARS", 64)

    with pytest.raises(ParseError):
        _leggi(_pagina(_campo("Stato:", "Aperto")))


# --------------------------------------------------------------------------
# Campi mancanti e valori non risolti
# --------------------------------------------------------------------------


def test_i_campi_attesi_mancanti_diventano_anomalie_non_valori_finti():
    """Una scheda incompleta si acquisisce e si marca, non si scarta."""
    snapshot = _leggi(_pagina(_campo("Stato:", "Aperto")))

    mancanti = [a for a in snapshot.anomalies if a.code == "campo_atteso_assente"]
    assert len(mancanti) == 5  # tutte le etichette attese tranne "Stato:"
    assert snapshot.total_positions is None
    assert snapshot.territories == ()
    assert snapshot.enti == ()


def test_uno_stato_ignoto_resta_unknown_con_l_originale():
    """La scheda usa `concorso-status`; un valore nuovo non va forzato."""
    html = _pagina(
        '<p class="mb-0"><span class="concorso-label fw-bold">Stato: </span>'
        '<span class="concorso-status Revocato py-2"><strong>Revocato</strong>'
        "</span></p>"
    )

    snapshot = _leggi(html)

    assert snapshot.status_normalized is NoticeStatus.UNKNOWN
    assert snapshot.status_original == "Revocato"


def test_una_data_illeggibile_produce_un_anomalia_e_conserva_il_testo():
    """L'anomalia accompagna il valore invece di sostituirlo."""
    html = _pagina(
        _campo("Stato:", "Aperto")
        + _campo("Data chiusura candidature:", "prossimamente")
    )

    snapshot = _leggi(html)

    assert snapshot.closing_at is not None
    assert snapshot.closing_at.raw == "prossimamente"
    assert snapshot.closing_at.instant is None
    assert any(a.code == "data_non_interpretabile" for a in snapshot.anomalies)


def test_scheda_senza_titolo_produce_un_anomalia_bloccante():
    """Senza titolo il bando non e' presentabile ne' verificabile."""
    snapshot = _leggi(_pagina(_campo("Stato:", "Aperto"), titolo=""))

    assert snapshot.title is None
    assert snapshot.has_blocking_anomaly is True


# --------------------------------------------------------------------------
# Costruzione dell'URL
# --------------------------------------------------------------------------


def test_url_di_dettaglio_costruito_solo_su_un_id_nella_forma_osservata():
    """Gli id sono stringhe opache della fonte: non si enumerano (SPEC 2.2)."""
    assert build_detail_url(SCHEDA_LOCALE).endswith(f"?concorso_id={SCHEDA_LOCALE}")


@pytest.mark.parametrize(
    "id_non_valido",
    ["", "../../etc/passwd", "df4a037f", f"{SCHEDA_LOCALE}&x=1", "g" * 32],
)
def test_un_id_fuori_forma_non_costruisce_nessun_URL(id_non_valido):
    """Accettarlo lascerebbe costruire una query arbitraria da un valore letto."""
    with pytest.raises(ValueError):
        build_detail_url(id_non_valido)
