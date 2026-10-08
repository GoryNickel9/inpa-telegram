"""Errori del livello di acquisizione.

Le distinzioni qui sono deliberate: la specifica richiede che
"errore di fonte", "documento non letto" e "nessun risultato" restino
tre condizioni separate (SPEC 5.3). Collassarle in un'unica eccezione
generica riprodurrebbe proprio il falso "nessun concorso" vietato da M0-08.
"""

from __future__ import annotations


class SourceError(Exception):
    """Base di tutti gli errori del livello di acquisizione."""


class TransportError(SourceError):
    """Guasto sotto il livello HTTP: DNS, TLS, connessione, timeout.

    Tenuto separato dagli errori applicativi perche' la specifica (1.3)
    vieta esplicitamente di classificare un fallimento DNS come 401/403/404
    o come blocco imposto dalla fonte.
    """


class HttpStatusError(SourceError):
    """Risposta HTTP ricevuta con stato non atteso."""

    def __init__(self, status: int, url: str, *, retry_after: float | None = None,
                 body_excerpt: str = "") -> None:
        super().__init__(f"HTTP {status} su {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after
        self.body_excerpt = body_excerpt


class ContentTypeError(SourceError):
    """Il corpo ricevuto non e' del tipo atteso (es. HTML al posto di JSON)."""


class ResponseTooLargeError(SourceError):
    """La risposta supera il limite di dimensione configurato."""


class RobotsDisallowedError(SourceError):
    """La richiesta e' vietata dal robots.txt dell'host.

    Non e' un errore di rete e non va ritentata: e' una condizione di
    utilizzo dichiarata dalla fonte (SPEC 3.1).
    """

    def __init__(self, url: str, rule: str) -> None:
        super().__init__(f"robots.txt vieta {url} (regola: {rule})")
        self.url = url
        self.rule = rule


class UnsafeUrlError(SourceError):
    """URL rifiutato dai controlli di sicurezza (host, schema, rete privata).

    Copre il requisito SPEC 11 su path traversal, redirect non verificati,
    indirizzi locali e servizi di metadata del cloud.
    """


class ParseError(SourceError):
    """Il documento e' stato ricevuto ma non e' interpretabile."""


class IncompleteAcquisitionError(SourceError):
    """Una scansione e' terminata senza poter garantire la completezza.

    Chi la riceve NON deve avanzare il punto di ripresa globale (SPEC 5.2).
    """
