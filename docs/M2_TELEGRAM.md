# M2 — Bot Telegram privato

M2 collega l'archivio M1 a Telegram. Implementa accesso per ID numerico,
filtri personali, elenco paginato, bandi seguiti/nascosti e notifiche persistenti.
Il processo Telegram consulta il database: l'acquisizione da inPA resta nel
processo `watch`. Le due applicazioni possono usare lo stesso archivio locale.

## Configurazione iniziale su Windows

Eseguire i comandi dalla cartella del progetto:

```powershell
cd "C:\Users\GoryNickel\Documents\GitHub\inpa-telegram"
```

1. Se il bot non esiste ancora, aprire [@BotFather](https://t.me/BotFather),
   inviare `/newbot`, scegliere nome e username e conservare il token.
   La procedura ufficiale è descritta nell'[introduzione ai bot](https://core.telegram.org/bots#how-do-i-create-a-bot).
2. Configurare il token localmente:

   ```powershell
   .\.venv\Scripts\python.exe -m inpa_bot telegram configure
   ```

   Il token viene richiesto con input nascosto. Se non si conosce ancora il
   proprio ID Telegram numerico, lasciare vuoto il campo amministratore.
   Il comando scrive `.env` atomicamente; il file è escluso da Git.
3. Aprire il nuovo bot su Telegram e inviare `/start` in una chat privata.
   Prima dell'avvio del servizio è normale non ricevere risposta.
4. Identificare il proprio ID, senza inviare messaggi automatici:

   ```powershell
   .\.venv\Scripts\python.exe -m inpa_bot telegram identify
   ```

   Il comando legge le richieste private `/start` e restituisce
   `private_start_user_ids`. Può attendere fino a 20 secondi. Se l'elenco è
   vuoto, inviare nuovamente `/start` e ripetere. Non avviare `identify` mentre
   il processo `telegram run` è in esecuzione. Se compaiono più ID, verificare
   quale sia il proprio prima di configurare un amministratore.
5. Ripetere `telegram configure`: lasciare vuoto il token per mantenere quello
   salvato e inserire il proprio ID amministratore numerico. Non usare lo
   username al posto dell'ID. Token e file `.env` non vanno incollati nelle chat.

Le dipendenze Telegram sono già state installate nell'ambiente `.venv` di questa
consegna. Per una nuova installazione:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,telegram]"
```

L'ambiente collaudato completo è riproducibile con `requirements-dev.lock`,
seguito da `python -m pip install -e . --no-deps`. M0/M1 restano installabili
senza l'extra `telegram`.

## Condividere il bot con altre persone

Gli invitati usano Telegram: non installano il progetto e non ricevono token o
file di configurazione. Condividere il link `https://t.me/USERNAME_DEL_BOT`,
sostituendo il segnaposto con lo username scelto su BotFather, senza `@`.

1. L'invitato apre il link e preme Avvia oppure invia `/start`.
2. L'amministratore apre `/menu` e tocca **Richieste di accesso**.
3. Verifica con la persona l'ID e tocca **Approva ID**. Restano validi il limite
   utenti e i controlli amministratore anche per i pulsanti.
4. L'invitato invia di nuovo `/start`, poi usa **Filtri**, **Concorsi** e **Notifiche**.

Prima dell'approvazione il bot resta silenzioso: la richiesta viene registrata.
Gli utenti hanno preferenze separate. Il menu si riapre con `/menu`; le schede,
i filtri e le notifiche hanno un pulsante di ritorno. La guida per gli invitati
è [USARE_IL_BOT.md](USARE_IL_BOT.md).

Nell'installazione locale il servizio richiede il PC acceso e connesso.
Dall'8 ottobre 2026 il bot è in esecuzione sulla VPS con avvio automatico:
il PC può essere spento. Vedere [VPS.md](VPS.md) per gestione e database attivo.

## Avviare i processi locali

In una finestra PowerShell, mantenere il monitoraggio M1:

```powershell
.\.venv\Scripts\python.exe -m inpa_bot watch --user-agent "InpaMonitor/0.1 (+mailto:CONTATTO_REALE)"
```

In una seconda finestra, nella stessa cartella:

```powershell
.\.venv\Scripts\python.exe -m inpa_bot telegram run
```

Poi inviare `/start` al bot. L'utente vede un solo menu compatto.
I bandi si aprono con **Concorsi**, cinque per pagina; `/help` mostra le istruzioni.
Le notifiche sono inizialmente disattivate. Dopo aver scelto i filtri, usare
`/notifiche on`. L'attivazione considera soltanto gli eventi successivi:
l'archivio iniziale resta consultabile con `/concorsi` e non viene inviato
come migliaia di nuovi risultati.

`Ctrl+C` arresta il processo. Riavviare lo stesso comando per mantenere utenti,
filtri, cursore e coda. L'arresto durante un invio può lasciare un esito ambiguo,
descritto sotto. Il bot non si registra come servizio Windows e richiede un PC
acceso e connesso. Un webhook attivo viene segnalato; il programma non lo
cancella automaticamente.

## Comandi disponibili

| Comando | Funzione |
|---|---|
| `/start`, `/help`, `/menu` | Guida con pulsanti; riepilogo al primo accesso |
| `/filtri` | Pagina iniziale: Nazionali, Territori incerti, Regioni e Provincia |
| `/filtri regione Sardegna, Lazio` | Imposta le regioni richieste |
| `/filtri provincia SS, CA` | Imposta le province richieste |
| `/filtri parole amministrativo` | Richiede tutte le parole indicate nel titolo |
| `/filtri nazionali on` o `off` | Inclusione dei nazionali nei filtri geografici |
| `/filtri incerti on` o `off` | Inclusione dei territori non risolti |
| `/filtri reset` | Ripristina tutti i territori e rimuove le parole |
| `/concorsi` | Elenco dei bandi aperti compatibili, cinque per pagina |
| `/seguiti` | Elenco dei bandi salvati, inclusi quelli chiusi |
| `/segui ID`, `/nonseguire ID` | Aggiunge o rimuove un bando dai seguiti |
| `/nascondi ID` | Nasconde un bando; `/segui ID` lo rende nuovamente visibile |
| `/notifiche` | Stato e pulsanti per attivare/disattivare |
| `/stato` | Ultima riuscita M1, in Europe/Rome, e schede pendenti |
| `/privacy` | Dati conservati, accessi e cancellazione |
| `/elimina` | Richiede conferma tramite pulsante, valida per cinque minuti |

I pulsanti delle schede permettono di aprire inPA, seguire o nascondere il bando.
Un link ufficiale di dettaglio incollato nella chat consulta la versione già
archiviata. I link vengono validati per schema, dominio, percorso e ID:
non attivano richieste verso URL arbitrari e non scaricano allegati.
Un bando assente dall'archivio resta consultabile sul sito ufficiale.

Regioni e province sono unite in OR. Per restringere alla sola provincia,
non selezionare contemporaneamente la regione. Il filtro territoriale usa
l'area dichiarata dalla fonte, che può non rappresentare la sede di lavoro.
Un'area regionale non viene trasformata in una provincia. Senza preferenze
geografiche vengono considerati tutti i territori; le opzioni nazionali e
incerti governano la selezione anche senza una regione/provincia scelta.
I territori incerti sono disattivati per i nuovi utenti e dopo `/filtri reset`.
La migrazione cambia solo gli utenti che non hanno mai modificato i filtri;
le scelte esplicite già salvate sono conservate.

Da **Provincia** si sceglie prima la regione, poi una o più province. La regione
usata per navigare non diventa automaticamente una preferenza geografica.
Toccare di nuovo una regione o provincia selezionata la deseleziona.

Il comando `/privacy` resta consultabile digitandolo, per compatibilità; Privacy
è stato rimosso dal menu a pulsanti e dall'elenco comandi nativo. Telegram mostra
il proprio elenco quando si digita `/` o si preme **Menu**, configurato con
[`setMyCommands`](https://core.telegram.org/bots/api#setmycommands) e
[`setChatMenuButton`](https://core.telegram.org/bots/api#setchatmenubutton).

I bandi dichiarati aperti con una scadenza esplicita già trascorsa vengono
esclusi da `/concorsi`. Una data senza ora rimane valida per l'intera data
locale pubblicata, senza inventare un orario di scadenza. `/seguiti` mantiene
la consultazione dei bandi salvati anche dopo la chiusura.

## Accesso e isolamento

Il massimo predefinito è quattro utenti, amministratori inclusi. Si può cambiare
con `INPA_TELEGRAM_MAX_USERS` in `.env`, tra 1 e 10. I comandi operano soltanto
in chat private in cui l'ID della chat coincide con l'ID del mittente.
Gruppi, canali, bot e utenti non autorizzati non possono consultare l'archivio.
Un `/start` non autorizzato registra solo una richiesta di accesso; il bot
resta silenzioso finché l'amministratore non approva.

Comandi amministratore:

| Comando | Funzione |
|---|---|
| `/richieste` | Elenca gli ID delle richieste di accesso |
| `/approva ID` | Autorizza un utente entro il limite configurato |
| `/revoca ID` | Revoca un utente e cancella i suoi dati locali |
| `/consegne` | Conta consegne in coda, riuscite, fallite o ambigue |

Verificare l'identità della persona prima di approvare un ID. Dopo
l'approvazione, il nuovo utente deve inviare nuovamente `/start`.
L'amministratore iniziale è configurato localmente: nessuno può diventare
amministratore mediante un comando pubblico. Rimuovere un amministratore
da `.env` e riavviare revoca anche il suo accesso e cancella i dati locali.
Le variabili d'ambiente già impostate prevalgono sul file `.env`.

Il database appartiene a un solo bot, identificato dal suo ID numerico. Un
token ruotato per lo stesso bot è compatibile; un bot diverso richiede un
archivio distinto. L'applicazione non stampa token né eccezioni di rete che
potrebbero contenere URL con credenziali.

## Consegna e deduplicazione

I pulsanti di menu, filtri, notifiche e stato aggiornano l'ultimo pannello
per utente. Un comando scritto, come `/menu` o `/start`, invia invece un nuovo
pannello in fondo alla chat, anche se il contenuto non cambia. Il bot
conserva l'ID del messaggio e l'hash del contenuto in `telegram_panels`, con
versione additiva `m2_ui_schema_version=1`. Il riferimento viene cancellato insieme
all'utente. L'ID è quello restituito da Telegram, mai un ID fornito dal comando.

Un pannello aperto dai pulsanti e invariato non richiede chiamate di rete. Quello cambiato usa
[`editMessageText`](https://core.telegram.org/bots/api#editmessagetext). Più
pannelli in coda dello stesso utente vengono sostituiti dall'ultimo. Gli altri
utenti, i bandi e le notifiche non vengono coinvolti. Se Telegram conferma che
il vecchio messaggio è stato eliminato o non è modificabile, si crea un pannello
sostitutivo. I vecchi messaggi già presenti in chat non vengono cancellati.
La coda distingue `panel` (modifica) e `panel_fresh` (nuova risposta); entrambi
aggiornano il riferimento solo dopo una consegna riuscita. Se un click sostituisce
un comando scritto ancora in coda, conserva la richiesta di una risposta nuova.

Un errore di rete durante una modifica può essere ritentato sullo stesso ID
senza creare messaggi nuovi. L'invio di un pannello nuovo resta invece soggetto
agli esiti ambigui descritti sotto: non viene ritentato automaticamente.

Il cursore degli aggiornamenti Telegram, gli effetti del comando e le risposte
in coda vengono salvati nella stessa transazione SQLite. Un aggiornamento già
elaborato non viene rieseguito. Il cursore della Bot API viene inviato nella
successiva richiesta di polling, secondo la [documentazione ufficiale](https://core.telegram.org/bots/api#getupdates).

Ogni utente ha un cursore separato sugli eventi M1. L'avanzamento e le notifiche
corrispondenti vengono salvati insieme. Le chiavi `(utente, evento)` impediscono
duplicati dopo il riavvio del collettore. Gli eventi `baseline` vengono ignorati.
I nuovi bandi devono essere aperti e compatibili; le modifiche arrivano per i
bandi compatibili o seguiti. Nascondere un bando ne sopprime gli avvisi.

Cambiare i filtri o disattivare le notifiche annulla le notifiche ancora in coda
per quell'utente. I nuovi filtri si applicano agli eventi successivi e al
riepilogo consultabile; i vecchi bandi non vengono ribattezzati “nuovi”.
Ripetere `/notifiche on` quando sono già attive mantiene il lavoro in coda.

Stati dell'outbox: `queued`, `sending`, `sent`, `failed`, `cancelled`, `ambiguous`.
Una risposta di successo salva l'ID del messaggio; i rifiuti espliciti sono
registrati come fallimenti. `Retry-After` salva un termine di attesa globale,
valido anche dopo il riavvio. Il sender invia al massimo un messaggio al secondo.
Comandi e invii vengono eseguiti da task separati.

Un timeout dopo `sendMessage` o un crash durante l'invio può significare che
Telegram ha già accettato il messaggio. In quel caso M2 registra `ambiguous`
e non ritenta automaticamente. Dopo un errore di rete dell'invio sospende
anche le altre consegne per 60 secondi. Non promette una consegna “exactly once”.
Gli amministratori vedono il conteggio con `/consegne`; gli utenti possono
sempre consultare `/concorsi` e `/seguiti`. Non esiste ancora un comando di
reinoltro manuale delle consegne ambigue.

## Persistenza e privacy

`m2_schema_version=1` aggiunge tabelle `telegram_*` senza cambiare il formato
degli snapshot M0/M1. Un lock `.telegram.lock`, distinto da `.sync.lock`,
impedisce due poller sullo stesso archivio. M1 continua a funzionare sullo
stesso database. Il modello di durabilità rimane quello documentato per M1:
disco locale, WAL e `synchronous=NORMAL`; la perdita di alimentazione non è
stata collaudata.

Si conservano ID, chat, filtri, seguiti/nascosti, cursori e consegne. I dati sono
leggibili da chi amministra il PC. I messaggi passano attraverso Telegram;
nessun profilo viene inviato a un modello esterno. I titoli di studio e altri
dati personali per la valutazione dei requisiti non vengono ancora raccolti.

Consegne concluse e richieste di accesso vengono eliminate dopo sette giorni,
con pulizia periodica ogni cinque minuti. Le consegne pendenti e ambigue restano
finché vengono gestite o si elimina l'utente. `/elimina` e `/revoca` rimuovono
anche le righe personali collegate, senza cancellare i bandi pubblici. I messaggi
già ricevuti o in corso di invio e le eventuali copie manuali del database
richiedono gestione separata. L'installazione locale non configura backup
automatici. La distribuzione VPS conserva gli ultimi sette backup riusciti:
dettagli e copie residue sono in [VPS.md](VPS.md).

## Validazione e limiti

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check src/inpa_bot/telegram src/inpa_bot/cli.py tests/telegram scripts/benchmark_telegram.py
.\.venv\Scripts\python.exe scripts/benchmark_telegram.py --records 1789
```

I test coprono autorizzazione, isolamento, paginazione, cursori, rollback,
riavvio, filtri, revoca/cancellazione, deduplicazione, invii falliti e ambigui,
`Retry-After`, token non esposti e compatibilità M1. Una prova usa l'SDK reale
su un trasporto simulato, senza contattare Telegram. I risultati sono nel
[rapporto M2](../reports/m2_validation.md).

Avvio e autenticazione sulla VPS sono verificati nel
[report di migrazione](../reports/vps_validation.md). Resta da provare il menu
dalla chat dopo il trasferimento. Restano nelle fasi successive: profili con titoli di studio,
verifica documentale M3, riepiloghi giornalieri programmati, orari di silenzio,
promemoria e gestione operativa M4. Il blocco sugli allegati osservato in M0
non viene aggirato. Il collaudo M1 di 48 ore può proseguire separatamente.

Il runtime usa [python-telegram-bot 22.8](https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html),
installato come dipendenza opzionale. `getUpdates`, `sendMessage` e i pulsanti
seguono la [Telegram Bot API](https://core.telegram.org/bots/api).
