# M2 — Validazione del bot privato

Verifica del 8 ottobre 2026 su Windows 11, Python 3.13.2. Il bot è implementato;
il collaudo con Telegram reale e il monitoraggio M1 di 48 ore restano da completare.
Nessun test qui riportato invia messaggi a Telegram o acquisisce pagine inPA.

## Risultati eseguiti

| Controllo | Risultato |
|---|---|
| `.venv\Scripts\python.exe -m pytest tests -q` | 411 passati, 5 saltati, 4 warning; 11,41 secondi |
| Test M2 isolati | 77 passati, 4 warning; 2,95 secondi |
| Ruff check | Superato |
| Ruff format del menu e dei suoi test | Eseguito sui due file modificati |
| `pip check` | Nessuna dipendenza incompatibile |

I cinque test saltati richiedono accesso alla fonte reale e sono opt-in.
I quattro warning provengono dal SDK Telegram: una versione futura rappresenterà
`retry_after` con `timedelta`. Il runtime gestisce sia secondi sia `timedelta`.

## Invarianti verificati

- Solo utenti autorizzati possono usare il bot; sono accettate chat private con
  identità coerente. Filtri, seguiti, nascosti e cancellazione restano per utente.
- Comando, risposte accodate e avanzamento dell'offset Telegram condividono una
  transazione. La raccolta degli eventi salva insieme coda e cursore utente.
- Gli eventi della prima acquisizione non generano una notifica per ogni bando.
  Le notifiche richiedono attivazione esplicita e rispettano filtri e nascosti.
- Un riavvio non reinvia automaticamente una consegna dall'esito incerto.
  `RetryAfter` imposta una pausa globale persistente; gli errori salvano il tipo,
  senza il testo dell'eccezione o il token.
- Le tabelle M2 sono additive. M1 può riaprire l'archivio e usare un lock separato.

I test comprendono rollback, ripresa, deduplicazione, revoca, limite utenti,
cancellazione con conferma scaduta, URL non validi, limiti UTF-16 e serializzazione
dei pulsanti attraverso il SDK reale con trasporto simulato.

Il menu aggiunge undici casi di test: navigazione senza comandi digitati,
approvazione tramite pulsante e tentativi di usare pulsanti amministrativi da
utenti ordinari o consultare bandi senza autorizzazione. `/start`, `/help` e
`/menu` mostrano i pulsanti; la guida condivisibile è `docs/USARE_IL_BOT.md`.
I comandi esistenti e il formato SQLite restano compatibili. Il processo già
avviato deve essere riavviato per caricare il menu. La prova dei nuovi pulsanti
su Telegram reale resta da eseguire.

## Benchmark offline

Esecuzione singola di `scripts/benchmark_telegram.py` su 1.789 bandi sintetici,
un thread e cache del sistema operativo già calda. I dati completi e l'hash del
codice sono in [m2_benchmark.json](m2_benchmark.json).
Questa misura precede l'aggiunta del menu; il suo hash identifica quella versione.
Il cambiamento di interfaccia non introduce ottimizzazioni né nuove dichiarazioni
di prestazioni.

| Operazione | Misura |
|---|---|
| Prima pagina e accodamento di 8 risposte | 0,1544 secondi |
| Registrazione delle 8 consegne simulate | 0,0342 secondi |
| Database dopo chiusura | 2.703.360 byte |

Il benchmark non misura latenza Telegram, throughput di rete o RAM. Non è una
misura di capacità in produzione.

## Decisioni e limiti

Il trasporto usa `python-telegram-bot` 22.8 come dipendenza opzionale. SQLite
conserva autorizzazioni, preferenze, offset e coda; token e amministratori
configurati risiedono nel file locale `.env`, escluso da Git.

Una risposta di rete persa può seguire una consegna riuscita. Poiché Telegram
non offre una chiave di idempotenza per `sendMessage`, il runtime conserva quel
caso come `ambiguous` e non lo reinvia automaticamente. Questo evita duplicati
automatici, ma può lasciare un messaggio non consegnato da esaminare.

Restano non verificati il trasporto con il bot dell'utente, l'esecuzione continua
e gli altri sistemi operativi. Requisiti personali dai documenti, digest,
fasce orarie e promemoria appartengono alle fasi successive. Il limite sugli
allegati inPA resta quello documentato nei report M0.

La prossima verifica è configurare l'ID amministratore, avviare il bot e provare
`/start`, `/filtri` e `/concorsi` nella propria chat privata seguendo
[M2_TELEGRAM.md](../docs/M2_TELEGRAM.md).

Per le successive modifiche a filtri, default e riuso dei messaggi vedere
[telegram_ui_validation.md](telegram_ui_validation.md), con 430 test passati
anche sulla VPS e verifica del menu nativo tramite API reale.
