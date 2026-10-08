"""Normalizzazione di testo, date, stati, interi e hash (SPEC 4.1, 4.2).

Il filo che tiene insieme questi test e' uno solo: cio' che la fonte non ha
pubblicato non deve comparire nel risultato. E' il requisito piu' facile da
violare senza accorgersene, perche' un valore inventato e' indistinguibile
da uno vero una volta archiviato.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from inpa_bot.source.models import Certainty, NoticeStatus
from inpa_bot.source.normalize import (
    clean_text,
    document_hash,
    metadata_hash,
    normalize_status,
    parse_int,
    parse_iso_datetime,
    parse_italian_datetime,
)


# --------------------------------------------------------------------------
# Date della scheda
# --------------------------------------------------------------------------


def test_data_estiva_della_scheda_diventa_un_istante_con_offset_due_ore():
    """"06 Agosto 2026  14:00" e' un istante italiano, quindi +02:00.

    Il formato con due spazi e' quello osservato su tutte le 52 date
    campionate. L'offset non e' fisso: viene dal database dei fusi, percio'
    ad agosto vale +02:00.
    """
    valore = parse_italian_datetime("06 Agosto 2026  14:00")

    assert valore is not None
    assert valore.date_only == date(2026, 8, 6)
    assert valore.instant is not None
    assert valore.instant.utcoffset() == timedelta(hours=2)
    assert valore.instant.astimezone(timezone.utc) == datetime(
        2026, 8, 6, 12, 0, tzinfo=timezone.utc
    )
    assert valore.tz_assumption == "Europe/Rome"
    assert valore.certainty is Certainty.OBSERVED
    # Il testo originale sopravvive alla normalizzazione: senza di esso non
    # sarebbe piu' verificabile che cosa la fonte avesse davvero scritto.
    assert valore.raw == "06 Agosto 2026  14:00"


def test_data_invernale_usa_offset_di_un_ora():
    """La stessa ora a gennaio vale +01:00: l'ora legale non e' un offset fisso."""
    valore = parse_italian_datetime("06 Gennaio 2026 14:00")

    assert valore is not None and valore.instant is not None
    assert valore.instant.utcoffset() == timedelta(hours=1)


def test_data_senza_orario_non_diventa_le_23_59():
    """Senza orario pubblicato non si calcola nessun istante (SPEC 4.1).

    E' il punto in cui e' piu' comodo inventare la mezzanotte: una scadenza
    dedotta alle 23:59 sposterebbe di ore le notifiche di un promemoria.
    """
    valore = parse_italian_datetime("06 Agosto 2026")

    assert valore is not None
    assert valore.date_only == date(2026, 8, 6)
    assert valore.instant is None
    assert valore.has_explicit_time is False
    assert valore.resolved is True
    assert valore.note is not None and "senza orario" in valore.note


def test_giorno_inesistente_resta_ambiguo_senza_arrotondamenti():
    """"31 Febbraio 2026" non diventa il 28: resta un dato non risolto."""
    valore = parse_italian_datetime("31 Febbraio 2026")

    assert valore is not None
    assert valore.certainty is Certainty.AMBIGUOUS
    assert valore.date_only is None and valore.instant is None
    assert valore.raw == "31 Febbraio 2026"


@pytest.mark.parametrize("testo", ["06 Pippo 2026", "prossimamente", "2026-08-06"])
def test_testo_non_riconosciuto_resta_ambiguo_e_non_solleva(testo):
    """Un formato inatteso non interrompe l'acquisizione della scheda."""
    valore = parse_italian_datetime(testo)

    assert valore is not None
    assert valore.certainty is Certainty.AMBIGUOUS
    assert valore.raw == testo


def test_ora_inesistente_del_cambio_ora_non_viene_inventata():
    """Il 29 marzo 2026 le 02:30 non esistono: nessun istante plausibile."""
    valore = parse_italian_datetime("29 Marzo 2026 02:30")

    assert valore is not None
    assert valore.instant is None
    assert valore.date_only == date(2026, 3, 29)
    assert valore.certainty is Certainty.AMBIGUOUS


def test_ora_ripetuta_del_ritorno_all_ora_solare_e_dichiarata():
    """Il 25 ottobre 2026 le 02:30 esistono due volte: la scelta si dichiara."""
    valore = parse_italian_datetime("25 Ottobre 2026 02:30")

    assert valore is not None
    assert valore.instant is not None
    assert valore.certainty is Certainty.AMBIGUOUS
    assert valore.note is not None and "ripetuto" in valore.note


def test_stringa_vuota_non_e_una_data():
    """Un'etichetta presente con valore vuoto e' un dato assente."""
    assert parse_italian_datetime("   ") is None
    assert parse_italian_datetime(None) is None


# --------------------------------------------------------------------------
# Date ISO della sitemap
# --------------------------------------------------------------------------


def test_lastmod_iso_con_zulu_e_un_istante_utc_senza_assunzioni():
    """Quando la fonte dichiara l'offset non c'e' nessun fuso da assumere."""
    valore = parse_iso_datetime("2026-09-18T21:59:00Z")

    assert valore is not None
    assert valore.instant == datetime(2026, 9, 18, 21, 59, tzinfo=timezone.utc)
    assert valore.tz_assumption is None
    assert valore.certainty is Certainty.OBSERVED


def test_iso_senza_offset_non_viene_letta_come_utc():
    """Assumere UTC su un orario senza fuso sbaglierebbe fino a due ore."""
    valore = parse_iso_datetime("2026-09-18T21:59:00")

    assert valore is not None
    assert valore.instant is None
    assert valore.certainty is Certainty.AMBIGUOUS


def test_iso_con_la_sola_data_non_produce_un_istante():
    """Anche in ISO 8601 vale la regola: senza orario nessun istante."""
    valore = parse_iso_datetime("2026-09-18")

    assert valore is not None
    assert valore.date_only == date(2026, 9, 18)
    assert valore.instant is None


# --------------------------------------------------------------------------
# Testo, stato, interi
# --------------------------------------------------------------------------


def test_clean_text_toglie_entita_e_spazi_ma_non_la_punteggiatura():
    """Entita' e spazi unificatori sono trasporto; le virgolette sono testo."""
    assert clean_text("&Egrave;&nbsp;cos&igrave;") == "È così"
    assert clean_text("  a\n\tb  ") == "a b"
    assert clean_text("&#8220;citato&#8221;") == "“citato”"
    assert clean_text("   ") is None
    assert clean_text(None) is None


@pytest.mark.parametrize(
    ("grezzo", "atteso"),
    [("Aperto", NoticeStatus.OPEN), ("chiuso", NoticeStatus.CLOSED)],
)
def test_stati_osservati_vengono_riconosciuti(grezzo, atteso):
    """I due stati realmente osservati sulle schede si normalizzano."""
    stato, originale = normalize_status(grezzo)

    assert stato is atteso
    assert originale == grezzo


def test_stato_ignoto_resta_unknown_conservando_l_originale():
    """"Revocato" non e' ne' aperto ne' chiuso: decide chi vede l'originale.

    Forzarlo su CLOSED nasconderebbe il bando, su OPEN ne prometterebbe uno
    che non esiste piu' (SPEC 2.1).
    """
    stato, originale = normalize_status("Revocato")

    assert stato is NoticeStatus.UNKNOWN
    assert originale == "Revocato"


def test_stato_assente_non_diventa_una_stringa_vuota():
    """Nessuno stato significa nessun originale da mostrare."""
    assert normalize_status(None) == (NoticeStatus.UNKNOWN, None)
    assert normalize_status("  ") == (NoticeStatus.UNKNOWN, None)


def test_numero_di_posti_con_separatore_di_migliaia():
    """"1.100" e' il numero osservato sulla scheda multiregionale."""
    assert parse_int("1.100") == 1100
    assert parse_int("1100") == 1100
    assert parse_int("0") == 0


@pytest.mark.parametrize(
    "testo", ["", "non specificato", "1 posto per ciascuna delle 3 sedi", "n. 5"]
)
def test_testo_non_numerico_non_diventa_zero(testo):
    """"Nessun posto dichiarato" e "zero posti" sono affermazioni diverse.

    Solo la seconda giustificherebbe di escludere il bando, quindi la prima
    non puo' essere rappresentata come 0 (SPEC 4.1).
    """
    assert parse_int(testo) is None


# --------------------------------------------------------------------------
# Hash
# --------------------------------------------------------------------------


def test_metadata_hash_non_dipende_dall_ordine_delle_chiavi():
    """Due dizionari con lo stesso contenuto danno lo stesso hash.

    Se dipendesse dall'ordine, ogni rilettura della scheda sembrerebbe un
    aggiornamento e le notifiche perderebbero significato (SPEC 4.2).
    """
    primo = metadata_hash({"titolo": "T", "posti": 3, "enti": ["A", "B"]})
    secondo = metadata_hash({"enti": ["A", "B"], "posti": 3, "titolo": "T"})

    assert primo == secondo


def test_metadata_hash_e_stabile_fra_chiamate_e_sensibile_al_contenuto():
    """Stabile a parita' di dati, diverso appena un valore cambia."""
    campi = {"titolo": "T", "scadenza": "2026-10-18T21:59:00+00:00"}

    assert metadata_hash(campi) == metadata_hash(dict(campi))
    assert metadata_hash(campi) != metadata_hash({**campi, "scadenza": "2026-10-20"})


def test_metadata_hash_ordina_gli_insiemi():
    """L'ordine di iterazione di un set cambia fra processi, l'hash no."""
    assert metadata_hash({"t": {"b", "a"}}) == metadata_hash({"t": {"a", "b"}})


def test_valore_assente_e_valore_vuoto_hanno_hash_diversi():
    """`None` non e' la stringa vuota: la differenza deve restare visibile."""
    assert metadata_hash({"pa_link": None}) != metadata_hash({"pa_link": ""})


def test_document_hash_lavora_sui_byte():
    """Testo e byte equivalenti danno lo stesso digest, in UTF-8."""
    assert document_hash("scheda") == document_hash(b"scheda")
    assert document_hash(b"") != document_hash(b"\x00")
