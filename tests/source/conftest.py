"""Fixture condivise dei test offline del livello di acquisizione (SPEC 3.5).

Tre cose sono definite qui e non nei singoli file, perche' se divergessero i
test perderebbero valore di prova:

1. I percorsi dei campioni reali. I fixture sono documenti scaricati il
   19/09/2026 dalla fonte: i test li leggono, non li fabbricano. Un HTML
   inventato dimostrerebbe che il parser legge l'HTML che abbiamo immaginato.
2. L'orologio. `OSSERVATO_IL` e' un istante fisso: `Provenance.observed_at`
   e' la data in cui il bot ha visto il dato (SPEC 4.2) e con
   `datetime.now()` gli snapshot di due esecuzioni non sarebbero
   confrontabili, cioe' proprio cio' che M0-06 chiede di poter fare.
3. Il fetcher di robots.txt sui fixture. Nessun test offline deve poter
   toccare la rete: un fetcher che conosce solo i due host campionati rende
   il fallimento evidente (`TransportError`) invece di far partire di
   nascosto una richiesta vera.
"""

from __future__ import annotations

import pathlib
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from inpa_bot.source.detail import AttachmentPermission, parse_detail
from inpa_bot.source.errors import TransportError
from inpa_bot.source.models import NoticeSnapshot

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SCHEDE = FIXTURES / "detail"

OSSERVATO_IL = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
"""Istante fisso delle osservazioni simulate, in UTC."""

# Le sei schede reali disponibili, con la ragione per cui ognuna e' stata
# tenuta: servono a scegliere il campione giusto invece di prenderne uno a caso.
SCHEDA_MULTIREGIONALE = "01c11fcf96d04a7092d039e5f1a66224"  # 14 regioni, 1100 posti
SCHEDA_LOCALE = "d3d8d8d7a82e40afbd75a26841bc9070"          # Calabria, PDF + DOCX
SCHEDA_SENZA_ALLEGATI = "df4a037fb81c4a9d94e5dfac92dedf4e"  # nessun allegato, nessun link PA
SCHEDA_TRE_ALLEGATI = "7ae248c78e784cd184d0ac0975be8c73"    # tre allegati, uno .p7m
SCHEDA_ETICHETTA_RARA = "4ab161d644994fb689f641ba2ad366fd"  # 'Corrispettivo minimo annuo:'
SCHEDA_RTF = "8534f1c7269e4744b6bd5d2a9f43b322"             # allegato .rtf

TUTTE_LE_SCHEDE: tuple[str, ...] = (
    SCHEDA_MULTIREGIONALE, SCHEDA_LOCALE, SCHEDA_SENZA_ALLEGATI,
    SCHEDA_TRE_ALLEGATI, SCHEDA_ETICHETTA_RARA, SCHEDA_RTF,
)

ROBOTS_PER_HOST: dict[str, str] = {
    "www.inpa.gov.it": "robots-www.txt",
    "inpa.gov.it": "robots-www.txt",
    "portale.inpa.gov.it": "robots-portale.txt",
}


def leggi_fixture(nome: str) -> str:
    """Testo di un fixture, sempre in UTF-8.

    L'encoding e' dichiarato e non lasciato alla piattaforma: su Windows il
    default e' cp1252 e le schede reali contengono accenti, virgolette
    tipografiche e toponimi tedeschi.
    """
    return (FIXTURES / nome).read_text(encoding="utf-8")


def leggi_scheda(notice_id: str) -> str:
    """HTML grezzo di una scheda campionata."""
    return (SCHEDE / f"{notice_id}.html").read_text(encoding="utf-8")


def fetcher_robots_fixture(url: str) -> tuple[int, str]:
    """Fetcher per `RobotsPolicy` che serve i robots.txt campionati.

    Un host non campionato solleva `TransportError` invece di restituire un
    robots vuoto: un file vuoto significherebbe "nessuna direttiva", cioe'
    permesso, e un test non deve poter concedere in silenzio cio' che la
    fonte non ha concesso.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    nome = ROBOTS_PER_HOST.get(host)
    if nome is None:
        raise TransportError(f"host senza fixture di robots.txt: {host!r}")
    return 200, leggi_fixture(nome)


@pytest.fixture
def percorso_fixtures() -> pathlib.Path:
    """Cartella dei campioni scaricati dalla fonte."""
    return FIXTURES


@pytest.fixture
def testo_fixture() -> Callable[[str], str]:
    """Caricatore di un fixture per nome file."""
    return leggi_fixture


@pytest.fixture
def html_scheda() -> Callable[[str], str]:
    """Caricatore dell'HTML di una scheda, per identificativo del bando."""
    return leggi_scheda


@pytest.fixture
def osservato_il() -> datetime:
    """Orologio fisso delle osservazioni simulate."""
    return OSSERVATO_IL


@pytest.fixture
def scheda() -> Callable[..., NoticeSnapshot]:
    """Interpreta una scheda campionata con l'orologio fisso.

    `attachment_permission` resta opzionale: senza valutazione delle
    condizioni di accesso gli allegati risultano non prelevabili, che e' il
    comportamento prudente di `detail.py` e va esercitato cosi' com'e'.
    """

    def _scheda(notice_id: str,
                permesso: AttachmentPermission | None = None) -> NoticeSnapshot:
        return parse_detail(
            leggi_scheda(notice_id),
            notice_id=notice_id,
            source_url=f"https://www.inpa.gov.it/bandi-e-avvisi/"
                       f"dettaglio-bando-avviso/?concorso_id={notice_id}",
            observed_at=OSSERVATO_IL,
            attachment_permission=permesso,
        )

    return _scheda
