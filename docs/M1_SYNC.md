# M1 — Sincronizzazione persistente dei metadati

Implementazione dell'8 ottobre 2026. M1 aggiunge l'orchestrazione al livello di
acquisizione M0. Telegram, profili personali e lettura degli allegati restano
nelle milestone successive. Le verifiche di questa consegna sono offline.

## Avvio locale

Da una shell nella cartella del progetto, con Python 3.11 o superiore:

```powershell
python -m pip install -e ".[dev]"
python -m inpa_bot --db var/inpa.sqlite3 sync --user-agent "InpaMonitor/0.1 (+mailto:CONTATTO_REALE)"
python -m inpa_bot --db var/inpa.sqlite3 status
python -m inpa_bot --db var/inpa.sqlite3 list --region IT-88 --limit 20
```

Sostituire `CONTATTO_REALE` con il contatto dell'operatore. `IT-88` è il codice
interno ISO della Sardegna; i codici del menu della fonte sono distinti.
`python -m inpa_bot list --help` elenca regioni e province ammesse.
L'installazione espone anche il comando equivalente `inpa-bot`.

In questa consegna è stato preparato anche l'ambiente locale `.venv`: per usarlo
senza attivare script PowerShell, sostituire `python` con
`.\.venv\Scripts\python.exe`. Le versioni delle dipendenze collaudate sono in
`requirements-dev.lock`; su un ambiente nuovo si possono installare con
`python -m pip install -r requirements-dev.lock`, poi installare il progetto
con `python -m pip install -e . --no-deps`.

Per mantenere il processo in esecuzione:

```powershell
python -m inpa_bot --db var/inpa.sqlite3 watch --user-agent "InpaMonitor/0.1 (+mailto:CONTATTO_REALE)"
```

`watch` esegue un ciclo ogni 60 minuti dalla fine del tentativo precedente,
inclusi i tentativi falliti. Il servizio parte solo quando si esegue il comando;
non viene registrato come servizio Windows e non si avvia da solo al login.
La pianificazione sopravvive al riavvio perché usa l'ultimo tentativo nel database.
`sync` forza un tentativo immediato o riprende quello incompleto; rispetta comunque
un eventuale `Retry-After` ancora valido.

`Ctrl+C` o `SIGTERM` richiedono l'arresto: il processo termina la richiesta in corso,
salva il risultato della scheda, poi si ferma tra due operazioni. Durante la
scoperta controlla l'arresto tra due pagine. Un'interruzione brusca lascia il piano
persistente da riprendere al prossimo avvio.

Le opzioni `--interval-seconds`, `--reconcile-seconds` e
`--missing-retention-days` appartengono a `sync` e `watch`. I valori predefiniti
sono rispettivamente 3600 secondi, 86400 secondi e 7 giorni. Ogni tentativo conserva
la configurazione nel suo rapporto JSON. Una ripresa mantiene il piano già salvato;
le nuove impostazioni di riconciliazione si applicano al piano successivo.

## Invarianti

1. La scoperta usa un catalogo senza filtri personali. Pagine incomplete,
   anomalie bloccanti, ID duplicati, cursori ripetuti, conteggi discordanti e
   limiti superati impediscono la creazione del piano. Il catalogo viene
   ricaricato a ogni nuova scansione, anche nello stesso processo.
2. La singola transazione SQLite salva versione, eventuale evento e avanzamento
   del lavoro. Un errore annulla tutte queste scritture. Un lavoro già completato
   non viene eseguito durante la ripresa dello stesso piano.
3. L'ultima sincronizzazione riuscita cambia soltanto quando tutte le schede
   pianificate sono state lette. Gli errori restano visibili e ritentabili;
   una scheda fallita non viene trasformata in un risultato vuoto.
4. L'assenza dal catalogo non cancella un bando e non ne modifica lo stato.
   Le schede assenti vengono ricontrollate giornalmente per 7 giorni dalla prima
   assenza osservata in un catalogo completo. Poi restano archiviate. Una
   ricomparsa azzera il periodo di assenza.
5. Un lock del sistema operativo permette un solo sincronizzatore per archivio.
   La terminazione del processo rilascia il lock. Il file `.sync.lock` resta su
   disco e non deve essere cancellato mentre il servizio è in esecuzione.

Le garanzie riguardano un archivio su disco locale e i processi che usano questa
API. Filesystem di rete, scrittori esterni che aggirano il lock e perdita di
alimentazione del dispositivo non sono stati collaudati. SQLite usa WAL e
`synchronous=NORMAL`, come M0: il test di crash verifica la terminazione del
processo, non la durabilità dopo una perdita di alimentazione.

## Quali schede vengono rilette

La prima acquisizione pianifica tutte le schede del catalogo. Nei cicli successivi
vengono lette quelle nuove, quelle con `lastmod` cambiato o mancante e quelle non
rilette da almeno 24 ore. La riconciliazione evita di dipendere soltanto da
`lastmod`: una proroga può non aggiornare quel campo.

Il primo piano completato definisce l'archivio iniziale. I suoi eventi hanno tipo
`baseline`; i piani successivi generano `new` e `changed`. Un cambiamento del solo
HTML grezzo può creare una versione storica, ma non un evento di modifica.
Gli eventi sono dati persistenti per M2, non notifiche già inviate. La garanzia
di deduplicazione riguarda eventi e lavori, non una futura consegna a Telegram.

Gli errori di trasporto e parsing restano associati al lavoro. Su 401, 403, 429,
503, divieti robots, URL non sicuri o un `Retry-After` positivo, il tentativo
interrompe ulteriori richieste. Il termine di `Retry-After` resta nel database e
vale anche dopo un riavvio. Un errore permanente, per esempio un 404 ripetuto,
lascia il piano incompleto: M1 non scarta automaticamente la scheda per far
apparire riuscita la scansione. Non esiste ancora un comando di esclusione manuale.

I valori di frequenza sono impostazioni iniziali da validare. Su 1.700 schede,
il solo intervallo minimo di 2 secondi implica circa **57 minuti** per una lettura
integrale, prima di latenza e retry. Questo è un calcolo del carico, non una misura
della velocità contro inPA.

## Filtri e risultati

```powershell
python -m inpa_bot --db var/inpa.sqlite3 list --province SS --exclude-national --exclude-unresolved
```

Il comando applica la funzione territoriale già verificata in M0. Una regione
non viene trasformata in una provincia. Nazionali e territori irrisolti passano
per default; le due opzioni di esclusione operano quando si imposta un filtro
geografico. Più regioni/province vengono unite in OR. Il risultato riporta sempre
il link ufficiale e ricorda che l'area dichiarata può non essere la sede di lavoro.
Non valuta l'ammissibilità personale e non assegna tutti i posti a una provincia.
`list` mostra l'ultima versione nota, inclusi i bandi chiusi o non più nel catalogo.

`status` espone ultima riuscita, piano attivo, lavori pendenti, numero di bandi,
versioni, eventi e ultimo tentativo. Le risposte sono JSON su stdout. Codici di
uscita: `0` esito riuscito, `1` sincronizzazione incompleta o rinviata,
`2` configurazione/database/I/O, `3` archivio già in sincronizzazione,
`130` arresto richiesto durante `sync`. Gli errori di sintassi argparse escono
con `2` su stderr. `watch` resta attivo dopo errori della fonte e pubblica il
rapporto; errori del database interrompono il processo.

## Persistenza e compatibilità

Lo schema M0 resta versione 1. La chiave separata `m1_schema_version=1` versiona
le nuove tabelle `sync_batches`, `sync_jobs`, `sync_tracking`, `sync_events` e
`sync_attempts`. L'apertura di un archivio M0 aggiunge queste tabelle senza
riscrivere versioni e allegati esistenti. Un archivio con una versione M1 futura
viene rifiutato. Anche `status` e `list` inizializzano l'estensione M1 quando
aprono un archivio M0 esistente.

La chiave `(notice_id, version_no)` rende univoci gli eventi. Il database può
contenere la cronologia completa: M1 non applica retention alle versioni, agli
eventi o ai tentativi. Un archivio appartiene a un solo adattatore di fonte.

## Validazione riproducibile

```powershell
python -m pytest tests -q
python -m ruff check src/inpa_bot/sync src/inpa_bot/cli.py src/inpa_bot/__main__.py tests/sync scripts/benchmark_sync.py
python -m ruff format --check src/inpa_bot/sync src/inpa_bot/cli.py src/inpa_bot/__main__.py tests/sync scripts/benchmark_sync.py
python scripts/benchmark_sync.py --records 1000
```

La suite M1 verifica importazione, riavvio, ripresa del solo lavoro pendente,
rollback indotto tramite trigger SQLite, crash reale con `os._exit`, lock tra
due connessioni, riconciliazione, conservazione degli assenti, blocchi della fonte,
`Retry-After`, compatibilità M0, CLI e pianificazione. Il parser viene attraversato
anche usando HTML delle fixture M0. I test simulati non dimostrano la stabilità
attuale del sito. La prova continuativa di **48 ore** resta da eseguire sulla
macchina di destinazione.

Il benchmark sintetico misura importazione, ciclo invariato e riapertura. Riporta
hardware osservabile, versione Python/SQLite, commit e hash dei sorgenti: il commit
da solo non identifica i file ancora non tracciati. Non misura rete, RAM, scaling
multicore o condizioni operative della fonte.

Il risultato misurato di questa consegna è in
[`reports/m1_benchmark.json`](../reports/m1_benchmark.json); il rapporto di
validazione è in [`reports/m1_validation.md`](../reports/m1_validation.md).
