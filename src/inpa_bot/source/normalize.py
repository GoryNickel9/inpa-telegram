"""Normalizzazione di testo, date, stati e hash (SPEC 4.1, 4.2).

Il modulo sta fra il parser e il contratto interno: riceve stringhe cosi'
come la fonte le pubblica e produce valori tipizzati. Tre regole lo
attraversano:

1. Cio' che non c'e' resta assente. Una data senza orario non diventa le
   23:59 e un numero non leggibile non diventa `0`: la specifica (4.1)
   vieta di sostituire un'informazione mancante con una plausibile.
2. Cio' che c'e' ma non e' risolvibile diventa `Certainty.AMBIGUOUS` con una
   nota, mai un'eccezione. Un'etichetta con un formato inatteso non deve
   interrompere l'acquisizione di un'intera scheda.
3. Il testo grezzo sopravvive alla normalizzazione. `SourceDateTime.raw`
   conserva la stringa esattamente come ricevuta, perche' la specifica (4.1)
   chiede di conservare orari originali *e* normalizzati: senza l'originale
   non e' piu' verificabile che cosa la fonte avesse davvero pubblicato.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Certainty, NoticeStatus, SourceDateTime

ROME = "Europe/Rome"
"""Fuso dell'interfaccia italiana: le date della scheda sono orari locali
non qualificati e vanno interpretati qui (SPEC 4.1)."""

ITALIAN_MONTHS: dict[str, int] = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

# La scheda scrive "06 Agosto 2026  14:00", ma i corpi descrittivi usano anche
# "1 ottobre 2026" (giorno a una cifra, mese minuscolo, nessun orario): il
# giorno e l'orario sono quindi entrambi flessibili, il mese e' una parola.
_ITALIAN_DATETIME_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[^\W\d_]+)\s+(?P<year>\d{4})"
    r"(?:[\s,]+(?:alle\s+)?(?:ore\s+)?"
    r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
    re.IGNORECASE,
)

_ISO_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Solo un numero intero, eventualmente con il separatore di migliaia italiano.
# Non si estrae una cifra da un testo libero: in "1 posto per ciascuna delle 3
# sedi" qualunque euristica sceglierebbe il numero sbagliato.
_INTEGER_RE = re.compile(r"\d{1,3}(?:\.\d{3})+|\d+")

_WHITESPACE_RE = re.compile(r"\s+")

# Caratteri a larghezza zero: in Unicode sono `Cf`, non spazi, quindi `\s` non
# li tocca. Vanno rimossi e non convertiti in spazio, perche' non separano
# nulla e spezzerebbero la parola che attraversano.
_ZERO_WIDTH = str.maketrans({"​": None, "‌": None, "‍": None,
                             "﻿": None})

_NO_TIME_NOTE = ("la fonte pubblica la sola data, senza orario: nessun istante "
                 "e' stato calcolato (SPEC 4.1)")


def clean_text(raw: str | None, *, unescape: bool = True) -> str | None:
    """Ripulisce una stringa della pagina conservandone il contenuto.

    Le schede arrivano con entita' HTML (`&Egrave;`, `&ndash;`, `&#8220;`) e
    con spazi unificatori (`&nbsp;`): sono artefatti di trasporto, non
    contenuto. La punteggiatura invece resta intatta, virgolette tipografiche
    comprese, perche' fa parte del testo ufficiale del bando.

    Restituisce `None` quando non rimane nulla: un'etichetta presente con il
    valore vuoto e' un dato assente, e come tale va rappresentato.

    `unescape=False` va usato quando le entita' sono GIA' state risolte da chi
    chiama. E' il caso del parser della scheda: `html.parser.HTMLParser` con
    `convert_charrefs=True` decodifica da solo sia il testo sia i valori degli
    attributi, e decodificare una seconda volta corrompe il contenuto. Un
    bando che nel sorgente scrive `&amp;lt;b&amp;gt;` intende il testo
    letterale `&lt;b&gt;`; con due passaggi diventerebbe `<b>`, cioe' del
    markup vero. Oltre a essere sbagliato, sarebbe pericoloso a valle: quel
    titolo, inviato a Telegram in modalita' HTML, verrebbe interpretato.
    """
    if raw is None:
        return None
    # In Python 3 `\s` su stringhe copre gia' gli spazi Unicode della
    # categoria Zs (U+00A0, U+2009, U+3000...): non serve elencarli.
    text = html.unescape(raw) if unescape else raw
    text = text.translate(_ZERO_WIDTH)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def _unreadable(raw: str, reason: str) -> SourceDateTime:
    """Data presente ma non risolvibile: si conserva il testo, non si indovina."""
    return SourceDateTime(raw=raw, certainty=Certainty.AMBIGUOUS, note=reason)


def parse_italian_datetime(raw: str | None,
                           *, tz_name: str = ROME) -> SourceDateTime | None:
    """Interpreta una data della scheda ("06 Agosto 2026  14:00").

    Con l'orario si ottiene un istante *aware* nel fuso indicato, cosi' che
    l'ora legale sia risolta dal database dei fusi e non da un offset fisso
    (SPEC 4.1): il 6 agosto vale +02:00, il 6 gennaio +01:00.

    Senza orario `instant` resta `None`. E' il punto in cui e' piu' comodo
    inventare le 23:59, ed e' esattamente cio' che la specifica vieta: una
    scadenza dedotta a mezzanotte produrrebbe notifiche sbagliate di ore.
    """
    if raw is None:
        return None
    text = clean_text(raw)
    if text is None:
        return None

    match = _ITALIAN_DATETIME_RE.fullmatch(text)
    if match is None:
        return _unreadable(raw, "testo non riconosciuto come data italiana")

    month = ITALIAN_MONTHS.get(match["month"].lower())
    if month is None:
        return _unreadable(raw, f"mese non riconosciuto: {match['month']!r}")

    try:
        day_only = date(int(match["year"]), month, int(match["day"]))
    except ValueError:
        # Es. "31 Febbraio 2026": la fonte ha pubblicato qualcosa, ma non un
        # giorno esistente. Nessun arrotondamento al giorno valido piu' vicino.
        return _unreadable(raw, "giorno inesistente nel calendario")

    if match["hour"] is None:
        return SourceDateTime(raw=raw, date_only=day_only, note=_NO_TIME_NOTE)

    try:
        naive = datetime(day_only.year, day_only.month, day_only.day,
                         int(match["hour"]), int(match["minute"]),
                         int(match["second"] or 0))
    except ValueError:
        # L'orario e' fuori intervallo ma il giorno e' stato letto: si tiene
        # il giorno, si dichiara l'incertezza, non si scarta tutto.
        return SourceDateTime(raw=raw, date_only=day_only,
                              certainty=Certainty.AMBIGUOUS,
                              note="orario fuori intervallo, conservata la sola data")

    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        # Senza database dei fusi (Windows senza `tzdata`) l'istante sarebbe
        # un'assunzione mascherata da osservazione: meglio dichiararlo.
        return SourceDateTime(
            raw=raw, date_only=day_only, certainty=Certainty.AMBIGUOUS,
            note=f"fuso '{tz_name}' non disponibile: istante non calcolato",
        )

    instant = naive.replace(tzinfo=zone)

    # Le due ore patologiche dei cambi d'ora. Vanno distinte perche' hanno
    # conseguenze diverse: l'ora saltata in primavera non esiste affatto, e
    # qualunque istante le si attribuisca sarebbe inventato; l'ora ripetuta in
    # autunno esiste due volte e la scelta della prima occorrenza e' una
    # convenzione nostra, che va dichiarata invece che subita.
    if instant.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return SourceDateTime(
            raw=raw, date_only=day_only, certainty=Certainty.AMBIGUOUS,
            note="orario inesistente nel passaggio all'ora legale",
        )
    if instant.utcoffset() != instant.replace(fold=1).utcoffset():
        return SourceDateTime(
            raw=raw, date_only=day_only, instant=instant, tz_assumption=tz_name,
            certainty=Certainty.AMBIGUOUS,
            note="orario ripetuto nel ritorno all'ora solare: scelta la prima occorrenza",
        )

    return SourceDateTime(raw=raw, date_only=day_only, instant=instant,
                          tz_assumption=tz_name, certainty=Certainty.OBSERVED)


def parse_iso_datetime(raw: str | None) -> SourceDateTime | None:
    """Interpreta un timestamp ISO 8601, come i `<lastmod>` della sitemap.

    Quando l'offset e' dichiarato dalla fonte ("2026-09-18T21:59:00Z") non c'e'
    nessuna assunzione da registrare: `tz_assumption` resta `None`, che e' cosa
    diversa dal caso italiano in cui il fuso lo scegliamo noi.
    """
    if raw is None:
        return None
    text = clean_text(raw)
    if text is None:
        return None

    if _ISO_DATE_ONLY_RE.fullmatch(text):
        try:
            return SourceDateTime(raw=raw, date_only=date.fromisoformat(text),
                                  note=_NO_TIME_NOTE)
        except ValueError:
            return _unreadable(raw, "data ISO 8601 non valida")

    candidate = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return _unreadable(raw, "testo non interpretabile come ISO 8601")

    if parsed.tzinfo is None:
        # Un orario ISO senza offset non individua un istante: assumere UTC
        # sarebbe un'invenzione silenziosa con un errore fino a due ore.
        return SourceDateTime(
            raw=raw, date_only=parsed.date(), certainty=Certainty.AMBIGUOUS,
            note="ISO 8601 senza offset: istante non determinabile",
        )

    instant = parsed.astimezone(timezone.utc)
    return SourceDateTime(raw=raw, date_only=instant.date(), instant=instant,
                          certainty=Certainty.OBSERVED)


def normalize_status(raw: str | None) -> tuple[NoticeStatus, str | None]:
    """Normalizza lo stato restituendo anche l'originale ripulito.

    Il riconoscimento e' per corrispondenza esatta e non per somiglianza. La
    specifica (2.1) avverte che gli stati osservati non sono l'elenco
    completo: uno stato ignoto ("Revocato", "In pubblicazione") deve restare
    `UNKNOWN` e conservare il testo, perche' trattarlo come chiuso
    nasconderebbe il bando e trattarlo come aperto ne prometterebbe uno che
    non esiste piu'. La decisione spetta a chi vede anche l'originale.
    """
    original = clean_text(raw)
    if original is None:
        return NoticeStatus.UNKNOWN, None
    key = original.casefold()
    if key in ("aperto", "open"):
        return NoticeStatus.OPEN, original
    if key in ("chiuso", "closed"):
        return NoticeStatus.CLOSED, original
    return NoticeStatus.UNKNOWN, original


def parse_int(raw: str | None) -> int | None:
    """Legge un intero della scheda ("1100", "1.100").

    Restituisce `None` e mai `0` quando il testo non e' un numero: "nessun
    posto dichiarato" e "zero posti" sono affermazioni diverse, e solo la
    seconda giustificherebbe di escludere il bando.
    """
    text = clean_text(raw)
    if text is None:
        return None
    if _INTEGER_RE.fullmatch(text) is None:
        return None
    return int(text.replace(".", ""))


def _json_default(value: object) -> object:
    # Gli insiemi si serializzano ordinati: l'ordine di iterazione di un `set`
    # di stringhe dipende da PYTHONHASHSEED e cambierebbe l'hash a ogni
    # processo, cioe' proprio il falso aggiornamento che si vuole evitare.
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)


def metadata_hash(fields: Mapping[str, object]) -> str:
    """sha256 dei soli campi significativi di una scheda.

    Esiste separato da `document_hash` perche' l'HTML di inPA contiene parti
    che cambiano a ogni richiesta (token, contatori, ordini variabili): se il
    confronto fra due acquisizioni si basasse sul documento, ogni giro
    sembrerebbe un aggiornamento e le notifiche perderebbero significato. Qui
    entrano solo i campi il cui cambiamento interessa davvero un utente
    (SPEC 4.2).

    La serializzazione e' deterministica per costruzione: chiavi ordinate,
    separatori fissi e nessuna dipendenza dalla piattaforma o dalla locale.
    """
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=_json_default)
    return document_hash(payload)


def document_hash(payload: bytes | str) -> str:
    """sha256 dei byte grezzi di un documento (HTML, PDF, allegato).

    Sui byte e non sul testo interpretato: per gli allegati la specifica (4.2)
    chiede di confrontare i byte, perche' lo stesso URL puo' servire un
    contenuto diverso senza che `ETag` o `Last-Modified` lo dichiarino.
    """
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()
