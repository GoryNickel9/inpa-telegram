# Migrazione VPS — 8 ottobre 2026

Bot e monitoraggio sono attivi su `212.227.134.114`. L'accesso SSH come `root`
con `vps_key` è riuscito; l'altro indirizzo salvato ha rifiutato quella chiave.
La chiave privata non è stata trasferita né mostrata.

## Ambiente e verifiche

| Verifica | Risultato osservato |
|---|---|
| Sistema VPS | Ubuntu 26.04, Python 3.14.4, systemd 259 |
| Risorse | 6 CPU, 7.866 MiB RAM totale, circa 164 GiB disco liberi |
| Suite Windows, Python 3.13.2 | 416 passati, 5 saltati, 4 warning; 11,92 s |
| Suite VPS dopo correzione import inutilizzati | 416 passati, 5 saltati, 4 warning; 1,28 s |
| Ruff, sorgenti e test | Superato su entrambi gli ambienti |
| `pip check` VPS | Nessuna dipendenza incompatibile |
| Verifica unità systemd | Nessun errore nelle unità inPA |
| `inpa-bot`, `inpa-watch`, `inpa-backup.timer` | Attivi e abilitati al boot |
| Autenticazione Telegram | Evento `telegram_started`, nessun recupero ambiguo |
| Primo backup manuale | `Result=success`, `ExecMainStatus=0` |

I cinque test saltati sono opt-in con rete. I quattro warning sono la
deprecazione SDK su `retry_after`, già gestito sia come secondi sia come
`timedelta`. systemd segnala anche due direttive obsolete di unità XFS già
presenti sul sistema, esterne alle unità installate. Queste unità non sono
state modificate. Le durate dei test non sono benchmark del servizio.

## Integrità e trasferimento

Il monitoraggio locale era in attesa, senza batch attivo o schede pendenti.
Sono stati fermati solo i processi `inpa_bot watch` di questo progetto, poi
creata una copia mediante SQLite Backup API, con controllo di integrità e
chiavi esterne. Il bot locale era già stato chiuso dall'utente.

La copia trasferita conteneva 1.789 bandi, versioni ed eventi, insieme alle
tabelle Telegram. Dimensione: 15.388.672 byte. SHA-256 uguale su Windows e VPS:

```text
bfa52f5e62c6873d5646659f231a5baf278ba98e6dd6d2592f50950c014a7630
```

La riapertura dell'archivio come utente `inpa-bot` ha conservato conteggi,
ultima sincronizzazione e assenza di lavoro pendente. Il primo backup della
VPS ha superato nuovamente i controlli. La copia di trasferimento temporanea
sul server e la configurazione privata temporanea sono state rimosse; i
sorgenti archiviati non includono `.env`, database o chiavi private.

## Decisioni operative

Il database attivo è `/var/lib/inpa-bot/inpa.sqlite3`, separato dal codice in
`/opt/inpa-telegram`. Token e impostazioni sono in `/etc/inpa-bot.env`, leggibile
solo da root; systemd li passa ai processi eseguiti come utente senza login.
Il file dati è `0600`, la directory `0700`. Nessuna porta applicativa è stata
aperta e nessun servizio delle altre applicazioni è stato modificato.

Il timer crea copie giornaliere dalle 03:15 UTC con ritardo casuale fino a dieci
minuti e mantiene le ultime sette copie riuscite. Il backup usa un file
temporaneo verificato e pubblicato atomicamente. Cinque test dedicati coprono
WAL con scritture non confermate, ripristino, retention, fonte mancante/corrotta
e parametro di retention non valido. Su Windows `fsync` richiede un handle
scrivibile: la correzione è coperta dalla prova di backup completo.

## Limiti e verifica successiva

I backup sullo stesso server non proteggono dalla perdita della VPS. Restano
copie locali precedenti, con gestione separata della cancellazione dei dati.
L'informativa `/privacy` ora segnala l'eventuale permanenza nei backup.
Non è stato eseguito un reboot reale né completato il monitoraggio di 48 ore.
La consegna del menu dalla VPS va verificata dall'utente inviando `/menu`.
Non sono stati inviati messaggi di prova artificiali ad altri utenti.

Comandi di gestione e ripristino: [VPS.md](../docs/VPS.md).
