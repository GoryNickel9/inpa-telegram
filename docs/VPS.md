# Bot inPA sulla VPS

Bot Telegram e monitoraggio girano come servizi Linux indipendenti. Gli utenti
continuano a usare lo stesso bot. Il PC Windows può essere spento.

## Installazione verificata l'8 ottobre 2026

Server `212.227.134.114`, Ubuntu 26.04, Python 3.14.4, systemd 259. Accesso SSH
come `root` con la chiave locale `C:\Users\GoryNickel\.ssh\vps_key`. Il profilo
locale `vps-inpa` ricorda server, utente e chiave. La chiave privata resta sul PC.

Da PowerShell:

```powershell
& 'C:\Windows\System32\OpenSSH\ssh.exe' vps-inpa
```

| Elemento | Percorso sulla VPS |
|---|---|
| Programma e ambiente Python | `/opt/inpa-telegram` |
| Database attivo, utenti e preferenze | `/var/lib/inpa-bot/inpa.sqlite3` |
| Token, amministratori, limite utenti e user agent | `/etc/inpa-bot.env` |
| Backup verificati | `/var/lib/inpa-bot/backups` |

I processi usano l'utente di sistema `inpa-bot`, senza login interattivo. La
directory dati ha permessi `0700`; database e configurazione privata `0600`.
systemd carica la configurazione privata prima di avviare i processi. Non
mostrare il contenuto del file di configurazione nei log o in chat.

## Controllo e riavvio

Nella sessione SSH:

```sh
systemctl is-active inpa-bot inpa-watch inpa-backup.timer
systemctl status inpa-bot inpa-watch --no-pager
journalctl -u inpa-bot -u inpa-watch -n 30 --no-pager
systemctl restart inpa-bot
```

Stato dell'archivio usando il percorso effettivo:

```sh
runuser -u inpa-bot -- /opt/inpa-telegram/.venv/bin/python -m inpa_bot --db /var/lib/inpa-bot/inpa.sqlite3 status
```

I servizi sono abilitati al boot e ripartono dopo un errore. Uno stop manuale
resta uno stop; `systemctl start inpa-bot inpa-watch` riavvia entrambi. `watch`
mantiene la cadenza e riprende il lavoro pendente dal database. Non avviare lo
stesso bot sul PC: i lock locali non coordinano due server diversi.

## Backup

Il timer esegue un backup ogni giorno dalle 03:15 UTC, con ritardo casuale
massimo di dieci minuti. Recupera un'esecuzione persa al riavvio. Conserva gli
ultimi sette backup riusciti, anche se eseguiti manualmente nello stesso giorno:
non significa necessariamente sette giorni di storico.

```sh
systemctl start inpa-backup
systemctl show inpa-backup -p Result -p ExecMainStatus
systemctl list-timers inpa-backup.timer --no-pager
```

Il servizio è `oneshot`: dopo il successo torna `inactive`.
`Result=success` e `ExecMainStatus=0` indicano un'esecuzione riuscita.

Lo script usa la [SQLite Backup API](https://www.sqlite.org/backup.html), include
i dati confermati nel WAL e verifica integrità e chiavi esterne. Pubblica il
file atomicamente prima di eliminare vecchie copie. La retention riguarda solo
i file datati prodotti dallo script.

Le copie sulla stessa VPS non proteggono dalla perdita del server. Non è ancora
configurato un backup esterno. Windows conserva l'archivio precedente e la
copia di migrazione: queste copie richiedono gestione separata quando si
cancellano dati personali.

Per ripristinare: fermare bot, monitoraggio e timer; conservare una copia
verificata dell'archivio attuale; ripristinare il backup scelto in una directory
dati vuota con proprietà `inpa-bot:inpa-bot` e permessi `0600`; riavviare e
controllare stato e log. Non riutilizzare vecchi WAL/SHM con un database diverso.
Il ripristino arretra anche cursori e consegne, quindi può causare ripetizioni.

## Nuova installazione o aggiornamento

Lo sviluppo e i test avvengono sul PC Windows. Il repository privato
`GoryNickel9/inpa-telegram` su GitHub conserva il codice. La VPS lo legge con
una deploy key dedicata, senza permesso di scrittura sul repository. Database,
token, configurazione privata e chiavi restano fuori da Git.

Per aggiornare, da PowerShell nella cartella del progetto:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
git add src tests docs deploy README.md
git commit -m "Descrizione della modifica"
git push origin main
$inpaCommit = git rev-parse HEAD
& 'C:\Windows\System32\OpenSSH\ssh.exe' vps-inpa "sh /opt/inpa-telegram/deploy/update-from-github.sh $inpaCommit"
```

Interrompere la procedura se un comando fallisce. GitHub esegue anche i test
offline su Python 3.13 e 3.14. Il push da solo non modifica il bot in esecuzione:
l'ultimo comando pubblica sulla VPS lo specifico commit testato sul PC.

Lo script scarica il codice da GitHub, verifica che il commit appartenga a
`origin/main` ed esegue test e lint in una cartella temporanea prima di fermare
il bot. Crea un backup SQLite e riavvia soltanto `inpa-bot`; controlla che
`inpa-watch` non sia ripartito. Se il passaggio fallisce, ripristina il commit
precedente. Non ripristina automaticamente il database.

Dipendenze e unità systemd devono coincidere con quelle installate: cambiamenti
a `requirements-dev.lock` o alle unità richiedono una migrazione separata.
Le migrazioni distruttive del database richiedono un piano di rollback dedicato.
Il controllo finale verifica il processo dopo cinque secondi; la risposta al
comando `/menu` va provata su Telegram. L'accesso GitHub della VPS usa l'alias
SSH `github-inpa`, con chiave `/root/.ssh/inpa_github_ed25519`.

### Prima installazione

1. Caricare i sorgenti in `/opt/inpa-telegram`, senza chiavi SSH, database o
   configurazioni private nell'archivio del codice. Creare `.venv` con Python
   3.11 o superiore, installare `requirements-dev.lock` e il progetto con
   `pip install --no-deps -e /opt/inpa-telegram`; eseguire i test offline.
2. Fermare i processi sull'host precedente. Creare una copia con
   `scripts/backup_sqlite.py`, trasferirla tramite SSH e verificarne l'hash.
   Installarla in `/var/lib/inpa-bot/inpa.sqlite3`. Non sovrascrivere un archivio
   già attivo sulla destinazione.
3. Preparare `/etc/inpa-bot.env` con token, ID amministratori, limite utenti e
   `INPA_USER_AGENT`, contenente il contatto reale dell'operatore. Non trasferire
   chiavi private. Eseguire come root
   `sh /opt/inpa-telegram/deploy/install-services.sh`.
4. Eseguire un backup e verificarlo; quindi attivare
   `systemctl enable --now inpa-bot inpa-watch inpa-backup.timer`.
   Controllare `telegram_started` nei log e provare `/menu` da Telegram.

Lo script prepara proprietà e unità, senza avviare servizi. Gli aggiornamenti
del codice devono conservare database e configurazione privata. La modalità
long polling non richiede un sito web né una porta in ingresso per Telegram.
