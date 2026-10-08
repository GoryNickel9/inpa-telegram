"""Versioni, confronto fra acquisizioni e archivio (SPEC 4.2, 5.2).

I casi `test_m0_06_*` sono il test di accettazione M0-06: due acquisizioni
degli stessi ID con confronto deterministico, piu' le tre modifiche
controllate (proroga, aggiunta di un allegato, sostituzione di contenuto).

Le tre modifiche sono applicate alla copia locale di una scheda reale e non
a un oggetto costruito a mano: cosi' il confronto attraversa anche il
parser, ed e' la stessa catena che girera' in produzione. La specifica
(3.4) chiede di etichettare questi casi come simulati, e lo sono: nelle 48
ore di osservazione non e' avvenuta nessuna rettifica reale.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from inpa_bot.source.detail import parse_detail
from inpa_bot.source.models import (
    Anomaly,
    AnomalySeverity,
    AttachmentRef,
    Certainty,
    NoticePosition,
    NoticeSnapshot,
    NoticeStatus,
    Provenance,
    SourceDateTime,
    SyncRun,
    TerritoryScope,
    TerritoryValue,
)
from inpa_bot.source.snapshot import (
    SCHEMA_VERSION,
    SnapshotStore,
    diff_snapshots,
    snapshot_from_json,
    snapshot_to_json,
)

from .conftest import OSSERVATO_IL, SCHEDA_LOCALE, leggi_scheda

SCADENZA_ORIGINALE = "05 Settembre 2026  23:59"
SCADENZA_PROROGATA = "20 Ottobre 2026  12:00"

# Le stesse date come arrivano in `SourceDateTime.raw`: il valore di
# un'etichetta passa da `clean_text`, che riduce a uno i due spazi della
# scheda. Il testo resta quello pubblicato, solo con gli spazi normalizzati.
SCADENZA_ORIGINALE_RAW = "05 Settembre 2026 23:59"
SCADENZA_PROROGATA_RAW = "20 Ottobre 2026 12:00"

CODA_ULTIMO_ALLEGATO = (
    "Allegato A bando Operatore_.docx (Pubblicato il 19 Agosto 2026  12:53)</a></p>"
)
ALLEGATO_AGGIUNTO = (
    '<p class="concorso-content font-lora">'
    '<a href="https://portale.inpa.gov.it/api/media/00000000-0000-4000-8000-000000000001" '
    'title="Rettifica.pdf" class="text-break">'
    "Rettifica.pdf (Pubblicato il 10 Settembre 2026  09:00)</a></p>"
)
URL_ALLEGATO_AGGIUNTO = (
    "https://portale.inpa.gov.it/api/media/00000000-0000-4000-8000-000000000001"
)

# Frammento del commento lasciato in coda dalla cache del tema: cambia a ogni
# richiesta pur non dicendo nulla sul bando. E' il caso reale che SPEC 4.2
# chiede di non far passare per un aggiornamento.
IMPRONTA_DELLA_CACHE = "2026-09-19 19:56:39"


def _leggi(html: str, *, quando: datetime = OSSERVATO_IL) -> NoticeSnapshot:
    return parse_detail(
        html, notice_id=SCHEDA_LOCALE,
        source_url=("https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/"
                    f"?concorso_id={SCHEDA_LOCALE}"),
        observed_at=quando,
    )


def _modifica(originale: str, prima: str, dopo: str) -> str:
    """Applica una modifica controllata verificando che abbia avuto effetto.

    Senza questo controllo un fixture riscritto renderebbe i test di M0-06
    verdi pur confrontando due documenti identici.
    """
    modificato = originale.replace(prima, dopo, 1)
    assert modificato != originale, f"frammento non trovato nel campione: {prima!r}"
    return modificato


@pytest.fixture
def html_originale() -> str:
    return leggi_scheda(SCHEDA_LOCALE)


@pytest.fixture
def prima(html_originale: str) -> NoticeSnapshot:
    return _leggi(html_originale)


# --------------------------------------------------------------------------
# M0-06: confronto fra due acquisizioni
# --------------------------------------------------------------------------


def test_m0_06_prima_osservazione_e_un_record_nuovo(prima):
    """Mai visto prima: nessun campo "modificato", ma gli allegati si elencano.

    Chi reagisce ad `attachments_added` per scaricarli non deve perdere
    quelli della prima osservazione.
    """
    differenza = diff_snapshots(None, prima)

    assert differenza.is_new is True
    assert differenza.changes == ()
    assert len(differenza.attachments_added) == 2
    assert differenza.significant is True


def test_m0_06_seconda_acquisizione_identica_non_produce_aggiornamenti(
        html_originale, prima):
    """Stessa scheda riletta un'ora dopo: nessun cambiamento significativo.

    L'istante dell'osservazione e' diverso di proposito: e' una data del
    bot, non della fonte, e da solo non deve muovere nulla (SPEC 4.2).
    """
    dopo = _leggi(html_originale, quando=OSSERVATO_IL + timedelta(hours=1))

    differenza = diff_snapshots(prima, dopo)

    assert differenza.is_new is False
    assert differenza.metadata_changed is False
    assert differenza.document_changed is False
    assert differenza.changes == ()
    assert differenza.attachments_added == ()
    assert differenza.significant is False


def test_m0_06_proroga_della_scadenza(html_originale, prima):
    """Caso controllato: la data di chiusura si sposta in avanti.

    La differenza deve nominare il campo giusto e mostrare entrambi i
    valori: un "qualcosa e' cambiato" non permetterebbe di riprogrammare i
    promemoria (SPEC 9.1).
    """
    dopo = _leggi(_modifica(html_originale, SCADENZA_ORIGINALE, SCADENZA_PROROGATA))

    differenza = diff_snapshots(prima, dopo)

    assert differenza.metadata_changed is True
    assert differenza.significant is True
    assert differenza.attachments_added == ()

    per_campo = {cambio.field: cambio for cambio in differenza.changes}
    assert set(per_campo) == {"closing_at", "closing_at.raw"}
    assert per_campo["closing_at"].before.startswith("2026-09-05")
    assert per_campo["closing_at"].after.startswith("2026-10-20")
    # Il testo pubblicato viaggia su un piano separato dall'istante: una
    # riscrittura del formato non e' una proroga e va distinta.
    assert per_campo["closing_at.raw"].before == SCADENZA_ORIGINALE_RAW
    assert per_campo["closing_at.raw"].after == SCADENZA_PROROGATA_RAW


def test_m0_06_aggiunta_di_un_allegato(html_originale, prima):
    """Caso controllato: la scheda pubblica un documento in piu'."""
    dopo = _leggi(_modifica(html_originale, CODA_ULTIMO_ALLEGATO,
                            CODA_ULTIMO_ALLEGATO + ALLEGATO_AGGIUNTO))

    differenza = diff_snapshots(prima, dopo)

    assert differenza.attachments_added == (URL_ALLEGATO_AGGIUNTO,)
    assert differenza.attachments_removed == ()
    assert differenza.attachments_changed == ()
    assert differenza.significant is True
    # Le date del bando non si sono mosse: un allegato in piu' non e' una
    # proroga, e chi legge la differenza deve poterlo vedere.
    assert not any(cambio.field.startswith("closing_at")
                   for cambio in differenza.changes)


def test_m0_06_sostituzione_di_contenuto_non_significativa(html_originale, prima):
    """Caso controllato: cambia il documento, non cambia il bando.

    E' il falso aggiornamento che SPEC 4.2 chiede di evitare: il commento
    della cache del tema si muove a ogni richiesta, e senza i due hash
    separati ogni giro sembrerebbe una modifica.
    """
    dopo = _leggi(_modifica(html_originale, IMPRONTA_DELLA_CACHE,
                            "2026-09-20 06:01:02"))

    differenza = diff_snapshots(prima, dopo)

    assert prima.raw_document_hash != dopo.raw_document_hash
    assert prima.metadata_hash == dopo.metadata_hash
    assert differenza.document_changed is True
    assert differenza.metadata_changed is False
    assert differenza.changes == ()
    assert differenza.significant is False


def test_un_allegato_sostituito_dietro_lo_stesso_indirizzo_viene_notato(
        html_originale, prima):
    """Stesso URL, nome diverso: non e' un allegato nuovo, ma non e' nulla."""
    dopo = _leggi(_modifica(html_originale, 'title="Allegato A bando Operatore_.docx"',
                            'title="Allegato A bando Operatore_v2.docx"'))

    differenza = diff_snapshots(prima, dopo)

    assert differenza.attachments_added == ()
    assert differenza.attachments_removed == ()
    assert len(differenza.attachments_changed) == 1


def test_confrontare_due_bandi_diversi_e_un_errore(prima):
    """L'identita' e' l'ID della fonte: non si deduplica sul titolo."""
    with pytest.raises(ValueError):
        diff_snapshots(prima, replace(prima, notice_id="0" * 32))


def test_senza_entrambi_gli_hash_non_si_afferma_nulla_sul_documento(prima):
    """Un dato assente resta assente: non diventa "documento invariato"."""
    differenza = diff_snapshots(prima, replace(prima, raw_document_hash=None))

    assert differenza.document_changed is False
    assert differenza.significant is False


def test_il_movimento_del_solo_hash_dei_metadati_viene_comunque_registrato(prima):
    """Rete di sicurezza: l'hash copre anche campi che il confronto non elenca."""
    differenza = diff_snapshots(prima, replace(prima, metadata_hash="cambiato"))

    assert differenza.metadata_changed is True
    assert [c.field for c in differenza.changes] == ["metadata_hash"]


# --------------------------------------------------------------------------
# Serializzazione
# --------------------------------------------------------------------------


def _snapshot_completo() -> NoticeSnapshot:
    """Uno snapshot che usa ogni tipo del contratto, per il round-trip."""
    provenienza = Provenance(
        source_url="https://www.inpa.gov.it/scheda", observed_at=OSSERVATO_IL,
        locator="scheda pubblica di dettaglio", certainty=Certainty.OBSERVED,
        note="campione completo",
    )
    scadenza = SourceDateTime(
        raw="05 Settembre 2026  23:59", date_only=date(2026, 9, 5),
        instant=datetime(2026, 9, 5, 21, 59, tzinfo=timezone.utc),
        tz_assumption="Europe/Rome", certainty=Certainty.OBSERVED,
    )
    territorio = TerritoryValue(
        raw="Calabria", scope=TerritoryScope.UNSPECIFIED, provenance=provenienza,
        region_code="IT-78", region_name="Calabria", certainty=Certainty.DERIVED,
    )
    allegato = AttachmentRef(
        notice_id=SCHEDA_LOCALE, url="https://portale.inpa.gov.it/api/media/uno",
        provenance=provenienza, filename="Bando.pdf", published=scadenza,
        declared_extension="pdf", fetch_permitted=False,
        fetch_block_reason="[*] Disallow: /",
    )
    return NoticeSnapshot(
        notice_id=SCHEDA_LOCALE, source_url="https://www.inpa.gov.it/scheda",
        observed_at=OSSERVATO_IL, status_original="Chiuso",
        status_normalized=NoticeStatus.CLOSED, title="Titolo", description="Testo",
        enti=("Conservatorio Musica Vibo Valentia",),
        procedure_type="Per titoli ed esami", total_positions=1,
        opening_at=scadenza, closing_at=scadenza, published_at=None,
        source_updated_at=scadenza, territories=(territorio,),
        positions=(NoticePosition(code="A", title="Operatore", positions=1,
                                  territories=(territorio,),
                                  provenance=provenienza),),
        attachments=(allegato,), pa_link="https://consvv.it/",
        extra_fields={"Corrispettivo minimo annuo:": "23.139€"},
        metadata_hash="m", raw_document_hash="d",
        anomalies=(Anomaly(code="x", message="y", severity=AnomalySeverity.INFO,
                           context="z"),),
        provenance=provenienza,
    )


def test_round_trip_json_restituisce_lo_stesso_oggetto():
    """Enum, tuple, dizionari e istanti con fuso devono tornare com'erano.

    Senza le buste di tipo un Enum tornerebbe stringa e una tupla lista:
    l'oggetto somiglierebbe all'originale ma non sarebbe lo stesso.
    """
    originale = _snapshot_completo()

    ricostruito = snapshot_from_json(snapshot_to_json(originale))

    assert ricostruito == originale
    assert ricostruito.status_normalized is NoticeStatus.CLOSED
    assert isinstance(ricostruito.attachments, tuple)
    assert ricostruito.closing_at.instant.tzinfo is not None
    assert ricostruito.extra_fields == originale.extra_fields


def test_la_serializzazione_e_ripetibile_byte_per_byte():
    """Due serializzazioni dello stesso snapshot danno lo stesso testo."""
    originale = _snapshot_completo()
    payload = snapshot_to_json(originale)

    assert snapshot_to_json(snapshot_from_json(payload)) == payload


def test_round_trip_di_una_scheda_reale(prima):
    """Il caso che conta davvero: cio' che il parser produce sul campo."""
    assert snapshot_from_json(snapshot_to_json(prima)) == prima


def test_un_payload_di_uno_schema_diverso_non_viene_letto_a_meta():
    """Un campo sconosciuto e' il segnale di uno schema diverso, non rumore."""
    payload = snapshot_to_json(_snapshot_completo())

    with pytest.raises(ValueError):
        snapshot_from_json(payload.replace('"notice_id"', '"id_del_bando"', 1))


# --------------------------------------------------------------------------
# Archivio
# --------------------------------------------------------------------------


@pytest.fixture
def archivio():
    with SnapshotStore() as store:
        yield store


def _conta(store: SnapshotStore, tabella: str) -> int:
    return store._connection().execute(  # noqa: SLF001 - verifica dello schema
        f"SELECT COUNT(*) FROM {tabella}").fetchone()[0]


def test_una_nuova_versione_nasce_solo_se_qualcosa_e_cambiato(
        archivio, html_originale, prima):
    """"Rivisto identico" e' informazione diversa da "non piu' visto"."""
    assert archivio.record(prima).is_new is True
    assert _conta(archivio, "notice_versions") == 1

    ripetuto = archivio.record(_leggi(html_originale,
                                      quando=OSSERVATO_IL + timedelta(hours=1)))

    assert ripetuto.significant is False
    assert _conta(archivio, "notice_versions") == 1


def test_una_proroga_archivia_una_seconda_versione(archivio, html_originale, prima):
    """Le versioni servono a raccontare cosa e' cambiato e quando."""
    archivio.record(prima)
    prorogato = _leggi(_modifica(html_originale, SCADENZA_ORIGINALE,
                                 SCADENZA_PROROGATA))

    differenza = archivio.record(prorogato)

    assert differenza.metadata_changed is True
    assert _conta(archivio, "notice_versions") == 2
    assert archivio.latest(SCHEDA_LOCALE).closing_at.raw == SCADENZA_PROROGATA_RAW


def test_un_allegato_sparito_resta_in_archivio(archivio, html_originale, prima):
    """Un'assenza non e' una cancellazione (SPEC 4.2)."""
    archivio.record(_leggi(_modifica(html_originale, CODA_ULTIMO_ALLEGATO,
                                     CODA_ULTIMO_ALLEGATO + ALLEGATO_AGGIUNTO)))

    differenza = archivio.record(prima)

    assert differenza.attachments_removed == (URL_ALLEGATO_AGGIUNTO,)
    assert _conta(archivio, "attachments_seen") == 3


def test_un_id_visto_in_un_elenco_non_ha_ancora_una_scheda(archivio):
    """`mark_seen` registra l'osservazione senza inventare un URL."""
    archivio.mark_seen(["a" * 32], OSSERVATO_IL)

    assert archivio.latest("a" * 32) is None
    riga = archivio._connection().execute(  # noqa: SLF001
        "SELECT source_url FROM notices").fetchone()
    assert riga["source_url"] is None


def test_gli_id_non_piu_visti_sono_materiale_per_una_riconciliazione(archivio, prima):
    """Non un verdetto: la sitemap copre i bandi attivi, non lo storico."""
    archivio.record(prima)
    archivio.mark_seen(["b" * 32], OSSERVATO_IL + timedelta(days=1))

    assert archivio.not_seen_since(OSSERVATO_IL + timedelta(hours=1)) == (
        SCHEDA_LOCALE,
    )


def test_una_acquisizione_incompleta_viene_registrata_come_tale(archivio):
    """`complete` e' la condizione che decide se il cursore puo' avanzare."""
    incompleta = SyncRun(
        started_at=OSSERVATO_IL, method="sitemap", finished_at=OSSERVATO_IL,
        outcome="ok", pages_expected=3, pages_read=1,
    )

    run_id = archivio.record_sync_run(incompleta)

    riga = archivio._connection().execute(  # noqa: SLF001
        "SELECT complete FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert riga["complete"] == 0


def test_uno_schema_di_versione_diversa_ferma_l_apertura(tmp_path):
    """Meglio fermarsi che scrivere in uno schema che non e' quello atteso."""
    percorso = tmp_path / "archivio.sqlite3"
    SnapshotStore(percorso).close()

    with sqlite3.connect(percorso) as conn:
        conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                     (str(SCHEMA_VERSION + 1),))

    with pytest.raises(RuntimeError):
        SnapshotStore(percorso)
