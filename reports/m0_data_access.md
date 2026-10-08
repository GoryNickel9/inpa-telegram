# M0 — Recupero dei dati inPA: esiti reali e limiti residui

**Data delle misure:** 19 settembre 2026, 17:50–19:30 UTC
**Macchina:** PC di sviluppo con accesso Internet diretto (Windows 11, Python 3.13.2, curl 8.12.1)
**User-Agent:** `InpaTelegramBot-Probe/0.1` — 1 richiesta per volta, ≥2 s fra richieste allo stesso host
**Riferimento:** SPEC §1 (REQ-001), §3 (procedura M0), §3.4 (test di accettazione)

---

## 1. In breve

La catena richiesta da REQ-001 funziona **per i metadati**, e funziona per una via diversa da
quella che la specifica indicava come candidato prioritario:

```text
sitemap pubblica → 1680 id di bandi attivi → scheda HTML server-side
                 → metadati completi + riferimenti agli allegati
                 → snapshot persistente → seconda acquisizione → confronto deterministico
```

**Non funziona per gli allegati**, e non per un problema tecnico: l'host che li ospita
(`portale.inpa.gov.it`) pubblica `Disallow: /` per ogni agente automatico. Lo stesso divieto
copre l'endpoint di ricerca `search-better` indicato in SPEC §2.1. Dettagli e testo integrale
in [`access_conditions.md`](access_conditions.md).

**Esito complessivo: REQ-001 soddisfatto per il perimetro dei metadati, non soddisfatto per i
documenti.** Le conseguenze sul piano di lavoro sono al §6.

---

## 2. Correzioni alla specifica, basate su misure

La specifica elencava come "non recuperate" alcune risorse e come non verificati alcuni punti.
Da questa macchina i risultati sono diversi. Ogni riga qui sotto e' una richiesta HTTP reale.

| Punto in SPEC | Stato nella specifica | Misura del 19/09/2026 |
|---|---|---|
| `www.inpa.gov.it/robots.txt` | non recuperato | **HTTP 200**, 202 byte, consente i bandi |
| `portale.inpa.gov.it/robots.txt` | non recuperato | **HTTP 200**, `Disallow: /` |
| `www.inpa.gov.it/note-legali/` | non recuperato | **HTTP 200**, licenza CC-BY 3.0 |
| `sitemap_index.xml` | non recuperato | non esiste; esiste **`/sitemap.xml`**, HTTP 200 |
| Errore DNS su `portale.inpa.gov.it` | limite d'ambiente | confermato limite d'ambiente: da qui risolve |
| Parametro regione «non accertato» | non accertato | **`regioneId`**, valori `1`–`20` piu' `-1` = Nazionale |
| `dataPubblicazione` | significato da verificare | e' la data di **apertura candidature** (§3.3) |
| Stati oltre `OPEN` | elenco non noto | l'interfaccia ne offre due, `OPEN` e `CLOSED`; resta non provato che siano tutti |

Tre precisazioni doverose:

- Il fatto che `portale.inpa.gov.it` risponda **non** lo rende utilizzabile: risponde anche il
  suo `robots.txt`, che lo vieta.
- Le due righe su `regioneId` e `dataPubblicazione` hanno provenienze diverse. Il **nome del
  parametro** e la semantica di `dataPubblicazione` vengono dal **JavaScript ufficiale di
  inPA** servito da `www.inpa.gov.it` (`/wp-content/plugins/dro-dashboard/...`): una fonte di
  prima parte, non il progetto di terzi citato in SPEC §2.1, e non tentativi. I **valori**
  `1`–`20` e `-1` vengono invece dal menu a tendina della ricerca, letto in un browser. Resta
  codice e interfaccia di frontend: descrivono come il sito chiama il proprio servizio, non
  un contratto garantito verso terzi.
- «L'interfaccia ne offre due» non significa «esistono solo due stati»: significa che due
  sono selezionabili. Il codice mappa infatti su `UNKNOWN` qualunque valore non riconosciuto,
  conservando l'originale.

---

## 3. La rotta adottata

### 3.1 Scoperta: la sitemap

`https://www.inpa.gov.it/robots.txt` dichiara esso stesso
`Sitemap: https://www.inpa.gov.it/sitemap.xml`. L'indice contiene tre sitemap, fra cui
**`sitemap-bandi.xml`**: 1680 `<url>`, ciascuna con `<loc>`, `<lastmod>`, `<changefreq>`,
`<priority>`.

Questa non e' una scorciatoia: e' il meccanismo che il gestore pubblica *per farsi
enumerare*. Rispetto all'endpoint di ricerca ha tre vantaggi concreti:

1. e' esplicitamente consentito;
2. `lastmod` fornisce gia' il segnale di cambiamento, senza dover rileggere tutto;
3. non e' paginato, quindi non esiste il problema di un elenco che cambia mentre lo si sfoglia
   (il rischio che SPEC §5.2 chiede di gestire).

**Copertura, misurata contro i conteggi della fonte.** La sitemap contiene i bandi **aperti**,
non lo storico. Tre misure indipendenti concordano:

| Misura | Valore |
|---|---|
| Voci in `sitemap-bandi.xml` (18:05 UTC) | **1680** |
| «Aperto» secondo la ricerca pubblica (18:50 UTC) | **1674** |
| «Chiuso» secondo la ricerca pubblica | **73 542** |
| Schede campionate dalla sitemap e risultate «Aperto» | 26 su 26 |
| Id chiusi di SPEC §2.4 presenti in sitemap | 0 su 2 |

Lo scarto di 6 fra 1680 e 1674 e' compatibile con i 45 minuti trascorsi fra le due misure
(bandi scaduti nel frattempo) e/o con la latenza di rigenerazione della sitemap. **Non e'
stato misurato quale delle due cause pesi**, ed e' una delle prime cose da quantificare in M1
perche' determina la cadenza di sincronizzazione.

Il punto che conta: i 73 542 bandi chiusi **non** sono nella sitemap. La copertura dei bandi
aperti e' invece praticamente completa (1680 ≈ 1674). Le schede chiuse restano comunque
raggiungibili per id diretto (verificato: entrambi gli id chiusi rispondono HTTP 200), quindi
i bandi seguiti si continuano a controllare per id come SPEC §5.2 richiede, e la scomparsa
dalla sitemap **non** viene interpretata come revoca.

Conseguenza dimensionale utile a M1: l'archivio da mantenere e' dell'ordine di **1700 record
attivi**, non di 75 000. SQLite su disco locale, come proposto in SPEC §10, e' ampiamente
sufficiente.

### 3.2 Dettaglio: la scheda HTML

`https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=<id>` e'
renderizzata **lato server**: i valori sono nell'HTML grezzo, senza bisogno di un browser.

La pagina di elenco `/bandi-e-avvisi/` invece **non** lo e' (0 link di dettaglio nell'HTML
grezzo): conferma l'osservazione di SPEC §2.2 [S2], e spiega perche' la scoperta passa dalla
sitemap e non dall'elenco.

Struttura stabile su 28 campioni:
`<span class="concorso-label">Etichetta: </span><span class="concorso-content">valore</span>`.

**L'HTML e' malformato in un punto ricorrente:** dopo «Data chiusura candidature» il `<p>` e'
chiuso da `</div>`. Presente in tutti i campioni. Per questo il parser scandisce un flusso di
token invece di costruire un albero: una struttura ad albero rigida attribuisce quel valore al
nodo sbagliato.

---

## 4. Test di accettazione M0

Esiti dichiarati senza arrotondamenti. «Parziale» e «Non eseguito» sono usati dove servono.

| Test | Esito | Evidenza |
|---|---|---|
| **M0-01** Accesso | **Parziale** | Ricerca (sitemap) e dettaglio ottenuti senza alcuna identita' digitale ne' sessione. **Nessun allegato**: vietato da robots. |
| **M0-02** Paginazione | **Superato, con riserva di natura** | Prova eseguita: 168 pagine, 1680 id letti, **1680 distinti, 0 duplicati, 0 sovrapposizione** fra pagina 1 e 2, totale dichiarato 1680, scansione non troncata. Vedi nota sotto. |
| **M0-03** Filtri | **Superato come strategia locale** | Filtro lato fonte non praticabile; strategia locale documentata e confrontata con l'interfaccia pubblica su 2 regioni. Vedi §4.1. |
| **M0-04** Campione | **Superato** | 28 schede, 26 dalla sitemap + 2 chiuse di SPEC §2.4. Enti diversi, 6 tipi di valutazione, 11 aree geografiche, da 1 a 1100 posti, da 0 a 3 allegati. Parser eseguito su tutte: **28/28**, vedi §4.3. |
| **M0-05** Documenti | **Non superato — bloccato** | 0 PDF scaricati su 5 richiesti. 40/40 allegati osservati stanno su un host che li vieta. Non e' un difetto del codice: e' una condizione della fonte. |
| **M0-06** Aggiornamenti | **Superato** | Due acquisizioni reali degli stessi 5 id, confronto deterministico. Casi proroga / nuovo allegato / contenuto sostituito provati su copie locali ed **etichettati come simulati**. Vedi §4.2. |
| **M0-07** Stabilita' 48 h | **Non eseguito** | Nessuna VPS in esercizio e nessuna osservazione continuativa. Dichiarato non eseguito, come gia' previsto da SPEC §3.4. |
| **M0-08** Errori | **Superato a livello di test** | Timeout, HTML al posto del contenuto atteso, 429, 5xx e pagine mancanti sono coperti da test e non producono «nessun concorso». Non ancora osservati tutti dal vivo. |
| **M0-09** Tracciabilita' | **Superato** | Campioni sanitizzati, contratto osservato, fixture reali, comando riproducibile, date e hash delle prove. |
| **M0-10** Condizioni | **Superato con un punto ostativo aperto** | Condizioni verificate e documentate; il punto sugli allegati resta aperto ed e' esplicito, non nascosto. |

### 4.1 Nota su M0-02 e M0-03

**Paginazione.** La fonte adottata **non pagina**: la sitemap e' un unico documento. Il test
«due pagine realmente differenti» e' stato quindi soddisfatto sulla paginazione *locale*
dell'adattatore, non su una paginazione del servizio. E' una differenza sostanziale rispetto a
quanto la specifica immaginava e va letta cosi': il rischio che M0-02 voleva scoprire —
elenchi che cambiano mentre li si sfoglia, conteggi incoerenti, record persi fra una pagina e
l'altra — **in questa rotta non esiste**, perche' l'elenco e' acquisito in una sola richiesta
atomica. Il campo `SearchPage.declared_total` espone comunque il totale dichiarato dalla fonte.

**Filtri.** Il filtro per regione lato fonte richiede l'host vietato. La strategia adottata e'
il filtraggio **locale** sul campo «Area geografica» della scheda. Il confronto con
l'interfaccia pubblica (eseguito a mano, in un browser, su `regioneId=11` Molise e
`regioneId=19` Valle d'Aosta) ha prodotto tre risultati che hanno cambiato il codice:

1. **«Nazionale» e' un valore a se'.** Il menu dei territori espone «Nazionale» (`id=-1`)
   accanto alle 20 regioni, e i bandi cosi' marcati compaiono nel risultato di *ogni* regione
   (Molise: 66 risultati; Valle d'Aosta: 38; i primi 4 sono gli stessi, tutti «Nazionale»).
   Trattarlo come territorio non riconosciuto avrebbe marcato ogni concorso nazionale come
   «da verificare». Ora e' una categoria esplicita, attivabile o disattivabile dall'utente,
   come SPEC §6 richiede.
2. **«Area geografica» mescola regioni e province.** Un campione reale elenca
   «Calabria, Pescara, Sud Sardegna, Barletta-Andria-Trani, …» per 108 elementi. L'anagrafica
   li risolve tutti (1 regione + 107 province, 0 non riconosciuti).
3. **Il campo non prova la sede di lavoro.** Un bando del «Comune di San Nazzaro» (Campania)
   e uno dell'«Ufficio Scolastico Regionale per il Lazio» sono entrambi «Nazionale». Per
   questo il territorio della scheda e' mappato su `TerritoryScope.UNSPECIFIED` e non su
   `WORK_SITE`: e' esattamente la confusione che SPEC §6 vieta.

Il filtro locale e' **conservativo per costruzione**: una provincia richiesta non e'
soddisfatta da un bando noto solo a livello regionale (i posti potrebbero essere altrove), e
un territorio irrisolto resta visibile marcato «da verificare» invece di sparire in silenzio.

### 4.2 Nota su M0-06 — una misura che ha cambiato il progetto

Le stesse 5 schede sono state scaricate due volte, a circa 25 minuti di distanza.
**Tutte e 5 hanno restituito byte diversi.** Le differenze, per ogni pagina, sono solo due:

```text
- var wpdm_js = {... "client_id":"69cd8489cc3fea956656fe6e2c73830e"};
+ var wpdm_js = {... "client_id":"45cda743e7f21ea8654687859d080904"};
- <div class="gtranslate_wrapper" id="gt-wrapper-60190065"></div>
+ <div class="gtranslate_wrapper" id="gt-wrapper-86234703"></div>
```

Un token e un id DOM generati a ogni richiesta. **Zero differenze nel contenuto del bando.**

Conseguenza pratica: un rilevamento basato sull'hash della pagina segnalerebbe *tutti* i
bandi come aggiornati *a ogni giro*, cioe' 1680 false notifiche l'ora. La separazione fra
`metadata_hash` (soli campi significativi) e `raw_document_hash` (documento intero), che SPEC
§4.2 chiedeva in via cautelativa, qui e' **necessaria e verificata sul campo**. Il test
`solo raw hash diverso → document_changed=True, significant=False` non e' teorico: riproduce
il comportamento reale del sito.

**La catena completa, eseguita dal vivo.** Il comportamento non e' solo previsto dai test: e'
stato eseguito contro il sito reale attraverso i moduli del progetto, con due acquisizioni
successive degli stessi tre id.

```text
scoperta: 1680 bandi, 0 anomalie
  df4a037fb8 open posti=1 terr=1 all=0 new=True
  07bd3da8a1 open posti=1 terr=1 all=2 new=True
      allegato permesso=False motivo=[*] Disallow: /
      allegato permesso=False motivo=[*] Disallow: /
  4fb52c2d29 open posti=1 terr=1 all=2 new=True
      allegato permesso=False motivo=[*] Disallow: /

-- seconda acquisizione degli stessi id --
  df4a037fb8 new=False meta=False doc=True SIGNIFICATIVO=False
  07bd3da8a1 new=False meta=False doc=True SIGNIFICATIVO=False
  4fb52c2d29 new=False meta=False doc=True SIGNIFICATIVO=False
```

Due cose da notare, perche' sono il cuore di M0:

- `doc=True` e `SIGNIFICATIVO=False` su tutti e tre: il documento e' cambiato davvero (i due
  elementi volatili), il bando no, e **nessun falso aggiornamento e' stato prodotto**;
- `permesso=False motivo=[*] Disallow: /`: il divieto non e' cablato nel codice. E' stato
  letto dal `robots.txt` di `portale.inpa.gov.it` al momento dell'osservazione e la regola
  che ha deciso viaggia nel dato, dove resta verificabile.

I tre eventi di M0-06 (proroga, aggiunta di un allegato, sostituzione del contenuto) non si
sono verificati spontaneamente nella finestra di osservazione. Sono stati riprodotti su copie
locali controllate e **sono etichettati come simulati**, come SPEC §3.4 consente.

---

### 4.3 Il parser sull'intero campione

Non solo sui sei casi limite tenuti come fixture: il parser e' stato eseguito su tutte e 28
le schede raccolte.

```text
schede lette con successo: 28/28
stati: {'open': 26, 'closed': 2}
senza titolo: 0 | senza data chiusura risolta: 0 | senza posti: 0
territori NON normalizzati: 0
estensioni allegati: {'pdf': 36, 'p7m': 2, 'rtf': 1, 'docx': 1}
allegati prelevabili: {False: 40}
anomalie: {'etichetta_non_mappata': 1}
pagina non-scheda -> ParseError (nessun falso 'bando vuoto')
```

Come leggerlo:

- **0 territori non normalizzati** su 28 schede: l'anagrafica regge i nomi come li scrive
  inPA («Emilia Romagna», «Trentino Alto Adige»). Testata anche sul caso peggiore visto
  nella ricerca pubblica, un elenco di 108 elementi misti regioni/province: risolti tutti.
- **L'unica anomalia** e' `etichetta_non_mappata`, sollevata da «Corrispettivo minimo annuo»,
  un campo presente su 1 scheda su 28. Non e' stato scartato: e' finito in `extra_fields`, che
  e' il meccanismo con cui ci si accorge che la fonte ha aggiunto un campo.
- **40 allegati su 40 non prelevabili**, con la regola robots come motivo in ciascun caso.
- Una pagina che non e' una scheda solleva `ParseError`. E' il requisito M0-08: una risposta
  di errore non deve trasformarsi in un bando senza requisiti.

### 4.4 Revisione critica: sette difetti trovati e corretti

Il codice e' stato sottoposto a una revisione indipendente su cinque prospettive (dati,
sicurezza, fedelta' dei rapporti, aderenza alla specifica, robustezza), con una verifica
adversariale su ogni rilievo: chi verificava aveva il compito di **confutarlo**, e il rilievo
passava solo se resisteva. Su 55 rilievi, 31 sono stati confutati.

I difetti corretti, tutti riprodotti prima e coperti da test dopo
(`tests/source/test_review_regressions.py`, 29 test):

| Dove | Difetto | Perche' contava |
|---|---|---|
| `robots.py` | Uno stato diverso da 200/404 (403, 429, 5xx, 3xx) era letto come «nessuna direttiva», cioe' **permesso totale** | Un `503` momentaneo di `portale.inpa.gov.it` avrebbe reso scaricabili gli allegati. Faceva cadere la condizione su cui si regge l'intero perimetro |
| `robots.py` | Il gruppo si sceglieva per sottostringa: il nostro token contiene `TelegramBot` | Si obbedisce alle regole di un altro programma; con un `Allow` altrui ci si attribuisce un permesso mai concesso |
| `http.py` | `Content-Length` non confrontato con i byte letti | Una scheda troncata in transito diventava un bando con titolo giusto e tutto il resto vuoto, e il diff la presentava come aggiornamento: un guasto di rete travestito da notifica |
| `adapter.py` | Una sitemap le cui voci non producono id restava «completa» con zero record | Il falso «nessun concorso» che M0-08 vieta |
| `adapter.py` | La porta non era verificata prima di valutare un allegato | Un href su `www.inpa.gov.it:8443` risultava `fetch_permitted=True`: un permesso mai concesso scritto nei dati |
| `snapshot.py` | Riordinare territori o enti contava come aggiornamento, mentre `metadata_hash` li ordinava | I due meccanismi si contraddicevano: impronta «identico», confronto «aggiornato». Notifica per nulla su ogni bando multiregionale |
| `snapshot.py` | Le letture non prendevano il lock che `close()` prende | Una chiusura concorrente faceva fallire la lettura con un errore del driver invece che con «archivio chiuso» |

In piu', due correzioni ai rapporti stessi: la frase sul «rifiuto prudente» descriveva un
comportamento che il codice non aveva (ora ce l'ha), e l'affermazione «nessun dato personale
di terzi» e' stata ridotta a cio' che e' stato davvero misurato (§4 di
`access_conditions.md`).

### 4.5 Gli undici rilievi rimasti in sospeso, verificati e chiusi

Undici verifiche della prospettiva «robustezza» non erano state eseguite: il processo si era
interrotto a meta'. Sono state riprese e portate a termine una per una, riproducendo ogni
scenario prima di decidere. Uno era gia' stato risolto da una correzione precedente; gli
altri dieci erano reali e sono corretti, con 32 test in
`tests/source/test_robustness_regressions.py`.

| Dove | Difetto | Perche' contava |
|---|---|---|
| `robots.py` | Un pattern con molti `*` mandava il confronto in **backtracking esponenziale**: misurati 0,2 s con 8 jolly e **18,5 s con 12** | `evaluate` gira per ogni URL: un solo pattern del genere nel robots.txt della fonte fermava l'intera acquisizione. Sostituito da un confronto a due indici |
| `robots.py` | Un guasto di rete su `robots.txt` restava in cache **24 ore** | Un errore di pochi secondi diventava un fermo di un giorno. Ora gli esiti negativi durano 5 minuti |
| `http.py` | `validate_url` lasciava sfuggire `ValueError` su porta o IPv6 malformati | E' la funzione che valida **i link inviati dagli utenti** (SPEC §9): il bot sarebbe caduto su un messaggio invece di rifiutare il link |
| `http.py` | Un `Retry-After: 3600` veniva troncato a 60 s e la richiesta **ripetuta 4 volte** | La fonte aveva chiesto di non bussare. SPEC §5.3 impone di rispettarlo e §3.1 vieta di aggirare i blocchi: ora si smette e si riporta l'attesa dichiarata |
| `http.py` | Risposte troppo grandi e URL rifiutati non lasciavano **alcuna traccia** in `client.records` | E' la sorgente dei rapporti M0-09: il rapporto avrebbe detto «nessuna richiesta» dove c'era stato un rifiuto |
| `http.py` | Una decodifica con sostituzioni passava in silenzio | Un charset sbagliato produce testo dall'aspetto normale: la differenza sarebbe stata indistinguibile da una modifica del bando |
| `detail.py` | Se la fonte rinomina le classi CSS dei **valori**, ogni scheda diventava un bando «valido» e vuoto | Il confronto avrebbe annunciato scadenza sparita, posti azzerati e allegati rimossi per tutti i bandi: un cambio di tema del sito trasformato in notifiche (M0-08) |
| `sitemap.py` | I `<url>` senza `<loc>` erano scartati in silenzio e il totale li contava gia' sottratti | Una sitemap con meta' voci mutile sarebbe sembrata una sitemap piccola e sana |
| `adapter.py` | Una sitemap passata da 1680 a 3 voci restava «completa» | Chi sincronizza avrebbe concluso che 1677 bandi erano spariti, mentre SPEC §4.2 avverte che un'assenza non e' una revoca |
| `snapshot.py` | Una versione archiviata illeggibile sollevava `JSONDecodeError` fuori dalla tassonomia | Una riga danneggiata interrompeva l'intera acquisizione. Restituire `None` sarebbe stato peggio: il bando sarebbe risultato «nuovo» e la notifica rispedita |

Il rilievo gia' risolto riguardava un href di allegato malformato che sollevava `ValueError`:
lo copriva la correzione sulla porta (§4.4).

**Come e' stato deciso il caso piu' delicato.** Il difetto su `detail.py` chiedeva di
distinguere «bando genuinamente scarno» da «scheda che non sappiamo piu' leggere». La
distinzione non e' arbitraria, e' misurata: sulle sei schede reali le etichette trovate e
quelle valorizzate coincidono (da 8 a 11, sempre 1 a 1); rinominando la classe dei
contenitori di valore il rapporto diventa 1 su 10. La soglia scatta sotto il 50% e solo con
almeno quattro etichette, cosi' un bando con un solo campo resta valido.

---

## 5. Carico imposto alla fonte

| Voce | Valore |
|---|---|
| Richieste automatiche, intera sessione | ~70 |
| Di cui per la raccolta del campione | 44 |
| Di cui schede di dettaglio | ~40 (26 campione + 5 riletture + 1 multiregionale + prove) |
| Intervallo minimo fra richieste allo stesso host | 2 s, sequenziale, mai in parallelo |
| Richieste automatiche a `portale.inpa.gov.it` | **4, tutte al solo `/robots.txt`** |
| Errori HTTP ricevuti | 1 × 404 atteso (pagina inesistente), 0 imprevisti |
| Byte scaricati | ~4 MB |

Le 4 richieste a `portale.inpa.gov.it` sono altrettante letture del suo `robots.txt`, una per
ciascun client istanziato nelle prove: la cache delle direttive vive nell'istanza, quindi un
processo nuovo rilegge il file. Nessuna di esse ha toccato un allegato o l'endpoint di
ricerca. In esercizio il file verrebbe letto una volta ogni 24 ore per processo.

Nessuna richiesta automatica e' stata inviata all'endpoint `search-better` ne' ad alcun
allegato: sarebbe stato violare la regola letta un minuto prima. La validita' tecnica di
quell'endpoint resta quindi **ignota per scelta** — e' il risultato corretto di questa
verifica, non una lacuna.

**Una precisazione necessaria sul confronto di M0-03.** Il confronto dei filtri con
l'interfaccia pubblica (§4.1) e' stato fatto aprendo a mano quattro volte la pagina di
ricerca in un browser. Quella pagina, nel browser, esegue il proprio JavaScript e chiama
`portale.inpa.gov.it/concorsi-smart/api/…`: **alcune richieste a quell'host sono quindi
partite dal browser**, esattamente come accadono a chiunque visiti il sito.

E' una consultazione manuale, non un accesso automatico, ed e' la via che SPEC §3.2 indica
(«aprire la pagina pubblica, osservare le richieste di rete»). Va comunque detto invece che
lasciato intendere che a `portale` non sia mai arrivato nulla. Quelle richieste sono
episodiche, non ripetibili da un processo, e **non fanno parte della rotta di acquisizione**:
il codice non contiene alcun percorso che chiami quell'host senza una deroga configurata
esplicitamente.

---

## 6. Cosa significa per il piano di lavoro

**Puo' procedere subito (M1 → M2).** Archivio, sincronizzazione, versionamento, filtri
territoriali, scadenze, promemoria, rilevazione di proroghe e di nuovi allegati: tutto questo
sta nei metadati della scheda, che sono acquisibili in modo consentito, stabile e verificato.
La *comparsa* di un documento e' un evento osservabile dalla scheda anche senza scaricarlo.

**Non puo' procedere (M3).** La verifica dei requisiti dai documenti richiede gli allegati.
Nel campione della specifica il requisito formativo sta nell'articolo 3 a pagina 5 del PDF, e
la scheda sintetica non lo espone. Senza allegati il bot puo' dire *quali* concorsi esistono e
*quando* scadono, ma **non** se una persona ne soddisfa i requisiti. E' una riduzione
sostanziale dell'obiettivo dichiarato.

**Decisione che spetta al committente, non al codice.** Tre strade:

1. chiedere al gestore un accesso autorizzato agli allegati (§5 di `access_conditions.md`);
   il codice e' gia' predisposto e la deroga si attiva per configurazione, restando tracciata;
2. rilasciare M1–M2 come servizio di monitoraggio, rimandando M3;
3. prevedere che sia l'utente ad aprire il PDF dal link ufficiale, con il bot che segnala il
   documento invece di analizzarlo.

Il progetto oggi implementa la **2**, con la **1** pronta e disattivata.

---

## 7. Limiti di questa verifica

- Una sola giornata, una sola rete, un solo user-agent. Nessuna informazione su blocchi
  geografici o su come la fonte reagisca a volumi maggiori.
- La sitemap e' stata osservata **una volta**: la sua frequenza di aggiornamento reale e la
  latenza fra pubblicazione di un bando e comparsa nella sitemap **non sono misurate**. E' la
  prima cosa da misurare in M1, perche' da essa dipende la cadenza di sincronizzazione.
- L'affermazione «la sitemap contiene i bandi aperti» e' sostenuta dal confronto con i
  conteggi della fonte (1680 voci contro 1674 «Aperto» e 73 542 «Chiuso») oltre che dai 26
  campioni. Resta **non osservata nel tempo** la transizione di un singolo bando: nessuno e'
  stato visto sparire dalla sitemap al momento della propria chiusura.
- Lo scarto di 6 fra le due misure non e' spiegato: puo' essere scadenza naturale nei 45
  minuti fra le due, latenza della sitemap, o entrambe.
- Nessuna risposta scritta del gestore sulle condizioni d'uso.
- M0-07 (48 ore di esercizio) non eseguito.
- I diciassette difetti corretti in §4.4 e §4.5 erano **tutti invisibili ai 224 test scritti
  prima della revisione**. E' la misura piu' utile di questo documento: una suite verde dice
  che il codice fa cio' che qualcuno ha pensato di chiedergli, non che sia corretto.
- La revisione ha coperto cinque prospettive, non tutte le prospettive possibili. Che i suoi
  rilievi siano ora chiusi non significa che non ne esistano altri.
- Il rilevamento del crollo della sitemap (§4.5) confronta due letture **dello stesso
  processo**: dopo un riavvio la prima lettura non ha termine di paragone. Un confronto
  persistente fra esecuzioni appartiene a M1, dove esiste l'archivio che lo regge.

---

## 8. Riprodurre queste misure

```bash
python scripts/probe_inpa.py --out evidence/ --limit 5
```

La prova riscarica `robots.txt` dei due host, l'indice delle sitemap, la sitemap dei bandi,
verifica la paginazione locale, apre le schede richieste e riporta per ogni allegato l'esito
della valutazione del permesso **senza tentarne il download**. Scrive in `evidence/` i corpi
grezzi, un `requests.jsonl` con una riga per richiesta e un `summary.json` con gli esiti.

Esecuzione del 19/09/2026, 18:55 UTC:

```text
[OK] 1. condizioni di accesso (robots.txt)
[OK] 2. scoperta: indice delle sitemap e sitemap dei bandi
[OK] 3. paginazione di search() (M0-02)
[OK] 4. schede di dettaglio
[OK] 5. allegati: valutazione delle condizioni di accesso
        nota: 6 riferimenti su 6 non prelevabili; nessun download tentato

Richieste inviate : 9 (830562 byte, 3.6 s di risposta)
Esito complessivo : OK
```

Numeri estratti da `summary.json` della stessa esecuzione:

| Voce | Valore |
|---|---|
| Voci dichiarate dalla sitemap | 1680 |
| Pagine lette nella scansione completa | 168 |
| Id letti / distinti / duplicati | 1680 / 1680 / 0 |
| Sovrapposizione fra pagina 1 e pagina 2 | nessuna |
| Richieste di rete durante la paginazione | 0 (elenco gia' in memoria) |
| Allegati trovati / prelevabili | 6 / **0** |
| Richieste di rete generate dagli allegati | **0** |
| Host interrogati oltre a `www` | `portale.inpa.gov.it`, solo `/robots.txt` |

I file in `evidence/` possono contenere header di risposta: sono esclusi dal versionamento
(`.gitignore`) e vanno sanitizzati prima di essere condivisi.

Test offline, senza rete:

```bash
python -m pytest tests/source -q
```

Esito del 20/09/2026: **286 passati, 5 saltati** (i saltati sono i test di rete, disattivati
per impostazione predefinita). Distribuzione: detail 40, adapter 35, **robustness_regressions
32**, http 30 + 7 di regressione, normalize 28, **review_regressions 29**, robots 27,
sitemap 18, snapshot 20, territory 20.

I 61 test nei due file di regressione coprono i difetti descritti in §4.4 e §4.5. Che non
siano tautologici e' stato verificato annullando tre correzioni una alla volta: ogni revert
fa fallire da uno a otto test.

Test di integrazione con richieste reali, esplicitamente opt-in:

```bash
INPA_INTEGRATION=1 python -m pytest tests/source/test_integration.py -q
```

Esito del 19/09/2026 contro la fonte reale: **4 passati, 1 saltato**. Il saltato e' condizionale
e corretto: la scheda estratta dalla sitemap in quel momento non pubblicava allegati, quindi il
test sugli allegati si e' astenuto invece di fallire per una ragione estranea a cio' che
verifica.
