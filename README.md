# inpa-telegram — acquisizione, sincronizzazione e bot privato (M0–M2)

Bot Telegram privato per seguire i concorsi pubblici su [inPA](https://www.inpa.gov.it),
pensato per 3–4 utenti.

Il repository contiene il livello di acquisizione **M0** e la sincronizzazione
persistente **M1** e il bot privato **M2**, verificati offline. Il primo ciclo
reale M1 ha acquisito 1.789 bandi senza errori l'8 ottobre 2026. L'attivazione
Telegram richiede il token locale e l'ID amministratore. La verifica dei requisiti dai documenti resta sospesa
per il limite sugli allegati documentato sotto.

M1 aggiunge ripresa del lavoro dopo un riavvio, eventi senza duplicati,
riconciliazione giornaliera, CLI e processo periodico locale. Comandi, invarianti
e limiti sono in [`docs/M1_SYNC.md`](docs/M1_SYNC.md). Il collaudo operativo di
48 ore resta da completare.

M2 aggiunge utenti autorizzati, filtri separati, elenco paginato, seguiti,
privacy/cancellazione e coda persistente delle notifiche. La procedura di
attivazione è in [`docs/M2_TELEGRAM.md`](docs/M2_TELEGRAM.md). La consegna a
Telegram è stata avviata sulla VPS; la prova del menu dopo il trasferimento
resta da completare nella chat dell'utente.

Per invitare altre persone basta il link Telegram e l'approvazione
dell'amministratore. Gli utenti trovano un menu a pulsanti con `/start` o
`/menu`: non devono installare Python. La procedura è nella guida M2 e le
istruzioni da condividere sono in [`docs/USARE_IL_BOT.md`](docs/USARE_IL_BOT.md).

Bot e monitoraggio sono stati trasferiti sulla VPS l'8 ottobre 2026, con servizi
systemd e backup SQLite verificati. Percorsi, gestione e ripristino sono in
[`docs/VPS.md`](docs/VPS.md). La copia Windows è conservata e i suoi processi
sono stati fermati; il database attivo è quello della VPS.

Lo sviluppo avviene su questo PC; il codice passa dal repository privato
GitHub alla VPS. Il push non pubblica automaticamente: il comando di aggiornamento
in [`docs/VPS.md`](docs/VPS.md) installa un commit dopo test, lint e backup.

---

## Stato: cosa funziona e cosa no

| | |
|---|---|
| Scoperta dei bandi attivi | **Funziona.** 1680 id dalla sitemap pubblica |
| Metadati della singola scheda | **Funziona.** HTML server-side, parser verificato su 28 schede reali |
| Rilevamento di modifiche e proroghe | **Funziona.** Confronto deterministico fra versioni |
| Sincronizzazione persistente e ripresa | **Implementata e verificata offline.** Piano SQLite e salvataggio atomico |
| Esecuzione periodica | **Attiva sulla VPS.** Servizio `inpa-watch`; collaudo di 48 ore da completare |
| Bot Telegram privato | **Attivo sulla VPS.** Menu e accessi verificati offline; prova del menu dopo il trasferimento da completare |
| Download degli allegati (PDF, DOCX…) | **Bloccato.** L'host che li ospita vieta l'accesso automatico |
| Verifica dei requisiti dai documenti (M3) | **Non realizzabile** finche' il punto sopra resta aperto |

Nelle verifiche del **19 settembre 2026**, `portale.inpa.gov.it` pubblicava
`User-agent: * / Disallow: /`, e quell'host ospita **tutti** gli allegati osservati (40 su 40)
oltre all'endpoint di ricerca che la specifica indicava come candidato.

Le due letture complete sono in [`reports/access_conditions.md`](reports/access_conditions.md)
(condizioni e cosa chiedere al gestore) e [`reports/m0_data_access.md`](reports/m0_data_access.md)
(misure, test M0-01…M0-10 e limiti residui).

---

## La rotta usata

```text
robots.txt di www.inpa.gov.it   →  dichiara la sitemap
  └─ /sitemap.xml               →  indice
      └─ /sitemap-bandi.xml     →  1680 id + lastmod
          └─ /bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=<id>
                                →  metadati + riferimenti agli allegati
```

Tutto su `www.inpa.gov.it`, il cui `robots.txt` consente queste pagine e **dichiara esso
stesso la sitemap**. Nessuna credenziale, nessuna identita' digitale, nessun aggiramento.

---

## Uso

Servono Python ≥ 3.11 e nient'altro: il livello di acquisizione usa la sola libreria standard
(l'unica dipendenza, `tzdata`, e' un pacchetto di soli dati che serve a `zoneinfo` su Windows).

```bash
pip install -e ".[dev]"
```

Prova riproducibile contro la fonte reale:

```bash
python scripts/probe_inpa.py --out evidence/ --limit 5
```

Test offline, nessuna richiesta di rete:

```bash
python -m pytest tests/source -q
```

Suite completa, inclusa M1:

```bash
python -m pytest tests -q
```

Sincronizzazione singola e stato locale, dopo l'installazione:

```bash
python -m inpa_bot sync --user-agent "InpaMonitor/0.1 (+mailto:CONTATTO_REALE)"
python -m inpa_bot status
```

Sostituire il contatto prima dell'uso. Per il processo periodico usare `watch`
al posto di `sync`; le opzioni complete sono in [`docs/M1_SYNC.md`](docs/M1_SYNC.md).

Test di integrazione con richieste reali, esplicitamente opt-in:

```bash
INPA_INTEGRATION=1 python -m pytest tests/source -q -m integration
```

Uso come libreria:

```python
from inpa_bot.source.adapter import InpaSitemapAdapter
from inpa_bot.source.models import SearchFilters

adapter = InpaSitemapAdapter()
page = adapter.search(SearchFilters())
print(page.declared_total, "bandi attivi")
print("filtri non applicati:", page.unapplied_filters)

notice = adapter.fetch_notice(page.records[0].notice_id)
print(notice.title, notice.status_normalized, notice.closing_at)
```

---

## Struttura

```text
src/inpa_bot/source/
  models.py      contratto interno (SPEC §4): tipi, provenienza, anomalie
  errors.py      tassonomia degli errori: trasporto ≠ HTTP ≠ robots ≠ parsing
  robots.py      lettura e applicazione di robots.txt, con la regola che ha deciso
  http.py        client prudente: ritmo, redirect verificati, limiti, difese SSRF
  sitemap.py     scoperta dalla sitemap
  detail.py      parser della scheda pubblica
  normalize.py   date italiane (Europe/Rome), stati, testo, hash
  territory.py   anagrafica di 20 regioni e 107 province, riconoscimento conservativo
  snapshot.py    archivio SQLite e confronto deterministico fra versioni
  adapter.py     implementazione di SourceAdapter che unisce il tutto

scripts/probe_inpa.py    prova riproducibile di M0
src/inpa_bot/sync/       orchestrazione, piano persistente, eventi e lock M1
src/inpa_bot/cli.py      sync, watch, status, list
src/inpa_bot/telegram/   comandi privati, filtri, outbox e polling M2
scripts/benchmark_sync.py benchmark offline su dati sintetici
tests/source/            test offline su fixture reali + integrazione opt-in
tests/sync/              test M1, ripresa, crash, rollback e CLI
tests/telegram/          test M2 e trasporto SDK simulato
docs/M1_SYNC.md          istruzioni operative e invarianti M1
docs/M2_TELEGRAM.md      configurazione BotFather e avvio del bot
reports/                 esiti delle verifiche e contratto osservato
samples/                 piccoli campioni sanitizzati
```

---

## Tre scelte che vale la pena conoscere

**Un dato assente resta assente.** `SourceDateTime` separa la data dall'istante: se la fonte
pubblica solo un giorno, `instant` resta `None`. Non esiste un ramo di codice che completi una
data con le 23:59.

**Due hash, non uno.** Le schede di inPA cambiano byte a **ogni** richiesta, per via di un
token e di un id DOM rigenerati ogni volta. Misurato, non supposto: cinque schede riscaricate
a 25 minuti di distanza differivano tutte, e solo in quei due punti. Un rilevamento basato
sull'hash della pagina produrrebbe 1680 false notifiche l'ora; `metadata_hash` copre i soli
campi significativi.

**I filtri non applicati sono dichiarati.** L'adattatore non puo' filtrare per regione lato
fonte, quindi lo dice: `SearchPage.unapplied_filters` elenca cio' che il chiamante ha chiesto
e che non e' stato applicato. Un filtro silenziosamente ignorato e' peggio di un filtro
assente.

**Una non risposta non e' un permesso.** Se `robots.txt` risponde 403, 429 o 5xx, il client
rifiuta invece di procedere — piu' prudente di RFC 9309, che sui 4xx consentirebbe. Era un
difetto reale, trovato in revisione: un `503` momentaneo avrebbe reso scaricabili gli
allegati.

In tutto la revisione critica ha prodotto **17 correzioni**, elencate una per una in
[`reports/m0_data_access.md`](reports/m0_data_access.md) §4.4 e §4.5, ciascuna con il suo
test di regressione. Erano tutte invisibili ai 224 test scritti prima: vale la pena saperlo
prima di fidarsi di una suite verde.

---

## Licenza e attribuzione dei dati

I contenuti di inPA sono pubblicati con licenza **CC-BY 3.0 IT** (fonte:
[Note legali](https://www.inpa.gov.it/note-legali/), consultate il 19/09/2026), che richiede
di **citare la fonte**. Per questo `NoticeSnapshot.source_url` non e' opzionale e ogni
messaggio prodotto a valle deve riportare il link ufficiale al bando.
