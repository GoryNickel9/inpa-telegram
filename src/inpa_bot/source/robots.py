"""Lettura e applicazione di robots.txt (SPEC 3.1).

Perche' un parser proprio invece di `urllib.robotparser`: la specifica non
chiede solo di obbedire, chiede di *registrare* la condizione riscontrata
(M0-09, M0-10). `urllib.robotparser` risponde si'/no e scarta la regola che
ha deciso; qui la regola viene restituita insieme al verdetto, cosi' che un
rifiuto finisca nei rapporti come "vietato da <regola> letta il <data>" e non
come un generico errore.

Il modulo implementa il criterio consueto: vince la regola con il percorso
piu' lungo; a parita' di lunghezza vince `Allow`. Sono gestiti i caratteri
jolly `*` e l'ancoraggio finale `$`.

Nota sul progetto: `portale.inpa.gov.it` pubblica `Disallow: /` per ogni
user-agent. Non e' un dettaglio implementativo ma un vincolo di
architettura, discusso in `reports/access_conditions.md`.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timedelta, timezone

from .errors import TransportError

DEFAULT_TTL = timedelta(hours=24)

FAILURE_TTL = timedelta(minutes=5)
"""Durata della cache quando il recupero di robots.txt fallisce.

Molto piu' breve del TTL normale: un robots.txt irraggiungibile
produce un rifiuto prudente, e tenere quel rifiuto per 24 ore
significherebbe che un singolo errore di rete ferma l'acquisizione
per un giorno intero.
"""

ALLOW_WHEN_ABSENT: frozenset[int] = frozenset({404, 410})
"""Stati che significano davvero "questo file non esiste".

Solo per questi l'assenza di direttive equivale all'assenza di divieti. Ogni
altro stato diverso da 200 e' una risposta che non porta direttive, non un
permesso: vedi `evaluate`.
"""


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """Una direttiva Allow/Disallow con il suo pattern originale."""

    allow: bool
    pattern: str

    def __str__(self) -> str:
        return f"{'Allow' if self.allow else 'Disallow'}: {self.pattern or '(vuoto)'}"


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """Verdetto su un URL, con la regola che lo ha determinato."""

    allowed: bool
    rule: str
    source_url: str
    fetched_at: datetime | None
    origin: str
    """Come si e' arrivati al verdetto: `rules`, `no-robots`, `unreachable`,
    `override`."""


@dataclass(frozen=True, slots=True)
class RobotsFile:
    """Contenuto utile di un robots.txt gia' interpretato."""

    host: str
    fetched_at: datetime
    status: int | None
    raw: str
    groups: dict[str, tuple[RobotsRule, ...]] = field(default_factory=dict)
    crawl_delay: dict[str, float] = field(default_factory=dict)
    sitemaps: tuple[str, ...] = ()
    reachable: bool = True


def _glob_matches(pattern: str, text: str) -> bool:
    """Confronto con jolly `*`, a due indici e senza ricorsione.

    Perche' non una regex: tradurre ogni `*` in `.*` produce espressioni del
    tipo `^/a.*a.*a.*…X$`, che sul percorso sbagliato vanno in backtracking
    esponenziale. Misurato su questo stesso codice prima della correzione:
    8 jolly 0,2 s, 12 jolly 18,5 s. `evaluate` viene chiamata per ogni URL,
    quindi un solo pattern del genere in un robots.txt fermerebbe l'intera
    acquisizione -- e il robots.txt e' un documento della fonte, cioe'
    contenuto che non controlliamo (SPEC 11).

    Questo algoritmo e' quello classico dei glob: quando incontra un `*` ne
    ricorda la posizione e, se il seguito fallisce, riparte facendogli
    consumare un carattere in piu'. Nessun ramo viene esplorato due volte: il
    costo e' al piu' proporzionale al prodotto delle due lunghezze.
    """
    p = t = 0
    star = -1
    mark = 0
    n, m = len(text), len(pattern)
    while t < n:
        if p < m and pattern[p] == "*":
            star = p
            p += 1
            mark = t
        elif p < m and pattern[p] == text[t]:
            p += 1
            t += 1
        elif star >= 0:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < m and pattern[p] == "*":
        p += 1
    return p == m


@lru_cache(maxsize=1024)
def _normalised_pattern(pattern: str) -> str:
    """Forma del pattern pronta per `_glob_matches`.

    Un pattern robots vincola l'inizio del percorso; `$` in coda vincola anche
    la fine. Senza `$`, aggiungere un `*` finale rende il confronto "prefisso"
    un normale confronto completo, senza casi particolari nell'algoritmo.

    I `*` consecutivi vengono compattati: sono equivalenti a uno solo e
    moltiplicherebbero inutilmente i punti di ripartenza.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    while "**" in body:
        body = body.replace("**", "*")
    return body if anchored_end else body + "*"


def parse_robots(text: str, host: str, *, status: int | None = 200,
                 fetched_at: datetime | None = None,
                 reachable: bool = True) -> RobotsFile:
    """Interpreta il testo di un robots.txt.

    Righe non riconosciute vengono ignorate, come prevede il formato: un
    robots.txt con direttive sconosciute resta valido per quelle note.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    groups: dict[str, list[RobotsRule]] = {}
    delays: dict[str, float] = {}
    sitemaps: list[str] = []

    current: list[str] = []
    # Un blocco di piu' `User-agent:` consecutivi condivide le regole che
    # seguono; la prima direttiva chiude l'elenco degli agenti.
    expecting_agents = False

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().lower()
        value = value.strip()

        if key == "user-agent":
            if not expecting_agents:
                current = []
                expecting_agents = True
            agent = value.lower()
            current.append(agent)
            groups.setdefault(agent, [])
            continue

        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue

        expecting_agents = False
        if not current:
            continue

        if key in ("allow", "disallow"):
            for agent in current:
                groups.setdefault(agent, []).append(
                    RobotsRule(allow=(key == "allow"), pattern=value)
                )
        elif key == "crawl-delay":
            try:
                parsed_delay = float(value.replace(",", "."))
            except ValueError:
                continue
            for agent in current:
                delays[agent] = parsed_delay

    return RobotsFile(
        host=host,
        fetched_at=fetched_at,
        status=status,
        raw=text,
        groups={k: tuple(v) for k, v in groups.items()},
        crawl_delay=delays,
        sitemaps=tuple(sitemaps),
        reachable=reachable,
    )


def _select_group(robots: RobotsFile, user_agent: str) -> tuple[str, tuple[RobotsRule, ...]]:
    """Sceglie il gruppo applicabile confrontando il *product token*.

    RFC 9309 §2.2.1 vuole un confronto per uguaglianza, senza distinzione di
    maiuscole, fra il nome del gruppo e il product token del crawler: per
    `InpaTelegramBot-Probe/0.1 (+contatto: ...)` il token e'
    `InpaTelegramBot-Probe`, cioe' quanto precede la barra.

    Il confronto per SOTTOSTRINGA, che sembra piu' tollerante, e' sbagliato e
    lo si vede su questo stesso progetto: il nostro token contiene
    `TelegramBot`, quindi un gruppo scritto per quel bot verrebbe adottato
    come se fosse il nostro. Si finirebbe per obbedire alle regole di un altro
    programma -- o, con un `Allow` altrui, per attribuirsi un permesso mai
    concesso.
    """
    token = user_agent.split("/", 1)[0].strip().lower()
    full = user_agent.strip().lower()
    for candidate in (token, full):
        if candidate and candidate in robots.groups:
            return candidate, robots.groups[candidate]
    return "*", robots.groups.get("*", ())


def evaluate(robots: RobotsFile, url: str, user_agent: str) -> RobotsDecision:
    """Decide se `url` e' consentito, restituendo la regola determinante."""
    if not robots.reachable:
        # Un robots.txt irraggiungibile non e' un permesso. La specifica (3.1)
        # chiede di registrare il limite senza attribuire un contenuto presunto:
        # qui si sceglie il rifiuto prudente, esplicitandone il motivo.
        return RobotsDecision(
            allowed=False,
            rule="robots.txt non recuperato: rifiuto prudente",
            source_url=f"https://{robots.host}/robots.txt",
            fetched_at=robots.fetched_at,
            origin="unreachable",
        )

    status = robots.status
    robots_url = f"https://{robots.host}/robots.txt"

    if status is None or status not in ALLOW_WHEN_ABSENT and status != 200:
        # Qualunque risposta che non sia "200 con direttive" o "il file non
        # esiste" e' una NON risposta, e una non risposta non e' un permesso.
        #
        # Qui il progetto e' deliberatamente piu' prudente di RFC 9309, che
        # per i 4xx consente di procedere: su 401, 403, 429, 5xx e 3xx si
        # rifiuta. La ragione e' concreta e riguarda proprio questo caso
        # d'uso: gli allegati stanno su un host che li vieta, e un suo 503
        # momentaneo non deve diventare la finestra in cui il bot si ritiene
        # autorizzato a scaricarli. Meglio saltare un giro che superare un
        # divieto per via di un guasto altrui.
        return RobotsDecision(
            allowed=False,
            rule=f"robots.txt ha risposto {status}: rifiuto prudente, "
                 f"nessuna direttiva utilizzabile",
            source_url=robots_url,
            fetched_at=robots.fetched_at,
            origin="unreachable",
        )

    if status in ALLOW_WHEN_ABSENT or not robots.groups:
        # Il file non esiste (404/410), oppure esiste ed e' servito con 200 ma
        # non contiene alcun gruppo: in entrambi i casi la fonte non ha
        # espresso divieti.
        return RobotsDecision(
            allowed=True,
            rule="nessuna direttiva applicabile",
            source_url=robots_url,
            fetched_at=robots.fetched_at,
            origin="no-robots",
        )

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    agent_name, rules = _select_group(robots, user_agent)

    winner: RobotsRule | None = None
    winner_len = -1
    for rule in rules:
        if rule.pattern == "":
            # "Disallow:" vuoto significa "nessun divieto": non ha effetto.
            continue
        if _glob_matches(_normalised_pattern(rule.pattern), path):
            length = len(rule.pattern)
            if length > winner_len or (length == winner_len and rule.allow):
                winner = rule
                winner_len = length

    if winner is None:
        return RobotsDecision(
            allowed=True,
            rule=f"nessuna regola corrispondente nel gruppo '{agent_name}'",
            source_url=f"https://{robots.host}/robots.txt",
            fetched_at=robots.fetched_at,
            origin="rules",
        )

    return RobotsDecision(
        allowed=winner.allow,
        rule=f"[{agent_name}] {winner}",
        source_url=f"https://{robots.host}/robots.txt",
        fetched_at=robots.fetched_at,
        origin="rules",
    )


class RobotsPolicy:
    """Cache per host delle direttive, piu' le autorizzazioni esplicite.

    `authorized_hosts` esiste per un caso concreto e non ipotetico: gli
    allegati di inPA stanno su un host che vieta tutto. Se il gestore
    concedesse un'autorizzazione scritta, il codice non va modificato, va
    configurato -- e la deroga resta visibile nella decisione restituita
    (`origin="override"`) invece di sparire in una riga commentata.
    """

    def __init__(self, fetcher, user_agent: str, *,
                 ttl: timedelta = DEFAULT_TTL,
                 authorized_hosts: dict[str, str] | None = None,
                 clock=None) -> None:
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._ttl = ttl
        self._authorized = dict(authorized_hosts or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, RobotsFile] = {}
        self._fetching: set[str] = set()

    def _host_key(self, url: str) -> tuple[str, str]:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme or "https", parsed.netloc.lower()

    def robots_for(self, url: str) -> RobotsFile:
        scheme, host = self._host_key(url)
        cached = self._cache.get(host)
        now = self._clock()
        if cached is not None:
            # Un esito negativo vale molto meno a lungo di uno positivo. Con il
            # TTL pieno, un singolo errore di rete su robots.txt avrebbe
            # bloccato ogni richiesta a quell'host per 24 ore, senza mai
            # riprovare: un guasto momentaneo trasformato in un fermo di un
            # giorno. La prudenza sta nel rifiutare adesso, non nel restare
            # fermi domani.
            ttl = self._ttl if cached.reachable else FAILURE_TTL
            if now - cached.fetched_at < ttl:
                return cached

        if host in self._fetching:
            # Rientranza: qualcuno ha chiesto le direttive di questo host mentre
            # le stiamo gia' scaricando. Succede se il fetcher, per decidere il
            # proprio ritmo, interroga a sua volta questa policy. Restituire un
            # file vuoto e non memorizzarlo interrompe la catena senza
            # trasformare il caso in un permesso: `reachable=False` significa
            # rifiuto prudente.
            return RobotsFile(host=host, fetched_at=now, status=None, raw="",
                              reachable=False)

        robots_url = f"{scheme}://{host}/robots.txt"
        self._fetching.add(host)
        try:
            status, text = self._fetcher(robots_url)
            parsed = parse_robots(text, host, status=status, fetched_at=now)
        except TransportError:
            parsed = RobotsFile(host=host, fetched_at=now, status=None, raw="",
                                reachable=False)
        finally:
            self._fetching.discard(host)
        self._cache[host] = parsed
        return parsed

    def check(self, url: str) -> RobotsDecision:
        _, host = self._host_key(url)
        robots = self.robots_for(url)

        if host in self._authorized:
            return RobotsDecision(
                allowed=True,
                rule=f"deroga configurata: {self._authorized[host]}",
                source_url=f"https://{host}/robots.txt",
                fetched_at=robots.fetched_at,
                origin="override",
            )

        return evaluate(robots, url, self._user_agent)

    def crawl_delay(self, url: str) -> float | None:
        robots = self.robots_for(url)
        agent_name, _ = _select_group(robots, self._user_agent)
        if agent_name in robots.crawl_delay:
            return robots.crawl_delay[agent_name]
        return robots.crawl_delay.get("*")

    def sitemaps(self, url: str) -> tuple[str, ...]:
        return self.robots_for(url).sitemaps
