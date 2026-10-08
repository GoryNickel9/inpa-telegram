# Bot Telegram privato per concorsi inPA
## Specifica funzionale e tecnica — recupero dati come requisito bloccante

**Versione:** 1.0  
**Data della verifica preliminare:** 19 settembre 2026  
**Destinatari del servizio:** Luca e altre 2–3 persone, quindi 3–4 utenti complessivi.  
**Destinatario del documento:** sviluppatore o agente coder incaricato dell’implementazione.  
**Stato:** specifica pronta; verifica delle fonti parziale; integrazione completa con inPA non ancora collaudata.

> **Regola di progetto:** prima di sviluppare notifiche, profili e valutazione dei requisiti, dimostrare come ottenere in modo ripetibile l’elenco dei bandi, i dettagli e gli allegati. La lettura di alcune schede non dimostra la completezza del monitoraggio.

Le indicazioni contrassegnate come **verificate** si riferiscono alle osservazioni descritte nel documento, non a un connettore già implementato. Le frequenze, le soglie e lo stack proposti sono scelte iniziali di progetto, modificabili dopo le misurazioni. I riferimenti `[S1]`–`[S9]` sono raccolti in fondo.

---

## 1. REQ-001 — Recuperare i dati di inPA prima di costruire il bot

### 1.1 Obiettivo della prima milestone

Realizzare un piccolo programma autonomo di acquisizione, senza Telegram e senza modello linguistico. Deve dimostrare l’intera catena:

```text
Ricerca pubblica → pagine di risultati → ID dei bandi
                 → dettagli → collegamenti agli allegati
                 → documenti leggibili → snapshot persistente
                 → seconda acquisizione → confronto delle versioni
```

Il requisito è soddisfatto solo quando esistono richieste riproducibili, risposte reali, controlli di completezza e un rapporto dei test. Un endpoint trovato nel codice di terzi è una pista concreta, non una prova del suo funzionamento attuale.

**Non è sufficiente:** recuperare soltanto notizie della homepage, interrogare un motore di ricerca, estrarre un elenco fisso di URL, ottenere una singola pagina di risultati o mostrare qualche bando di esempio.

### 1.2 Esito delle verifiche già svolte

| Oggetto | Esito al 19/09/2026 | Limite della verifica |
|---|---|---|
| Funzioni di ricerca per Regione/Provincia | Documentate nelle FAQ ufficiali `[S1]` | I parametri HTTP dei filtri non sono stati ricavati dal traffico del sito. |
| Pagina pubblica di ricerca | Aperta; visibili i controlli di ricerca `[S2]` | L’estrazione testuale dello strumento non ha restituito i risultati. Non dimostra che l’HTML grezzo sia privo di dati, né che il sito sia guasto. |
| Dettaglio di un bando locale | Aperto senza credenziali fornite dall’utente `[S3]` | Campione singolo, non validazione generale di un parser. |
| Seconda scheda, multiregionale | Aperta; presenti dati territoriali, scadenza e allegati `[S4]` | Non verifica il comportamento del filtro provinciale. |
| PDF collegato alla prima scheda | Aperto e letto: 15 pagine; controllate visivamente le pagine 1 e 5 `[S5]` | Non è stato realizzato un downloader locale né misurata la copertura di tutti i formati. |
| Collegamento a un allegato DOCX | Presente nella prima scheda `[S3]` | Il contenuto DOCX non è stato scaricato o analizzato. |
| Endpoint di ricerca paginata | Individuato nel sorgente di un progetto terzo `[S6]` | Nessuna risposta JSON ottenuta direttamente da inPA in questa verifica. |
| Richiesta POST all’endpoint candidato | Tentata con Python, senza autenticazione | Fermata dalla risoluzione DNS dell’ambiente prima di una risposta HTTP. |
| API pubbliche documentate ufficialmente | Non individuate nei materiali consultati | Non equivale a dimostrarne l’inesistenza. |
| `robots.txt` e note legali | Non recuperati con successo dallo strumento | Permessi e condizioni per il monitoraggio restano da verificare. |
| Informativa privacy di inPA | Consultata `[S7]` | Non costituisce da sola un’autorizzazione al riuso o al polling. |
| Paginazione completa e funzionamento continuativo | Non collaudati | Sono parte obbligatoria di M0. |

**Conclusione tecnica attuale:** è verificata la consultabilità di due schede e di un PDF; è disponibile una pista precisa per la ricerca strutturata. Non è ancora verificato un flusso automatico completo, stabile e autorizzato per questo impiego.

### 1.3 Evidenza del limite dell’ambiente

Il tentativo diretto di ricerca è stato eseguito il **19/09/2026 alle 17:45:42 UTC**, con metodo `POST`, corpo di ricerca e nessuna credenziale. L’errore è stato:

```text
Failed to resolve 'portale.inpa.gov.it'
[Errno -3] Temporary failure in name resolution
```

Non è stato ricevuto uno stato HTTP. Non classificare questo risultato come `401`, `403`, `404`, blocco imposto da inPA o prova che l’endpoint non funzioni. Anche il tentativo di scaricare la homepage con Python si è fermato su un errore DNS. La consultazione tramite lo strumento web è un percorso distinto e può utilizzare contenuti indicizzati o memorizzati: non sostituisce un test dalla VPS di destinazione.

---

## 2. Superfici di accesso individuate

### 2.1 Ricerca strutturata: candidato prioritario da verificare

Nel progetto pubblico `davidebr90/concorsi-bridge`, file `scripts/sync.mjs`, è presente una richiesta di questa forma `[S6]`. Qui `size` è ridotto a `20` per la prova; il sorgente consultato usa `100`:

```http
POST https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better?page=0&size=20
Content-Type: application/json

{"text":"","status":["OPEN"]}
```

Lo stesso sorgente usa `provinciaCodice`, per esempio:

```json
{"text":"","status":["OPEN"],"provinciaCodice":"SS"}
```

**Sassari è soltanto un caso di prova:** non è un filtro già scelto da tutti gli utenti.

Il codice terzo legge `content` e `totalElements` dalla risposta, incrementa `page` partendo da zero e usa `size` per la dimensione della pagina. Non imposta un header di autorizzazione nella chiamata mostrata. Tutto questo descrive il codice consultato; **non certifica il contratto attuale del servizio**.

Il medesimo codice tratta, tra gli altri, questi campi:

```text
id, codice, titolo, descrizione, figuraRicercata,
sedi, entiRiferimento, categorie, settori, tipoProcedura,
numPosti, dataPubblicazione, dataScadenza, linkReindirizzamento
```

Il coder deve verificare nomi, tipi, valori nulli e significato su risposte reali. Il parametro per la regione e gli eventuali filtri temporali non sono stati accertati. Non inventarli.

**Precisazioni obbligatorie:**

- Un percorso sul dominio ufficiale non equivale a un’API ufficialmente supportata per integrazioni di terzi.
- La capacità di ottenere una risposta non dimostra da sola il permesso di usare il servizio periodicamente.
- `OPEN` è un valore usato nel sorgente esaminato, non un elenco completo degli stati. Un monitoraggio limitato a questo stato non copre automaticamente bandi non ancora aperti, revocati o già chiusi ma seguiti.
- Non copiare dal progetto terzo euristiche sui titoli di studio o elenchi territoriali hardcoded: qui servono verifiche documentali e anagrafiche versionate.

### 2.2 Dettaglio pubblico del bando

Formato osservato sulle schede ufficiali `[S3][S4]`:

```text
https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=<ID>
```

Usare gli ID effettivamente restituiti dalla fonte; non enumerarli per tentativi. Conservare l’ID come stringa opaca e il collegamento originale.

Le schede esaminate contengono metadati e collegamenti ai documenti. L’adattatore dovrà preferire i dettagli strutturati, se individuati e verificati, oppure estrarre i dati dalla pagina pubblica. Non dichiarare disponibili ulteriori endpoint di dettaglio senza osservarli.

### 2.3 Allegati: collegamento realmente aperto

Dalla prima scheda è stato seguito con successo questo URL `[S5]`:

```text
https://portale.inpa.gov.it/api/media/83f5fa66-9038-4e7d-b09f-11e86f3956be
```

Il documento restituito dallo strumento è un PDF di 15 pagine. Il percorso non termina in `.pdf`: **il downloader non deve basarsi soltanto sull’estensione dell’URL**.

Ricavare gli URL dai collegamenti pubblicati nella scheda o dalle risposte verificate. Non assumere che tutti gli allegati usino sempre lo stesso host o formato.

### 2.4 Campioni utilizzati

| Campione | Identificativo e osservazione utile |
|---|---|
| Operatori, Conservatorio di Vibo Valentia | `d3d8d8d7a82e40afbd75a26841bc9070`. Scheda locale con un PDF e un collegamento DOCX `[S3]`. |
| Assistenti, Ministero della Difesa | `01c11fcf96d04a7092d039e5f1a66224`. Scheda multiregionale comprendente la Sardegna e con una pubblicazione successiva di FAQ `[S4]`. |

Entrambi risultano chiusi nelle schede consultate: servono come campioni tecnici, non come opportunità attuali per gli utenti. Per l’accettazione M0 occorrono anche bandi realmente aperti al momento del test.

Nel PDF del primo campione il requisito formativo è nella sezione dei requisiti speciali, articolo 3, pagina fisica 5 di 15; la scheda sintetica non lo espone con lo stesso dettaglio `[S3][S5]`. Questo è un caso di prova per l’estrazione dal documento, non una valutazione del profilo di Luca.

---

## 3. M0 — Procedura di verifica e scelta del metodo

### 3.1 Controllo preliminare delle condizioni

Consultare e registrare note legali, eventuale licenza, indicazioni sull’accesso automatizzato e `robots.txt` degli host coinvolti. Conservare URL e data della consultazione. Se una pagina non è recuperabile, registrare il limite senza attribuirle un contenuto presunto.

Distinguere tre verifiche: **accessibilità tecnica**, **condizioni di utilizzo**, **gestione dei dati personali**. Il superamento della prima non risolve automaticamente le altre due. Prima del polling di produzione, chiarire eventuali condizioni incompatibili o ambigue con il gestore; il contatto informativo è riportato nelle pagine ufficiali `[S1]`.

Nessuna elusione di autenticazione, CAPTCHA, limiti o blocchi. Non utilizzare proxy rotanti, account personali o credenziali SPID/CIE per trasformare un percorso riservato in una fonte del bot.

### 3.2 Prova dalla macchina di destinazione

Su PC o VPS con accesso Internet, eseguire una richiesta di ricerca di dimensione ridotta e salvare risposta e metadati. Verificare prima la connettività e distinguere errori DNS/TLS da errori applicativi.

Se la richiesta candidata funziona, confrontarla con il traffico prodotto dalla ricerca pubblica in un browser non autenticato. Se non funziona, non indovinare decine di endpoint: aprire la pagina pubblica, osservare le richieste di rete e individuare il meccanismo effettivo.

Playwright supporta l’osservazione di richieste e risposte di rete `[S8]`. Può essere usato per questa fase o per un adattatore browser qualora necessario e consentito. La presenza di contenuti dinamici è una pista da verificare, non una conclusione già provata dalla sola estrazione testuale.

### 3.3 Ordine di preferenza

| Metodo | Condizione per adottarlo |
|---|---|
| API o feed ufficiale documentato | Esistenza, copertura e condizioni verificate. |
| Richieste pubbliche strutturate del portale | Contratto osservato, riprodotto senza credenziali e condizioni chiarite. |
| Estrazione da pagine pubbliche | Risultati, dettagli e paginazione ricostruibili e verificabili. |
| Browser automatizzato | Necessario per il rendering pubblico; testato senza aggirare controlli. |

Non adottare un aggregatore privato o la ricerca web come unica fonte del monitoraggio. Eventuali indizi esterni devono riportare al documento ufficiale.

### 3.4 Test minimi di accettazione

Le quantità seguenti sono obiettivi di collaudo proposti, non capacità già dimostrate.

| Test | Criterio di superamento |
|---|---|
| M0-01 — Accesso | Ricerca, dettaglio e almeno un allegato ottenuti senza identità digitale o sessioni personali. |
| M0-02 — Paginazione | Almeno due pagine realmente differenti e una ricerca circoscritta completata fino alla fine; conteggi, duplicati e variazioni durante la lettura spiegati. |
| M0-03 — Filtri | Almeno due regioni e due province confrontate con l’interfaccia pubblica; se un filtro non è disponibile, strategia locale documentata e verificata. |
| M0-04 — Campione | Almeno 20 schede di enti e procedure diversi, includendo bandi aperti, territoriali e multiregionali. |
| M0-05 — Documenti | Almeno 5 PDF reali scaricati; lettura corretta o errore di estrazione esplicito. Formati ulteriori dichiarati come supportati solo dopo test specifici. |
| M0-06 — Aggiornamenti | Due acquisizioni dei medesimi ID con confronto deterministico; test controllato di proroga, aggiunta allegato e sostituzione di contenuto. |
| M0-07 — Stabilità | Osservazione dalla VPS per 48 ore con la cadenza concordata; esiti, tempi e volume richieste registrati. Non è una prova già eseguita. |
| M0-08 — Errori | Timeout, risposta HTML al posto di JSON, `429`, `5xx` e pagine mancanti non producono un falso “nessun concorso”. |
| M0-09 — Tracciabilità | Esempi sanitizzati, schema osservato, versioni, data del test e comando riproducibile nel repository. |
| M0-10 — Condizioni | Esito documentato della verifica di condizioni e limiti; nessun punto ostativo irrisolto per l’uso previsto. |

Se non avviene una rettifica reale nelle 48 ore, testare le modifiche su copie locali controllate. Etichettare il test come simulato: non dichiarare osservato dal vivo un evento non accaduto.

**Passaggio a M1:** tutti i criteri applicabili superati; nessuna lacuna non gestita nel recupero degli elenchi o degli allegati. Un limite di campo, come la provincia non esplicita, può essere accettato solo con un comportamento conservativo documentato, non nascondendolo.

### 3.5 Consegne di M0

```text
reports/m0_data_access.md          # esiti reali e limiti residui
reports/source_contract.json      # contratto osservato, non ipotizzato
reports/access_conditions.md      # fonti e valutazione delle condizioni
scripts/probe_inpa.py             # prova riproducibile e limitata
samples/                         # piccoli campioni sanitizzati
src/inpa_bot/source/              # adattatore indipendente dal bot
tests/source/                    # test offline e integrazione opt-in
```

Non includere cookie, token, dati dei candidati o interi dataset nel repository. Le consegne sopra sono da implementare: non sono file già prodotti insieme a questa specifica.

---

## 4. Contratto interno dell’acquisizione

L’applicazione deve dipendere da un’interfaccia interna, non dai nomi correnti dei campi inPA. I nomi seguenti sono del progetto e **non descrivono API ufficiali**:

```python
class SourceAdapter:
    def search(self, filters, page_cursor): ...
    def fetch_notice(self, notice_id): ...
    def list_attachments(self, notice_id): ...
    def fetch_attachment(self, attachment_ref): ...
```

Ogni risposta di ricerca deve esporre record, indicatore di completezza, eventuale cursore successivo, conteggio dichiarato dalla fonte se presente e anomalie. Non forzare la semantica a pagine numeriche qualora il servizio usi un altro meccanismo.

### 4.1 Dati minimi da conservare

| Oggetto | Informazioni |
|---|---|
| Bando | ID originale, URL ufficiale, titolo, descrizione, enti, tipo di procedura, stato originale e normalizzato, posti totali se disponibili. |
| Date | Pubblicazione, apertura, scadenza, eventuale aggiornamento della fonte, prima e ultima osservazione del bot. |
| Territori | Valore originale, regione/provincia/comune normalizzati quando provati, origine e grado di certezza. |
| Profilo o codice di concorso | Sottoinsieme dei posti, requisiti e sedi pertinenti a quel codice, quando il bando contiene più profili. |
| Allegato | ID o URL originale, nome, data pubblicata, MIME rilevato, dimensione, hash dei byte, versione e stato di estrazione. |
| Evidenza | Documento, versione, articolo, pagina fisica o ancoraggio testuale; testo originale necessario alla verifica. |
| Acquisizione | Ora, durata, metodo, esito, pagine attese/lette, record nuovi/modificati ed errori. |

Non sostituire informazioni mancanti con valori plausibili. Distinguere `dataPubblicazione` dalla data di apertura: la mappatura deve seguire il significato verificato, non il solo formato della data.

Conservare orari originali e normalizzati. Usare `Europe/Rome` per l’interfaccia italiana, gestendo l’ora legale; un orario senza fuso richiede un’interpretazione documentata. Non inventare le 23:59 quando è pubblicata soltanto una data.

### 4.2 Identità e versionamento

Usare l’ID della fonte per l’identità; non deduplicare soltanto sul titolo. Separare l’hash dei metadati significativi dall’hash dell’HTML completo, per evitare falsi aggiornamenti dovuti a elementi di pagina.

Per gli allegati confrontare anche i byte: lo stesso URL può richiedere una nuova verifica. Eventuali `ETag` e `Last-Modified` vanno usati se effettivamente presenti, senza considerarli garantiti. Conservare un manifesto delle versioni documentali usate per ogni valutazione.

Una data osservata dal bot non è una data dichiarata da inPA. Un record assente da una ricerca non è automaticamente revocato o eliminato.

---

## 5. Strategia di sincronizzazione

### 5.1 Prima acquisizione

Costruire l’archivio dei bandi ancora aperti nel perimetro concordato. Non inviare centinaia di notifiche arretrate: consegnare a ciascun utente un riepilogo iniziale, navigabile per pagine.

Come progetto iniziale, preferire una raccolta condivisa di metadati sufficientemente ampia da non perdere bandi nazionali, e scaricare i documenti pertinenti all’unione dei filtri degli utenti. La convenienza di una scansione nazionale o di ricerche territoriali deve essere misurata in M0.

### 5.2 Esecuzioni successive

Proposta iniziale: rilevamento dei nuovi bandi ogni 60 minuti, una sola acquisizione condivisa per tutti gli utenti. Dettagli dei bandi seguiti ricontrollati con una cadenza adeguata al volume; riconciliazione completa del perimetro almeno giornaliera. Sono impostazioni da validare rispetto a carico e condizioni della fonte.

Non basarsi esclusivamente sulla data di pubblicazione: una proroga può riguardare un vecchio bando. Mantenere distinti il controllo delle nuove pubblicazioni e quello dei bandi già seguiti, anche dopo la chiusura delle domande per un periodo configurabile.

Se il servizio consente filtri temporali, usare una finestra sovrapposta e deduplicare gli ID. Se l’elenco può cambiare durante la paginazione, registrare le incongruenze e riconciliare: il totale iniziale non è necessariamente uno snapshot immutabile.

Non avanzare il punto di ripresa globale dopo una scansione incompleta. Non perdere record perché il loro ID era già stato visto: gli ID esistenti devono poter aggiornare i contenuti.

### 5.3 Carico e gestione degli errori

Come default prudente di prova: una richiesta per volta, intervallo minimo di 2 secondi tra richieste allo stesso host e cache locale. Questi valori non sono limiti ufficiali inPA; renderli più restrittivi quando necessario.

Applicare timeout, tentativi limitati e attese crescenti per errori temporanei; rispettare `Retry-After` quando presente. Su richieste di autenticazione, blocchi o challenge fermare il percorso interessato e segnalare il problema, senza tentare elusioni.

Il bot deve mostrare l’ultima sincronizzazione riuscita. Un errore di fonte, un PDF non letto e l’assenza di nuovi risultati sono tre condizioni differenti. Dopo errori ripetuti notificare l’amministratore una sola volta per incidente e comunicare il ripristino.

---

## 6. Filtri geografici

I filtri di Regione/Provincia sono documentati nelle FAQ `[S1]`; il loro comportamento tecnico deve essere verificato. Separare **sede dell’ente**, **sede di lavoro**, **ambito del bando** e **sede delle prove**.

Ogni utente può selezionare più regioni/province. La preferenza geografica riguarda principalmente il luogo di lavoro. Prevedere opzioni distinte per includere bandi nazionali, multiregionali o con sede ancora non specificata.

| Caso | Comportamento |
|---|---|
| Sede di lavoro esplicita e compatibile | Includere. |
| Sede esplicita fuori dai territori scelti | Escludere dalla ricerca territoriale, salvo scelta diversa dell’utente. |
| Concorso nazionale o multiregionale | Esaminare la ripartizione per codice/sede e non assegnare indistintamente tutti i posti alla provincia. |
| Solo sede dell’ente nota | Non trattarla come prova della sede lavorativa. |
| Località assente o ambigua | Segnalare “territorio da verificare”, con scelta dell’utente sulla visibilità. |

Non risolvere il filtro cercando una sigla nel titolo. Usare un’anagrafica territoriale ufficiale aggiornata e versionata, conservando alias e valori originali per cambiamenti amministrativi. Non assumere che un elenco hardcoded trovato in un altro progetto sia aggiornato.

---

## 7. Utenti e profili personali

Un unico bot, accesso su autorizzazione, conversazioni private e configurazioni indipendenti. L’identità applicativa è l’ID numerico Telegram, non lo username modificabile. Ogni comando e pulsante deve verificare l’autorizzazione e il proprietario della risorsa.

L’amministratore approva o revoca gli utenti e consulta la salute del servizio. Non serve un pannello web. L’isolamento applicativo non deve essere descritto come impossibilità tecnica per chi gestisce il server di accedere al database: spiegare il modello reale di accesso nell’informativa del bot.

### 7.1 Profilo minimo

Raccogliere inizialmente denominazione esatta dei titoli di studio posseduti, eventuale classe di laurea, territori e preferenze professionali. Chiedere progressivamente patenti, abilitazioni, iscrizioni professionali, esperienze e situazione lavorativa nella PA quando servono a un confronto.

Non limitarsi al titolo di studio più alto. Registrare le date di conseguimento e scadenza quando rilevanti. Un requisito deve essere verificato rispetto alla data prevista dal bando, non soltanto rispetto a oggi.

Per ogni campo distinguere **dichiarato posseduto**, **dichiarato non posseduto** e **non dichiarato**. I dati mancanti non sono risposte negative. Ogni modifica crea una nuova versione del profilo e può rivalutare i bandi ancora pertinenti.

### 7.2 Minimizzazione

Non richiedere credenziali SPID/CIE, codice fiscale, indirizzo completo, documenti d’identità o CV integrale per il funzionamento base. Per un vincolo d’età chiedere la minima informazione sufficiente al caso concreto.

Nella prima versione non raccogliere informazioni su salute, condanne, procedimenti o dettagli delle categorie protette. Mostrare le clausole pertinenti come verifiche personali da svolgere sul bando. L’informativa ufficiale conferma che le procedure possono coinvolgere categorie di dati differenti `[S7]`; il bot non deve replicare indiscriminatamente il questionario della candidatura.

---

## 8. Lettura documentale e verifica dei requisiti

### 8.1 Processo previsto

```text
Documento ufficiale e versione
    → estrazione testo con riferimenti alle pagine
    → identificazione di requisiti e ambito del profilo/codice
    → struttura validata
    → confronto locale con il profilo personale
    → esito, motivazione, riferimenti e dati mancanti
```

Il modello linguistico serve per strutturare i documenti pubblici; non per decidere liberamente se una persona sarà ammessa. Non sono previsti addestramento di un modello o GPU dedicata. Provider e modello restano configurabili e non sono ancora scelti.

Analizzare una sola volta ciascun insieme di documenti invariato. La cache deve dipendere anche dalla versione del parser, dello schema e del modello/prompt; una rettifica invalida le valutazioni interessate.

### 8.2 Completezza dell’estrazione

Estrarre il testo direttamente quando disponibile. Per PDF scansionati, provare un percorso dedicato e limitato solo dopo il fallimento dell’estrazione normale; non applicare OCR indiscriminatamente. In caso di pagine illeggibili, indicare il problema e impedire un esito pienamente positivo non giustificato.

Riconoscere almeno bando, rettifica, proroga, FAQ e istruzioni. Per graduatorie ed elenchi nominativi, notificare eventualmente la pubblicazione del collegamento, senza costruire archivi di persone o inviarne il contenuto al modello.

Gestire i formati non ancora supportati come `UNSUPPORTED_FORMAT`, non come documenti privi di requisiti. Per DOCX usare riferimenti a sezioni/paragrafi, salvo una conversione con paginazione verificata; non inventare un numero di pagina.

### 8.3 Struttura dei requisiti

Ogni requisito deve indicare tipo, ambito, obbligatorietà, logica, riferimento temporale e fonte. Gestire gruppi `AND`/`OR`, alternative, eccezioni e condizioni dipendenti da altri requisiti.

Distinguere requisiti di accesso, titoli valutabili, preferenze, riserve parziali, procedure integralmente riservate e modalità della prova. Una prova di lingua non implica automaticamente il possesso preventivo di una certificazione.

Per titoli esteri, equipollenze o equivalenze, accettare solo regole supportate da una fonte ufficiale pertinente. Non applicare la regola generica “titolo superiore soddisfa sempre quello inferiore”.

Le FAQ precisano che i requisiti specifici dipendono dal bando `[S1]`. Per bandi con più codici, valutare ciascun codice separatamente: non fondere tutti i requisiti in un unico profilo impossibile.

### 8.4 Esiti

| Esito | Condizione |
|---|---|
| **Compatibile con i requisiti verificati** | Tutti i requisiti nel perimetro verificabile sono soddisfatti, con estrazione completa per quel perimetro. Esplicitare eventuali verifiche personali escluse: non è una certificazione di ammissione. |
| **Da verificare** | Dato mancante, interpretazione non risolta, requisito non supportato o documento incompleto. Indicare esattamente il punto. |
| **Requisito non soddisfatto** | Requisito obbligatorio inequivoco, pertinente al codice, con dato personale confermato incompatibile e nessuna alternativa o eccezione irrisolta. |

Non usare percentuali inventate di ammissione o un punteggio opaco. Rendere sempre visibili il perimetro del controllo, la versione del bando, la versione del profilo e i requisiti non verificati.

Se una rettifica contraddice la scheda, registrare il conflitto e il documento pertinente. Non applicare automaticamente “vince sempre il file più recente”: una FAQ più recente non è necessariamente una modifica del bando.

---

## 9. Funzioni Telegram

La prima versione comprende configurazione guidata, modifica del profilo, filtri, elenco dei bandi aperti, notifiche sui nuovi risultati, controllo di un collegamento inPA, bandi seguiti e promemoria configurabili.

Comandi proposti:

```text
/start       accesso e configurazione iniziale
/profilo     dati personali e campi mancanti
/filtri      territori e preferenze
/concorsi    ricerca nell’archivio aggiornato
/seguiti     bandi salvati
/notifiche   modalità e orari
/stato       aggiornamento e salute della fonte
/privacy     dati trattati, accessi e conservazione
/elimina     cancellazione dei dati personali con conferma
```

Ricevere anche un collegamento di dettaglio senza richiedere un comando speciale. Validarlo prima del recupero: dominio esatto e formato previsto, nessun indirizzo arbitrario o accesso a risorse interne del server.

La Telegram Bot API documenta messaggi, pulsanti e ricezione degli aggiornamenti `[S9]`. Per il progetto si propone long polling, con un solo processo che riceve gli aggiornamenti; l’utente deve prima avviare la conversazione con il bot.

### 9.1 Contenuto delle notifiche

Ogni messaggio deve distinguere titolo, ente, codice/profilo, posti pertinenti, territorio, scadenza con ora disponibile, esito del confronto, motivazioni principali, verifiche mancanti e link ufficiale. Aggiungere pulsanti per aprire, seguire o nascondere il bando e completare il profilo.

Non presentare tutti i posti di un bando multiregionale come posti disponibili nella provincia selezionata. Non chiamare “nuovo concorso” un vecchio bando diventato compatibile dopo una modifica del profilo.

Prevedere invio alla rilevazione o riepilogo giornaliero, orari di silenzio e promemoria scelti dall’utente. Una proroga deve riprogrammare i promemoria. Il riepilogo deve restare disponibile anche se Telegram non ha consegnato un avviso.

### 9.2 Deduplicazione

Usare una coda persistente di notifiche con chiave logica basata su utente, evento, bando e versione significativa. Salvare esiti e tentativi. Non reinviare tutto dopo un riavvio.

La deduplicazione applicativa non giustifica una promessa assoluta di consegna “exactly once”: un timeout dopo l’accettazione del messaggio può lasciare un esito ambiguo. Gestire e registrare esplicitamente questi casi.

---

## 10. Architettura proposta

```text
Adattatore inPA → archivio condiviso dei bandi e delle versioni
                → estrazione documentale e requisiti strutturati
                → motore locale di confronto ← profili individuali
                → coda notifiche → Telegram
```

**Stack iniziale:** Python, SQLite su disco persistente locale, servizio su VPS, libreria Telegram mantenuta e analisi documentale tramite API opzionale. Non introdurre Redis, Kubernetes, database vettoriali o pannelli web senza una necessità misurata.

Usare accessi brevi al database e serializzare le scritture; PDF e chiamate al modello non devono bloccare la ricezione dei comandi. Configurare segreti tramite ambiente o secret store, non nel repository. Bloccare le versioni delle dipendenze dopo i test.

Entità suggerite:

```text
users, profiles, user_filters,
notices, notice_versions, notice_positions,
attachments, document_versions, extracted_requirements,
user_evaluations, follows, notification_outbox, sync_runs
```

Applicare vincoli di unicità e chiavi esterne; conservare profili e valutazioni con l’ID proprietario. PostgreSQL resta una possibile migrazione futura, non un requisito per quattro utenti.

---

## 11. Sicurezza, riservatezza e costi

I testi acquisiti da pagine e documenti sono dati non affidabili: eventuali istruzioni contenute nei PDF non devono poter modificare il comportamento dell’agente, ottenere segreti o avviare chiamate di rete. Il componente di estrazione non deve avere accesso ai token Telegram o al database dei profili.

Al modello esterno inviare, per impostazione base, soltanto i testi pubblici necessari del bando. Eseguire localmente il confronto con gli utenti. Se in futuro si vuole inviare un profilo a un provider, trattarlo come modifica esplicita del progetto e dell’informativa, non come comportamento implicito.

Limitare dimensione dei download, numero di documenti per lavoro, durata dell’analisi e risorse dei parser. Convalidare MIME e firma del file, impedire path traversal e decompressioni incontrollate. Consentire redirect e host ulteriori solo dopo verifica; bloccare indirizzi locali, reti private e servizi di metadata del cloud.

Prevedere backup cifrati con prova di ripristino, accesso ristretto al server, log privi di dati personali e cancellazione del profilo con rimozione delle valutazioni associate. Documentare anche i tempi di eliminazione dai backup e i limiti della cancellazione dei messaggi già ricevuti su Telegram. Definire conservazione e informativa prima dell’utilizzo da parte degli altri utenti.

Il costo non va stimato dal solo numero di persone. Misurare bandi pertinenti, pagine documentali, aggiornamenti, token e spazio occupato. Impostare un tetto mensile per il modello e non rianalizzare documenti invariati. Non acquistare hardware dedicato sulla base di questa specifica.

---

## 12. Roadmap e criteri finali

| Milestone | Risultato verificabile |
|---|---|
| **M0 — Accesso dati** | Richieste riproducibili, paginazione, dettagli, allegati, condizioni e test descritti nella sezione 3. È il primo blocco di lavoro. |
| **M1 — Archivio e sincronizzazione** | Importazione iniziale, filtri conservativi, versionamento, errori espliciti e nessun duplicato dopo il riavvio. |
| **M2 — Bot privato multiutente** | 3–4 utenti autorizzati, chat e filtri separati, riepilogo iniziale, nuovi bandi e coda persistente. |
| **M3 — Requisiti documentali** | Estrazione con fonti, logica per codice/profilo, dati mancanti e confronti personali verificati su casi annotati. |
| **M4 — Aggiornamenti e rilascio** | Rettifiche/proroghe, promemoria, backup/ripristino, limiti di spesa e prova operativa documentata. |

Per M3 costruire un piccolo insieme di riferimento manualmente controllato, includendo requisiti alternativi, riserva parziale, mobilità, titoli ambigui, più profili e documenti illeggibili. Misurare separatamente errori di estrazione e di confronto. Nessuna esclusione automatica deve derivare da un dato mancante o da un requisito senza fonte.

Prima del rilascio verificare anche che un utente non possa leggere/modificare dati altrui attraverso comandi o callback costruite a mano; che una revoca dell’accesso sia immediata; che un riavvio non perda il lavoro; che la fonte non disponibile sia indicata come tale.

**Fuori perimetro:** candidatura automatica, pagamenti di partecipazione, accesso agli account inPA, gestione delle credenziali di identità digitale, raccolta massiva delle graduatorie nominative e certificazione giuridica dell’ammissione.

---

## Appendice A — Richieste riproducibili per il collaudo

Questi comandi sono destinati al coder, dopo il controllo delle condizioni di accesso. **Non sono presentati come già riusciti in questo ambiente.** Usare Bash e una versione di `curl` che supporti le opzioni indicate. I file di header possono contenere cookie: tenerli privati e sanitizzarli prima di condividerli.

### A.1 Scheda e PDF campione

```bash
set -euo pipefail
umask 077
mkdir -p evidence

curl --fail-with-body --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  --proto '=https' \
  --user-agent 'InpaTelegramBot-Probe/0.1' \
  --dump-header evidence/detail.headers \
  --output evidence/detail.html \
  'https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=d3d8d8d7a82e40afbd75a26841bc9070'

sleep 2

curl --fail-with-body --silent --show-error \
  --connect-timeout 10 --max-time 60 --max-filesize 26214400 \
  --proto '=https' \
  --user-agent 'InpaTelegramBot-Probe/0.1' \
  --dump-header evidence/document.headers \
  --output evidence/document.bin \
  'https://portale.inpa.gov.it/api/media/83f5fa66-9038-4e7d-b09f-11e86f3956be'
```

Controllare status, contenuto reale, eventuali redirect e firma `%PDF-`. Non rinominare automaticamente una pagina di errore HTML in PDF. Questi URL sono campioni fissi, non un metodo per trovare nuovi concorsi.

### A.2 Ricerca paginata candidata

La forma della richiesta deriva dal sorgente terzo `[S6]`; il test deve stabilire se è ancora valida.

```bash
set -euo pipefail
umask 077
mkdir -p evidence
API='https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better'

for PAGE in 0 1; do
  curl --fail-with-body --silent --show-error \
    --connect-timeout 10 --max-time 30 --max-filesize 5242880 \
    --proto '=https' \
    --user-agent 'InpaTelegramBot-Probe/0.1' \
    --request POST --header 'Content-Type: application/json' \
    --data '{"text":"","status":["OPEN"]}' \
    --dump-header "evidence/search-${PAGE}.headers" \
    --output "evidence/search-${PAGE}.json" \
    "${API}?page=${PAGE}&size=20"
  sleep 2
done
```

Ispezione locale delle due risposte, con la libreria standard Python:

```python
import json
from pathlib import Path

pages = []
for number in (0, 1):
    path = Path(f"evidence/search-{number}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Risposta non leggibile come JSON: {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise SystemExit(f"Schema diverso da quello candidato: {path}")
    rows = data["content"]
    if any(not isinstance(row, dict) or not row.get("id") for row in rows):
        raise SystemExit(f"Record privi di un ID utilizzabile: {path}")
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"ID duplicati dentro la pagina: {path}")
    pages.append(set(ids))
    print({"page": number, "records": len(rows),
           "total_declared": data.get("totalElements"),
           "sample_ids": ids[:3]})

print({"overlap_between_pages": sorted(pages[0] & pages[1]),
       "second_page_new_ids": len(pages[1] - pages[0])})
print("Prova parziale: completare filtri, fine paginazione e confronto con il sito.")
```

Un JSON valido non prova che il filtro sia applicato. Una sovrapposizione può derivare da pagine ignorate oppure da aggiornamenti concorrenti: va investigata. Una pagina vuota può essere legittima solo nel contesto dei conteggi e della ricerca verificata.

### A.3 Provincia candidata

```bash
curl --fail-with-body --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  --proto '=https' \
  --user-agent 'InpaTelegramBot-Probe/0.1' \
  --request POST --header 'Content-Type: application/json' \
  --data '{"text":"","status":["OPEN"],"provinciaCodice":"SS"}' \
  --output evidence/search-SS.json \
  'https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better?page=0&size=20'
```

Confrontare gli ID con il filtro equivalente dell’interfaccia e con il contenuto territoriale dei bandi. Non interpretare la semplice appartenenza al risultato provinciale come prova che tutti i posti siano in quella provincia.

---

## Appendice B — Registro delle fonti

Fonti consultate il 19 settembre 2026. Per le osservazioni web si distingue la lettura tramite strumento dal collaudo HTTP sulla macchina di destinazione.

**[S1] inPA — FAQ ufficiali.** Funzioni di ricerca, modalità di candidatura e variabilità dei requisiti specifici.  
`https://www.inpa.gov.it/faq-domande-e-risposte/`

**[S2] inPA — Ricerca pubblica.** Pagina aperta; la versione testuale restituita ha mostrato i controlli ma non l’elenco dei bandi.  
`https://www.inpa.gov.it/bandi-e-avvisi/`

**[S3] inPA — Scheda del campione locale.** Consultati metadati e collegamenti agli allegati.  
`https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=d3d8d8d7a82e40afbd75a26841bc9070`

**[S4] inPA — Scheda del campione multiregionale.** Consultati territorio, scadenza e pubblicazione di FAQ successive.  
`https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=01c11fcf96d04a7092d039e5f1a66224`

**[S5] inPA — PDF collegato al campione locale.** Aperto come PDF di 15 pagine; ispezionate visivamente le pagine fisiche 1 e 5; letto il testo pertinente ai requisiti.  
`https://portale.inpa.gov.it/api/media/83f5fa66-9038-4e7d-b09f-11e86f3956be`

**[S6] Sorgente di terzi — davidebr90/concorsi-bridge.** Letto tramite connettore GitHub; punto di partenza per endpoint, body e paginazione, non documentazione ufficiale inPA. File `scripts/sync.mjs`; SHA del **blob del file**, non del commit: `b970112f863bb025b78b368ab094444361cb9608`. Non è stato eseguito il progetto terzo.  
`https://github.com/davidebr90/concorsi-bridge/blob/main/scripts/sync.mjs`

**[S7] inPA — Informativa privacy.** Consultata per il contesto del trattamento dei dati; non usata come licenza di crawling.  
`https://www.inpa.gov.it/privacy-policy/`

**[S8] Playwright — Documentazione ufficiale Network per Python.** Possibilità di osservare le richieste e risposte del browser.  
`https://playwright.dev/python/docs/network`

**[S9] Telegram — Bot API ufficiale.** Interfaccia del bot, metodi di ricezione, messaggi e pulsanti.  
`https://core.telegram.org/bots/api`

### Risorse tentate ma non recuperate con successo

```text
https://www.inpa.gov.it/robots.txt
https://portale.inpa.gov.it/robots.txt
https://www.inpa.gov.it/note-legali/
https://www.inpa.gov.it/wp-json/
https://www.inpa.gov.it/sitemap_index.xml
```

Il mancato recupero da questo strumento non dimostra che queste risorse non esistano. Non è stata ottenuta una documentazione OpenAPI ufficiale e non sono stati verificati un feed RSS completo, limiti di frequenza ufficiali o autorizzazione al monitoraggio periodico.

---

## Istruzione operativa conclusiva per il coder

**Iniziare da M0, non dal bot.** Verificare prima il percorso di ricerca candidato; confermarlo nel traffico pubblico se accessibile, oppure individuare il metodo effettivo senza inventare endpoint. Consegnare un rapporto con richieste, risposte reali e limiti. Solo dopo il superamento di REQ-001 procedere con archivio, Telegram e requisiti personali.
