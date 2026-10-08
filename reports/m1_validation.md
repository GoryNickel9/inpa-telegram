# M1 — Esito della validazione

Data: 8 ottobre 2026. Ambiente locale Windows 11, Python 3.13.2.
Le prove riguardano la sincronizzazione dei metadati. Non è stato avviato un
monitoraggio reale e non sono stati scaricati allegati.

## Test

`python -m pytest tests -q`: **334 superati, 5 saltati**, in 12,70 secondi
nell'ambiente Python già disponibile. Ripetizione nell'ambiente `.venv`
installato da `pyproject.toml`: **334 superati, 5 saltati**, in 12,79 secondi.
I cinque test saltati sono quelli di integrazione di rete opt-in.
M1 aggiunge **48 test** ai 286 già presenti in M0.

Le prove coprono salvataggio/ripresa, versione/evento/avanzamento nella stessa
transazione, rollback SQLite, crash reale di un processo, esclusione di un
secondo sincronizzatore, cataloghi parziali o incoerenti, riconciliazione,
assenza senza cancellazione, controlli di accesso, `Retry-After` persistente,
compatibilità con archivio M0, filtri territoriali, CLI e pianificazione dopo
un riavvio. La CLI è stata verificata sia come `python -m inpa_bot` sia come
eseguibile installato `inpa-bot` tramite i rispettivi `--help`.

Lint: Ruff con regole di correttezza `E4`, `E7`, `E9`, `F`, esplicite in
`pyproject.toml`; formattazione controllata separatamente sui file M1.
L'estensione dei controlli stilistici agli altri moduli resta fuori da questa
consegna. Il core M0 modificato conserva i suoi test indipendenti.

## Benchmark

Comando eseguito nell'ambiente `.venv`:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_sync.py --records 1000 --out reports/m1_benchmark.json
```

Dataset: 1.000 schede sintetiche con descrizione di 1.024 caratteri ASCII,
un thread, SQLite 3.45.3. CPU rilevata come Intel64 Family 6 Model 165
Stepping 3; 8 CPU logiche. Dettagli e hash dei sorgenti nel
[`rapporto JSON`](m1_benchmark.json).

| Operazione | Tempo osservato |
|---|---:|
| Prima importazione | 1,093 s |
| Ciclo senza modifiche | 0,0447 s |
| Riapertura e lettura dello stato | 0,0106 s |

Dimensione su disco dopo chiusura: 2.854.913 byte, incluso il file di lock.
È una singola esecuzione con cache del sistema operativo calda. Non misura
latenza di rete, intervalli di cortesia, consumo RAM o stabilità di inPA.

## Limiti residui

La prova continuativa di **48 ore** sulla macchina di destinazione resta da
eseguire. Il processo `watch` non è stato registrato come servizio del sistema
operativo. Il blocco sugli allegati resta quello documentato nelle osservazioni
M0 del 19 settembre; non è stato rivalutato in questa consegna.

Gli eventi persistenti sono pronti per l'integrazione M2. Non esistono ancora
utenti Telegram, messaggi consegnati o deduplicazione della consegna sul servizio
esterno. Errori permanenti della fonte mantengono il piano incompleto e visibile.
Non sono stati verificati Linux, macOS, filesystem di rete o crash per perdita
di alimentazione. Lo storico non viene ancora ridotto tramite retention.
