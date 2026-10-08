# Filtri e navigazione Telegram — 8 ottobre 2026

La VPS esegue la nuova interfaccia: pagina iniziale Filtri, selezione delle
province tramite regione, menu comandi nativo e riuso del pannello. Privacy è
assente dai pulsanti e dall'elenco comandi. Il comando diretto `/privacy` resta
disponibile per compatibilità e per consultare l'informativa.

## Validazione eseguita

| Controllo | Risultato |
|---|---|
| Suite completa Windows, Python 3.13.2 | 430 passati, 5 saltati, 4 warning; 11,31 s |
| Suite completa VPS, Python 3.14.4 | 430 passati, 5 saltati, 4 warning; 1,37 s |
| Ruff check | Superato su Windows e VPS |
| Ruff format, Telegram e relativi test | 18 file conformi |
| `pip check` VPS | Nessuna incompatibilità |
| Backup prima dell'aggiornamento | Riuscito |
| Autenticazione dopo riavvio bot | `telegram_started`, nessun recupero ambiguo |
| API reale `getMyCommands`, italiano | menu, concorsi, filtri, seguiti, notifiche, stato, help, start, elimina |
| API reale `getChatMenuButton` | Tipo `commands` |

I test saltati richiedono rete e sono opt-in. I warning sono quelli già
documentati sul futuro tipo `retry_after` del SDK. La verifica dell'elenco
comandi usa chiamate di lettura e non invia messaggi artificiali agli utenti.

## Invarianti

- Nazionali e territori incerti si possono cambiare dalla pagina iniziale;
  gli interruttori valgono anche senza regione o provincia selezionata.
- I territori incerti sono disattivati per i nuovi utenti e dopo reset.
  La migrazione cambia soltanto utenti senza modifiche ai filtri e si applica
  una volta; conserva le scelte esplicite e gestisce il vecchio default SQLite.
- Sfogliare le province di una regione non seleziona quella regione. I filtri
  regionali e provinciali restano uniti in OR, con avviso quando entrambi presenti.
- I pannelli in coda vengono sostituiti soltanto per lo stesso utente. La
  registrazione dell'ID e dell'hash del pannello riuscito è transazionale con
  lo stato della consegna; il riferimento sopravvive al riavvio e viene rimosso
  dalla cancellazione dell'utente.
- Un pannello invariato non effettua invii; uno diverso modifica il messaggio
  noto. L'ID non arriva da input arbitrario. Un rifiuto esplicito perché il
  messaggio non esiste più permette un nuovo invio. Un timeout del nuovo invio
  rimane ambiguo, senza reinvio automatico.

I casi comprendono riavvio, doppi tocchi, coalescenza per utente, timeout durante
modifica, messaggio già modificato o cancellato, invio sostitutivo incerto,
cancellazione utente, migrazione del default e flag senza filtri geografici.
I test con SDK reale verificano serializzazione di `editMessageText`, pulsanti,
comandi pubblici e comandi riservati alla chat amministratore. Le autorizzazioni
restano nel dispatcher: il menu nativo non concede accessi.

## Pubblicazione e continuità

È stato fermato e riavviato soltanto `inpa-bot`. `inpa-watch` è rimasto attivo
dalle 20:47:51 UTC, cioè 22:47:51 Europe/Rome, con zero riavvii. L'ultimo ciclo
verificato è terminato alle 22:56:37 locali senza errori, con 1.789 bandi e zero
schede pendenti. L'osservazione di 48 ore sulla VPS raggiunge la durata prevista
il 10 ottobre alle 22:47:51 locali: il suo esito non è ancora completato.
Il PC non mantiene più processi locali `inpa_bot` necessari al servizio.

Il database attivo resta `/var/lib/inpa-bot/inpa.sqlite3`; non è stato
sovrascritto durante questo aggiornamento. Il formato base M2 resta versione 1;
la tabella additiva `telegram_panels` dichiara `m2_ui_schema_version=1` e la
migrazione dei default è marcata da `m2_filter_defaults_v2`.

## Limiti

Il bot aggiorna il pannello di navigazione; non cancella a tempo le schede dei
bandi, le notifiche o i vecchi messaggi. Telegram mantiene il messaggio
modificato nella posizione originale della chat. Il caso raro di invio nuovo
con risposta persa resta soggetto al limite di deduplicazione già documentato.
L'aspetto dell'elenco nativo dipende dal client Telegram e può richiedere di
riaprire la chat. La prova visiva finale spetta all'utente con `/menu` e Filtri.
Non sono state introdotte ottimizzazioni né nuove dichiarazioni di prestazioni.

## Testi essenziali

La successiva revisione, richiesta dall'utente, riduce menu, filtri, conferme,
stato e schede. `/start` invia un solo pannello; i risultati si aprono da
Concorsi e le istruzioni da `/help`. Gli interruttori mostrano il proprio stato
senza ripeterlo nel testo. Resta l'avviso quando regioni e province si sommano.
Le schede conservano titolo, ente, area dichiarata, scadenza, stato e richiamo
alla verifica di sede e requisiti. Il collegamento ufficiale rimane nel
pulsante Apri inPA.

Verifica dopo la revisione: 430 test passati, 5 saltati, 4 warning, su Windows
in 10,30 secondi e sulla VPS in 1,40 secondi. Ruff e `pip check` VPS passano.
Il test dell'onboarding verifica un solo pannello e risultati consultabili
su richiesta. Il benchmark script ora richiede esplicitamente `/concorsi`
dopo `/start`, per continuare a misurare la prima pagina; non è stato eseguito
un nuovo benchmark né modificato il report storico.

## Correzione delle risposte ai comandi scritti

L'8 ottobre il bot risultava attivo, ma gli ultimi quattro comandi registrati
alle 21:30–21:31 UTC aggiornavano tutti il messaggio 54. Un contenuto identico
veniva segnato come consegnato senza chiamare Telegram. Questo lasciava il
comando senza una risposta visibile in fondo alla chat, anche dopo aver
cancellato il vecchio messaggio.

I comandi scritti ora accodano `panel_fresh`; i pulsanti continuano a usare
`panel`. Entrambi salvano il riferimento solo dopo il successo. Un click che
sostituisce un comando ancora in coda conserva la richiesta di una risposta
nuova. Gli invii nuovi con esito incerto non vengono ritentati automaticamente.

Windows: 436 test passati, 5 saltati, 4 warning SDK in 9,95 secondi; Ruff e
`pip check` riusciti. Sei casi aggiunti coprono comandi identici dopo riavvio,
click che supera un comando e timeout durante una risposta nuova. La verifica
visiva della chat richiede ancora `/menu` da parte dell'utente.

È stato riavviato soltanto il servizio Telegram. Il monitoraggio conserva
l'avvio originale e zero riavvii: la durata di osservazione non è stata ridotta.
