"""Client HTTP prudente per l'acquisizione (SPEC 5.3, 9, 11).

Il client e' volutamente scomodo da usare in modo aggressivo:

* una richiesta per volta, con intervallo minimo per host, aumentato
  automaticamente se il robots.txt dichiara un `Crawl-delay` maggiore;
* i redirect NON sono automatici. Ogni salto viene ricontrollato contro
  l'elenco degli host ammessi e contro robots.txt, perche' un redirect e'
  esattamente il modo in cui si finisce su un host mai valutato (SPEC 11);
* il corpo si legge a blocchi con un tetto di byte, per non dipendere dalla
  buona fede del `Content-Length`;
* `401`, `403` e le richieste di autenticazione interrompono il percorso e
  vengono segnalate. Non esiste un ramo di codice che ritenti con credenziali
  o cambi user-agent: la specifica (3.1) vieta ogni elusione.

Sulla difesa dagli indirizzi interni: la barriera primaria e' l'elenco degli
host ammessi, perche' un bot che parla solo con due domini noti non ha motivo
di risolvere altro. Il controllo sugli indirizzi risolti e' una seconda
barriera contro un DNS che restituisca un indirizzo privato. Resta una
finestra TOCTOU fra il controllo e la connessione effettiva; con un elenco
chiuso di host `*.gov.it` il rischio residuo e' accettabile ed e' annotato
qui invece che taciuto.
"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .errors import (
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    TransportError,
    UnsafeUrlError,
)
from .robots import RobotsDecision, RobotsPolicy

USER_AGENT = "InpaTelegramBot-Probe/0.1 (+contatto: configurare prima dell'uso)"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ROBOTS_BYTES = 512 * 1024
"""Tetto per il solo `robots.txt`.

RFC 9309 §2.5 chiede ai crawler di analizzare almeno 500 KiB; oltre quella
soglia il contenuto puo' essere ignorato. Un robots.txt reale sta in pochi
kilobyte: questo limite e' gia' molto generoso.
"""


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """Parametri di cortesia e sicurezza.

    I valori non sono limiti ufficiali inPA: sono default prudenti proposti
    dalla specifica (5.3), da rendere piu' restrittivi se necessario.
    """

    user_agent: str = USER_AGENT
    timeout: float = 30.0
    min_interval: float = 2.0
    max_bytes: int = 25 * 1024 * 1024
    max_retries: int = 3
    backoff_base: float = 2.0
    max_backoff: float = 60.0
    max_redirects: int = 5
    allowed_hosts: frozenset[str] = frozenset({"www.inpa.gov.it", "inpa.gov.it"})
    allow_private_addresses: bool = False
    """Solo per i test locali. In produzione resta False."""


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_s: float
    fetched_at: datetime
    redirects: tuple[str, ...] = ()
    robots_decision: RobotsDecision | None = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def charset(self) -> str | None:
        raw = self.headers.get("content-type", "")
        for piece in raw.split(";")[1:]:
            key, _, value = piece.partition("=")
            if key.strip().lower() == "charset":
                return value.strip().strip('"') or None
        return None

    def decoded(self, fallback: str = "utf-8") -> tuple[str, bool]:
        """Testo del corpo, piu' un indicatore di decodifica imperfetta.

        Il secondo valore e' vero quando qualche byte non e' stato
        interpretabile ed e' stato sostituito. Serve perche' la sostituzione,
        da sola, produce testo di aspetto normale: un corpo troncato a meta'
        carattere o servito con il charset sbagliato diventerebbe una scheda
        che si legge benissimo e dice cose leggermente diverse dall'originale.
        Chi chiama puo' cosi' trasformarlo in un'anomalia invece di non
        accorgersene.
        """
        encoding = self.charset or fallback
        try:
            return self.body.decode(encoding), False
        except (LookupError, UnicodeDecodeError):
            return self.body.decode(fallback, errors="replace"), True

    def text(self, fallback: str = "utf-8") -> str:
        return self.decoded(fallback)[0]


@dataclass
class RequestRecord:
    """Traccia di una richiesta, per i rapporti M0-09."""

    url: str
    method: str
    status: int | None
    error: str | None
    elapsed_s: float
    bytes_read: int
    started_at: datetime
    content_type: str | None = None
    robots_rule: str | None = None
    attempt: int = 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disattiva i redirect automatici: ogni salto va rivalutato a mano."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _is_public_address(host: str) -> tuple[bool, str]:
    """Verifica che l'host non risolva su reti interne o metadata del cloud."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransportError(f"Risoluzione DNS fallita per {host}: {exc}") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"indirizzo non interpretabile: {address}"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"indirizzo non pubblico: {address}"
        # 169.254.169.254 e' gia' link-local, ma l'equivalente IPv6 dei
        # metadata non lo e' sempre: meglio essere espliciti.
        if address in ("169.254.169.254", "fd00:ec2::254"):
            return False, f"servizio metadata del cloud: {address}"
    return True, "ok"


class HttpClient:
    """Esegue richieste GET rispettando robots, ritmo e limiti."""

    def __init__(self, config: HttpConfig | None = None,
                 robots: RobotsPolicy | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self.config = config or HttpConfig()
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self.records: list[RequestRecord] = []
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.robots = robots if robots is not None else RobotsPolicy(
            fetcher=self._fetch_robots_txt, user_agent=self.config.user_agent
        )

    # -- controlli preliminari ------------------------------------------

    def validate_url(self, url: str) -> str:
        """Applica schema, host ammesso e controllo degli indirizzi risolti.

        E' anche la validazione richiesta per i link inviati dagli utenti
        (SPEC 9): dominio esatto e formato previsto, nessun indirizzo
        arbitrario.

        Proprio perche' riceve testo scritto da una persona, ogni rifiuto esce
        come `UnsafeUrlError`. `urlsplit` e l'accesso a `.port` sollevano
        `ValueError` su indirizzi come `https://host:99999/`, `https://host:abc/`
        o `https://[incompleto`: lasciarlo passare significherebbe far uscire
        dalla tassonomia di `errors.py` un input malformato, cioe' far cadere
        il bot su un messaggio invece di rispondere "questo link non va bene".
        """
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
            host = parsed.hostname
        except ValueError as exc:
            raise UnsafeUrlError(f"URL non interpretabile ({exc}): {url!r}") from exc

        if parsed.scheme != "https":
            raise UnsafeUrlError(f"Schema non ammesso ({parsed.scheme!r}): {url}")
        if not host:
            raise UnsafeUrlError(f"URL senza host: {url}")
        host = host.lower()
        if host not in self.config.allowed_hosts:
            raise UnsafeUrlError(
                f"Host non incluso fra quelli ammessi: {host}. "
                "Aggiungerlo richiede una verifica esplicita delle condizioni."
            )
        if port not in (None, 443):
            raise UnsafeUrlError(f"Porta non ammessa: {port}")
        if not self.config.allow_private_addresses:
            ok, reason = _is_public_address(host)
            if not ok:
                raise UnsafeUrlError(f"{reason} ({host})")
        return host

    def _respect_interval(self, host: str, *, consult_robots: bool = True) -> None:
        """Attende quanto serve prima di interrogare di nuovo `host`.

        `consult_robots=False` serve al recupero del robots.txt stesso: leggere
        il `Crawl-delay` richiede il robots.txt, e chiederlo mentre lo si sta
        scaricando rientrerebbe nella stessa funzione all'infinito. Per quella
        singola richiesta vale il solo intervallo configurato.
        """
        interval = self.config.min_interval
        if consult_robots:
            try:
                declared = self.robots.crawl_delay(f"https://{host}/")
            except Exception:  # noqa: BLE001 - il ritmo non deve far fallire la richiesta
                declared = None
            if declared is not None:
                interval = max(interval, declared)

        last = self._last_request.get(host)
        if last is not None:
            waited = self._clock() - last
            if waited < interval:
                self._sleep(interval - waited)
        self._last_request[host] = self._clock()

    # -- recupero --------------------------------------------------------

    def _fetch_robots_txt(self, url: str) -> tuple[int, str]:
        """Recupera un robots.txt senza consultare robots.txt (evita il ciclo)."""
        host = urllib.parse.urlsplit(url).hostname or ""
        self._respect_interval(host.lower(), consult_robots=False)
        request = urllib.request.Request(
            url, headers={"User-Agent": self.config.user_agent, "Accept": "text/plain"}
        )
        try:
            with self._opener.open(request, timeout=self.config.timeout) as resp:
                # Tetto proprio, molto piu' basso di `max_bytes`: quello serve
                # ai documenti dei bandi, mentre un robots.txt e' un file di
                # testo di qualche kilobyte. Leggerne 25 MB perche' l'host li
                # invia significherebbe farsi riempire la memoria dal
                # documento che dovrebbe dirci come comportarci bene.
                payload = resp.read(MAX_ROBOTS_BYTES)
                return resp.status, payload.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise TransportError(f"robots.txt non recuperabile da {url}: {exc}") from exc

    def get(self, url: str, *, accept: str = "*/*",
            check_robots: bool = True) -> Response:
        """GET con redirect verificati salto per salto."""
        seen: list[str] = []
        current = url
        for _ in range(self.config.max_redirects + 1):
            response = self._single_get(current, accept=accept,
                                        check_robots=check_robots)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise HttpStatusError(response.status, current,
                                          body_excerpt="redirect senza Location")
                seen.append(current)
                current = urllib.parse.urljoin(current, location)
                continue
            return replace(response, url=url, final_url=current,
                           redirects=tuple(seen))
        raise UnsafeUrlError(f"Troppi redirect a partire da {url}")

    def _single_get(self, url: str, *, accept: str, check_robots: bool) -> Response:
        try:
            host = self.validate_url(url)
        except UnsafeUrlError as exc:
            # `records` e' la sorgente dei rapporti M0-09: un URL rifiutato e'
            # un esito della prova quanto una richiesta riuscita. Senza questa
            # riga il rapporto direbbe "nessuna richiesta" dove in realta' c'e'
            # stato un rifiuto, e chi legge non saprebbe distinguere "non e'
            # stato necessario" da "e' stato impedito".
            self.records.append(RequestRecord(
                url=url, method="GET", status=None, error=f"unsafe_url: {exc}",
                elapsed_s=0.0, bytes_read=0,
                started_at=datetime.now(timezone.utc),
            ))
            raise

        decision: RobotsDecision | None = None
        if check_robots:
            decision = self.robots.check(url)
            if not decision.allowed:
                self.records.append(RequestRecord(
                    url=url, method="GET", status=None,
                    error="robots_disallowed", elapsed_s=0.0, bytes_read=0,
                    started_at=datetime.now(timezone.utc),
                    robots_rule=decision.rule,
                ))
                raise RobotsDisallowedError(url, decision.rule)

        attempt = 0
        while True:
            attempt += 1
            self._respect_interval(host)
            started_at = datetime.now(timezone.utc)
            start = self._clock()
            request = urllib.request.Request(url, headers={
                "User-Agent": self.config.user_agent,
                "Accept": accept,
                "Accept-Language": "it-IT,it;q=0.9",
            })
            try:
                with self._opener.open(request, timeout=self.config.timeout) as raw:
                    body = self._read_limited(raw, url)
                    headers = {k.lower(): v for k, v in raw.headers.items()}
                    elapsed = self._clock() - start
                    self.records.append(RequestRecord(
                        url=url, method="GET", status=raw.status, error=None,
                        elapsed_s=elapsed, bytes_read=len(body),
                        started_at=started_at,
                        content_type=headers.get("content-type"),
                        robots_rule=decision.rule if decision else None,
                        attempt=attempt,
                    ))
                    return Response(
                        url=url, final_url=url, status=raw.status, headers=headers,
                        body=body, elapsed_s=elapsed, fetched_at=started_at,
                        robots_decision=decision,
                    )
            except ResponseTooLargeError as exc:
                # Sollevata da `_read_limited` prima che la richiesta riuscita
                # venga registrata: senza questo ramo una risposta rifiutata
                # per dimensione non comparirebbe affatto nel rapporto.
                # Non si ritenta: il documento sarebbe grande anche al secondo
                # tentativo, e ribussare non cambierebbe l'esito.
                self.records.append(RequestRecord(
                    url=url, method="GET", status=None, error=f"troppo grande: {exc}",
                    elapsed_s=self._clock() - start, bytes_read=self.config.max_bytes,
                    started_at=started_at,
                    robots_rule=decision.rule if decision else None,
                    attempt=attempt,
                ))
                raise
            except TransportError as exc:
                # Corpo piu' corto del `Content-Length` dichiarato. E' un guasto
                # di trasporto come un timeout, quindi si registra e si ritenta
                # con la stessa politica: la seconda volta la risposta puo'
                # arrivare intera.
                elapsed = self._clock() - start
                self.records.append(RequestRecord(
                    url=url, method="GET", status=None, error=f"transport: {exc}",
                    elapsed_s=elapsed, bytes_read=0, started_at=started_at,
                    robots_rule=decision.rule if decision else None,
                    attempt=attempt,
                ))
                if attempt <= self.config.max_retries:
                    self._sleep(self._backoff(attempt, None))
                    continue
                raise
            except urllib.error.HTTPError as exc:
                elapsed = self._clock() - start
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                if exc.code in (301, 302, 303, 307, 308):
                    # Anche un salto e' una richiesta fatta alla fonte: senza
                    # questa riga il conteggio del rapporto sarebbe piu' basso
                    # del traffico realmente generato.
                    self.records.append(RequestRecord(
                        url=url, method="GET", status=exc.code, error=None,
                        elapsed_s=elapsed, bytes_read=0, started_at=started_at,
                        content_type=headers.get("content-type"),
                        robots_rule=decision.rule if decision else None,
                        attempt=attempt,
                    ))
                    return Response(
                        url=url, final_url=url, status=exc.code, headers=headers,
                        body=b"", elapsed_s=elapsed, fetched_at=started_at,
                        robots_decision=decision,
                    )
                excerpt = b""
                try:
                    excerpt = exc.read(2048)
                except Exception:  # noqa: BLE001 - il corpo dell'errore e' opzionale
                    pass
                self.records.append(RequestRecord(
                    url=url, method="GET", status=exc.code, error=f"HTTP {exc.code}",
                    elapsed_s=elapsed, bytes_read=len(excerpt),
                    started_at=started_at,
                    robots_rule=decision.rule if decision else None,
                    attempt=attempt,
                ))
                if exc.code in (401, 403):
                    # Percorso interrotto di proposito: nessun tentativo con
                    # credenziali, nessun cambio di user-agent (SPEC 3.1).
                    raise HttpStatusError(
                        exc.code, url,
                        body_excerpt="accesso negato: percorso interrotto, "
                                     "nessun tentativo di elusione",
                    ) from exc
                retry_after = _parse_retry_after(headers.get("retry-after"))
                if retry_after is not None and retry_after > self.config.max_backoff:
                    # La fonte ha chiesto un'attesa piu' lunga di quanto questo
                    # processo sia disposto a dormire. Le due risposte possibili
                    # sono aspettare davvero o smettere: riprovare prima, che e'
                    # cio' che farebbe un `min(retry_after, max_backoff)`,
                    # significa ignorare una richiesta esplicita del server e
                    # bussare mentre ci e' stato detto di non farlo (SPEC 5.3,
                    # e SPEC 3.1 sul non aggirare i blocchi).
                    #
                    # Si smette, riportando l'attesa dichiarata: chi ha chiamato
                    # puo' riprogrammare il lavoro, cosa che questo livello non
                    # ha titolo per decidere.
                    raise HttpStatusError(
                        exc.code, url, retry_after=retry_after,
                        body_excerpt=f"la fonte chiede {retry_after:.0f} s di attesa, "
                                     f"oltre il tetto di {self.config.max_backoff:.0f} s: "
                                     "percorso interrotto senza ritentare",
                    ) from exc
                if exc.code in RETRYABLE_STATUS and attempt <= self.config.max_retries:
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                raise HttpStatusError(exc.code, url, retry_after=retry_after,
                                      body_excerpt=excerpt[:500].decode(
                                          "utf-8", errors="replace")) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                elapsed = self._clock() - start
                self.records.append(RequestRecord(
                    url=url, method="GET", status=None, error=f"transport: {exc}",
                    elapsed_s=elapsed, bytes_read=0, started_at=started_at,
                    robots_rule=decision.rule if decision else None,
                    attempt=attempt,
                ))
                if attempt <= self.config.max_retries:
                    self._sleep(self._backoff(attempt, None))
                    continue
                # Errore di trasporto: mai riclassificato come 4xx o come
                # blocco della fonte (SPEC 1.3).
                raise TransportError(f"{url}: {exc}") from exc

    def _read_limited(self, stream, url: str) -> bytes:
        """Legge il corpo a blocchi, con tetto e verifica di completezza.

        Il confronto con `Content-Length` non e' pedanteria. `HTTPResponse.read(n)`
        di CPython non solleva `IncompleteRead`: se la connessione cade a meta',
        restituisce i byte arrivati e basta. Senza questo controllo una pagina
        troncata in transito arriverebbe al parser come documento valido, e
        siccome la scheda conserva le proprie etichette all'inizio del corpo,
        produrrebbe un bando con titolo giusto e tutti i campi vuoti: una
        proroga sparita, i posti azzerati, gli allegati "rimossi". Cioe' un
        errore di trasporto travestito da contenuto, che M0-08 vieta.

        Un corpo corto e' percio' `TransportError`, la stessa famiglia di un
        timeout, e viene ritentato come tale.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.max_bytes:
                raise ResponseTooLargeError(
                    f"{url}: superati {self.config.max_bytes} byte"
                )
            chunks.append(chunk)

        declared = getattr(stream, "headers", None)
        raw_length = declared.get("content-length") if declared is not None else None
        if raw_length is not None:
            try:
                expected = int(str(raw_length).strip())
            except ValueError:
                expected = None
            if expected is not None and total < expected:
                raise TransportError(
                    f"{url}: corpo incompleto, letti {total} byte dei {expected} "
                    "dichiarati da Content-Length"
                )
        return b"".join(chunks)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.config.max_backoff)
        return min(self.config.backoff_base ** attempt, self.config.max_backoff)


def _parse_retry_after(value: str | None) -> float | None:
    """Interpreta `Retry-After`, sia in secondi sia come data HTTP."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
