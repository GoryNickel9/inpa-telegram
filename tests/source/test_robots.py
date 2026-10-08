"""Condizioni di accesso dichiarate dagli host (SPEC 3.1).

I due robots.txt usati sono quelli recuperati il 19/09/2026. Il piu'
importante e' quello di `portale.inpa.gov.it`: vieta tutto, allegati
compresi, ed e' il vincolo su cui poggia l'intera architettura del
progetto. Un test che lo dimostrasse per finta non servirebbe a nulla,
percio' si legge il file vero.

La seconda meta' dei test riguarda i casi in cui e' facile concedere per
sbaglio: robots irraggiungibile, gruppo di user-agent sbagliato, parita' di
lunghezza fra Allow e Disallow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from inpa_bot.source.errors import TransportError
from inpa_bot.source.robots import (
    RobotsFile,
    RobotsPolicy,
    evaluate,
    parse_robots,
)

from .conftest import fetcher_robots_fixture, leggi_fixture

AGENTE = "InpaTelegramBot-Probe/0.1"
ADESSO = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

URL_ALLEGATO = "https://portale.inpa.gov.it/api/media/83f5fa66-9038-4e7d-b09f-11e86f3956be"
URL_SCHEDA = ("https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/"
              "?concorso_id=df4a037fb81c4a9d94e5dfac92dedf4e")


@pytest.fixture
def robots_portale() -> RobotsFile:
    return parse_robots(leggi_fixture("robots-portale.txt"),
                        "portale.inpa.gov.it", fetched_at=ADESSO)


@pytest.fixture
def robots_www() -> RobotsFile:
    return parse_robots(leggi_fixture("robots-www.txt"),
                        "www.inpa.gov.it", fetched_at=ADESSO)


# --------------------------------------------------------------------------
# I due robots.txt reali
# --------------------------------------------------------------------------


def test_il_portale_vieta_gli_allegati_a_qualunque_agente(robots_portale):
    """`Disallow: /` copre `/api/media/...`: e' il vincolo del progetto.

    Non e' un dettaglio implementativo: e' il motivo per cui la rotta di
    scoperta e' la sitemap e per cui `fetch_attachment` non scarica nulla.
    """
    decisione = evaluate(robots_portale, URL_ALLEGATO, AGENTE)

    assert decisione.allowed is False
    assert "Disallow: /" in decisione.rule
    assert decisione.origin == "rules"
    # La regola viaggia con il verdetto: un rifiuto deve poter finire in un
    # rapporto come "vietato da <regola>" e non come errore generico (M0-09).
    assert decisione.source_url == "https://portale.inpa.gov.it/robots.txt"


def test_il_sito_pubblico_consente_le_schede_dei_bandi(robots_www):
    """Nessuna delle direttive di `www` tocca `/bandi-e-avvisi/`."""
    decisione = evaluate(robots_www, URL_SCHEDA, AGENTE)

    assert decisione.allowed is True
    assert decisione.origin == "rules"


@pytest.mark.parametrize(
    "percorso",
    ["/wp-json/", "/wp-admin/", "/wp-login.php", "/xmlrpc.php", "/admin-area/"],
)
def test_il_sito_pubblico_vieta_le_aree_di_amministrazione(robots_www, percorso):
    """Le cinque direttive realmente pubblicate vanno rispettate tutte."""
    decisione = evaluate(robots_www, f"https://www.inpa.gov.it{percorso}", AGENTE)

    assert decisione.allowed is False


def test_admin_ajax_e_consentito_perche_la_regola_e_piu_lunga(robots_www):
    """`Allow: /wp-admin/admin-ajax.php` batte `Disallow: /wp-admin/`."""
    decisione = evaluate(robots_www,
                         "https://www.inpa.gov.it/wp-admin/admin-ajax.php", AGENTE)

    assert decisione.allowed is True
    assert "Allow" in decisione.rule


def test_la_sitemap_dichiarata_dal_robots_viene_letta(robots_www):
    """E' da qui che si arriva alla rotta di scoperta, senza indovinare URL."""
    assert robots_www.sitemaps == ("https://www.inpa.gov.it/sitemap.xml",)


# --------------------------------------------------------------------------
# Regole: precedenza, jolly, gruppi
# --------------------------------------------------------------------------


def test_a_parita_di_lunghezza_vince_allow():
    """Criterio consueto, verificato in entrambi gli ordini di scrittura."""
    prima_il_divieto = parse_robots("User-agent: *\nDisallow: /area\nAllow: /area\n", "h")
    prima_il_permesso = parse_robots("User-agent: *\nAllow: /area\nDisallow: /area\n", "h")

    assert evaluate(prima_il_divieto, "https://h/area/x", AGENTE).allowed is True
    assert evaluate(prima_il_permesso, "https://h/area/x", AGENTE).allowed is True


def test_la_regola_piu_lunga_vince_su_quella_piu_generica():
    """Non conta l'ordine di scrittura ma la specificita' del percorso."""
    robots = parse_robots("User-agent: *\nDisallow: /a/\nAllow: /a/b/\n", "h")

    assert evaluate(robots, "https://h/a/x", AGENTE).allowed is False
    assert evaluate(robots, "https://h/a/b/x", AGENTE).allowed is True


def test_jolly_e_ancoraggio_finale():
    """`*` e `$` sono le due estensioni d'uso comune del formato."""
    robots = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", "h")

    assert evaluate(robots, "https://h/documenti/bando.pdf", AGENTE).allowed is False
    assert evaluate(robots, "https://h/documenti/bando.html", AGENTE).allowed is True


def test_disallow_vuoto_non_e_un_divieto():
    """"Disallow:" senza valore significa "nessun divieto"."""
    robots = parse_robots("User-agent: *\nDisallow:\n", "h")

    assert evaluate(robots, "https://h/qualsiasi", AGENTE).allowed is True


def test_il_gruppo_dedicato_all_agente_prevale_su_quello_generico():
    """Un gruppo scritto per noi non va ignorato a favore di `*`."""
    robots = parse_robots(
        "User-agent: *\nDisallow:\n\n"
        "User-agent: inpatelegrambot-probe/0.1\nDisallow: /riservato/\n",
        "h",
    )

    assert evaluate(robots, "https://h/riservato/x", AGENTE).allowed is False
    assert evaluate(robots, "https://h/riservato/x", "AltroBot/2.0").allowed is True


def test_piu_user_agent_consecutivi_condividono_le_regole():
    """Un blocco con piu' intestazioni si applica a tutte."""
    robots = parse_robots(
        "User-agent: uno\nUser-agent: due\nDisallow: /x\n", "h"
    )

    assert evaluate(robots, "https://h/x", "uno").allowed is False
    assert evaluate(robots, "https://h/x", "due").allowed is False


def test_le_regole_si_applicano_anche_alla_query_string():
    """La scheda si distingue solo per il parametro: il percorso e' comune."""
    robots = parse_robots("User-agent: *\nDisallow: /*?concorso_id=\n", "h")

    assert evaluate(robots, "https://h/scheda/?concorso_id=abc", AGENTE).allowed is False
    assert evaluate(robots, "https://h/scheda/", AGENTE).allowed is True


def test_righe_non_riconosciute_non_invalidano_le_altre():
    """Un robots con direttive sconosciute resta valido per quelle note."""
    robots = parse_robots(
        "# commento\nUser-agent: *\nQualcosa-di-nuovo: 3\nDisallow: /x\n", "h"
    )

    assert evaluate(robots, "https://h/x", AGENTE).allowed is False


def test_crawl_delay_viene_conservato():
    """Serve al client per rallentare oltre il proprio intervallo minimo."""
    robots = parse_robots("User-agent: *\nCrawl-delay: 5\n", "h")

    assert robots.crawl_delay["*"] == 5.0


# --------------------------------------------------------------------------
# Assenza di direttive e irraggiungibilita'
# --------------------------------------------------------------------------


def test_robots_assente_con_404_significa_nessuna_direttiva():
    """Un 404 e' una risposta: dice che l'host non pubblica direttive."""
    decisione = evaluate(parse_robots("", "h", status=404), "https://h/x", AGENTE)

    assert decisione.allowed is True
    assert decisione.origin == "no-robots"


def test_robots_irraggiungibile_non_e_un_permesso():
    """Senza risposta non si attribuisce un contenuto presunto (SPEC 3.1).

    La differenza con il caso precedente e' sostanziale: li' la fonte ha
    risposto, qui non sappiamo nulla, e il rifiuto prudente e' l'unica
    posizione difendibile.
    """
    irraggiungibile = RobotsFile(host="h", fetched_at=ADESSO, status=None,
                                 raw="", reachable=False)

    decisione = evaluate(irraggiungibile, "https://h/x", AGENTE)

    assert decisione.allowed is False
    assert decisione.origin == "unreachable"


def test_la_policy_nega_quando_il_recupero_fallisce():
    """Lo stesso rifiuto prudente, visto dal punto di ingresso pubblico."""

    def fetcher_guasto(url: str) -> tuple[int, str]:
        raise TransportError(f"DNS non risolto per {url}")

    policy = RobotsPolicy(fetcher=fetcher_guasto, user_agent=AGENTE)

    decisione = policy.check("https://portale.inpa.gov.it/api/media/x")

    assert decisione.allowed is False
    assert decisione.origin == "unreachable"


# --------------------------------------------------------------------------
# Policy: cache e deroghe
# --------------------------------------------------------------------------


def test_la_policy_legge_i_robots_reali_dei_due_host():
    """Il verdetto di progetto: schede si', allegati no."""
    policy = RobotsPolicy(fetcher=fetcher_robots_fixture, user_agent=AGENTE)

    assert policy.check(URL_SCHEDA).allowed is True
    assert policy.check(URL_ALLEGATO).allowed is False


def test_il_robots_di_un_host_viene_scaricato_una_volta_sola():
    """Entro il TTL la cache evita una richiesta per ogni URL controllato."""
    visite: list[str] = []

    def fetcher(url: str) -> tuple[int, str]:
        visite.append(url)
        return fetcher_robots_fixture(url)

    policy = RobotsPolicy(fetcher=fetcher, user_agent=AGENTE,
                          clock=lambda: ADESSO)

    policy.check(URL_SCHEDA)
    policy.check("https://www.inpa.gov.it/bandi-e-avvisi/")

    assert visite == ["https://www.inpa.gov.it/robots.txt"]


def test_scaduto_il_ttl_le_direttive_vengono_rilette():
    """Le condizioni della fonte possono cambiare: la cache non e' eterna."""
    visite: list[str] = []
    momento = ADESSO

    def fetcher(url: str) -> tuple[int, str]:
        visite.append(url)
        return fetcher_robots_fixture(url)

    policy = RobotsPolicy(fetcher=fetcher, user_agent=AGENTE,
                          ttl=timedelta(hours=1), clock=lambda: momento)
    policy.check(URL_SCHEDA)

    momento = ADESSO + timedelta(hours=2)
    policy.check(URL_SCHEDA)

    assert len(visite) == 2


def test_una_deroga_configurata_resta_visibile_nella_decisione():
    """Un'autorizzazione del gestore si configura, non si commenta nel codice.

    Resta `origin="override"` con la motivazione: cosi' una deroga non puo'
    confondersi con un permesso letto dal robots.txt.
    """
    policy = RobotsPolicy(
        fetcher=fetcher_robots_fixture, user_agent=AGENTE,
        authorized_hosts={"portale.inpa.gov.it": "autorizzazione via email del 20/09/2026"},
    )

    decisione = policy.check(URL_ALLEGATO)

    assert decisione.allowed is True
    assert decisione.origin == "override"
    assert "20/09/2026" in decisione.rule


def test_la_deroga_di_un_host_non_vale_per_gli_altri():
    """Una deroga e' per host: non diventa un permesso generale."""
    policy = RobotsPolicy(
        fetcher=fetcher_robots_fixture, user_agent=AGENTE,
        authorized_hosts={"portale.inpa.gov.it": "autorizzazione"},
    )

    assert policy.check("https://www.inpa.gov.it/wp-json/").allowed is False


def test_la_sitemap_dichiarata_e_raggiungibile_dalla_policy():
    """`policy.sitemaps` e' il modo previsto per arrivare alla scoperta."""
    policy = RobotsPolicy(fetcher=fetcher_robots_fixture, user_agent=AGENTE)

    assert policy.sitemaps("https://www.inpa.gov.it/") == (
        "https://www.inpa.gov.it/sitemap.xml",
    )
