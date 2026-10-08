# Condizioni di accesso alle fonti inPA

**Verifica eseguita il:** 19 settembre 2026, fra le 18:00 e le 18:20 UTC
**Da:** macchina di sviluppo con accesso Internet diretto (Windows 11, `curl` 8.12.1, Python 3.13.2)
**User-Agent usato:** `InpaTelegramBot-Probe/0.1`
**Riferimento:** SPEC §3.1 (controllo preliminare delle condizioni) e test M0-10

Questo documento riporta cosa e' stato **effettivamente recuperato** e quali conclusioni se ne
possono trarre. Distingue le tre verifiche che la specifica chiede di non confondere:
accessibilita' tecnica, condizioni di utilizzo, gestione dei dati personali.

> Nota rispetto alla specifica: le risorse elencate come "tentate ma non recuperate"
> nell'Appendice B (`robots.txt` di entrambi gli host, `note-legali`, `sitemap`) **sono state
> recuperate con successo** da questa macchina. Il mancato recupero precedente dipendeva
> dall'ambiente di analisi, non dal sito. Le conclusioni che seguono sostituiscono quelle
> provvisorie del documento di specifica.

---

## 1. Esito sintetico

| Verifica | Esito | Conseguenza per il progetto |
|---|---|---|
| Accessibilita' tecnica di `www.inpa.gov.it` | Riuscita | Rotta utilizzabile |
| Accessibilita' tecnica di `portale.inpa.gov.it` | Riuscita (robots.txt servito) | Non rilevante: vedi sotto |
| `robots.txt` di `www.inpa.gov.it` | Consente le pagine dei bandi | **Scoperta e dettagli: permessi** |
| `robots.txt` di `portale.inpa.gov.it` | `Disallow: /` per ogni agente | **Ricerca API e allegati: non praticabili** |
| Licenza dei contenuti | CC-BY 3.0 IT | Riuso consentito con citazione della fonte |
| Informativa privacy | Consultata | Nessun dato personale di terzi viene raccolto dal bot |
| Limiti di frequenza ufficiali | Non pubblicati | Si applica un ritmo prudente autoimposto |

**Punto ostativo residuo (M0-10): uno.** Il download automatico degli allegati non e'
compatibile con il `robots.txt` dell'host che li ospita. Il perimetro di M0 e' stato
ridefinito di conseguenza, in modo documentato, invece di procedere ugualmente.

---

## 2. `robots.txt` — testo integrale osservato

### 2.1 `https://www.inpa.gov.it/robots.txt`

HTTP 200, `text/plain; charset=UTF-8`, 202 byte, `sha256:05137b4dd278a751…`

```text
User-agent: *
Disallow: /wp-admin/
Disallow: /wp-login.php
Disallow: /xmlrpc.php
Disallow: /admin-area/
Disallow: /wp-json/
Allow: /wp-admin/admin-ajax.php

Sitemap: https://www.inpa.gov.it/sitemap.xml
```

**Lettura.** I divieti riguardano l'amministrazione di WordPress e l'API REST `/wp-json/`.
Non e' vietato nulla sotto `/bandi-e-avvisi/`, che e' il percorso delle schede pubbliche, ne'
`/sitemap*.xml`. Il file **dichiara esso stesso** una sitemap: e' un invito esplicito
all'enumerazione automatica, non una tolleranza implicita.

Percorsi valutati per questo progetto:

| URL | Verdetto | Regola determinante |
|---|---|---|
| `/sitemap.xml`, `/sitemap-bandi.xml` | consentito | nessuna regola corrispondente |
| `/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=…` | consentito | nessuna regola corrispondente |
| `/wp-json/…` | **vietato** | `Disallow: /wp-json/` |

L'ultima riga e' rilevante: rinuncia a priori a cercare un'API REST WordPress come fonte
alternativa. Non e' stata tentata.

### 2.2 `https://portale.inpa.gov.it/robots.txt`

HTTP 200, `text/plain`, 25 byte, `sha256:efdb5938a9736727…`

```text
User-agent: *
Disallow: /
```

**Lettura.** Divieto totale, per qualunque agente automatico, su tutto l'host. Riguarda:

- `https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better`
  — l'endpoint di ricerca indicato dalla specifica come candidato prioritario (§2.1);
- `https://portale.inpa.gov.it/api/media/<uuid>` — **tutti** gli allegati. Nei 28 campioni
  raccolti, **40 allegati su 40** stanno su questo host, senza eccezioni.

### 2.3 Perche' il divieto vale anche se il sito pubblico usa quell'API

La ricerca pubblica su `www.inpa.gov.it` e' resa dal browser, che chiama quell'endpoint via
JavaScript (verificato: `dro-cerca-bandi.js` chiama
`inpaVars.apiurl + '/concorsi-smart/api/concorso-public-area/search-better'`, con
`inpaVars.apiurl = "https://portale.inpa.gov.it"`).

Questo **non** rende l'endpoint disponibile a un crawler. `robots.txt` si rivolge ai
programmi che navigano il sito per conto proprio, che e' esattamente cio' che fa un bot di
monitoraggio; il browser di una persona che consulta una pagina e' un caso diverso. Il
gestore ha reso la distinzione esplicita servendo regole opposte sui due host: permissive
dove pubblica le pagine da leggere, totali dove espone i servizi applicativi.

Trattare quel confine come aggirabile perche' "tanto e' pubblico" sarebbe l'elusione che la
specifica vieta al §3.1. Il progetto non lo fa.

---

## 3. Condizioni di utilizzo e licenza

`https://www.inpa.gov.it/note-legali/` — HTTP 200, recuperata il 19/09/2026.

Citazione rilevante, sezione *Copyright*:

> «Salvo ove diversamente indicato, i contenuti pubblicati sul presente sito (testi, immagini,
> fotografie, video, ecc.) sono messi a disposizione con licenza CC-BY 3.0»
> — con obbligo di citare la fonte e, ove possibile, l'indirizzo della pagina originale.

**Conseguenze operative, gia' riflesse nel codice:**

1. Il riuso dei metadati dei bandi e' consentito. Il bot deve **citare la fonte** in ogni
   messaggio: per questo `NoticeSnapshot.source_url` non e' opzionale e ogni notifica dovra'
   riportare il link ufficiale (SPEC §9.1 lo chiedeva gia' per altre ragioni; qui diventa
   anche un obbligo di licenza).
2. Vale il "salvo ove diversamente indicato": i singoli documenti allegati possono avere
   condizioni proprie. Non essendo scaricabili (§2.2), il punto per ora non si pone.

**Limite onesto della verifica.** Il testo delle Note legali descrive come «il Sito»
`www.funzionepubblica.gov.it`, mentre e' pubblicato su `inpa.gov.it` e linkato dal footer di
ogni pagina inPA. L'applicabilita' a inPA e' quindi *molto probabile ma non letterale*. Non
si e' trovata una pagina di note legali specifica per inPA. Questo e' uno dei punti da
chiarire con il gestore (§5).

Non e' stata individuata documentazione OpenAPI ufficiale, ne' un feed RSS, ne' limiti di
frequenza pubblicati.

---

## 4. Dati personali

`https://www.inpa.gov.it/privacy-policy/` — HTTP 200, consultata.

Il livello di acquisizione **non cerca** dati personali di terzi: i campi che estrae sono
metadati amministrativi (titolo, ente, date, territori, posti, link agli allegati). Va detto
con precisione che cosa questo significa e che cosa no.

- **Non e' stato misurato** che quei campi ne siano sempre privi. Titolo e descrizione sono
  testo libero redatto dall'amministrazione e possono contenere incidentalmente un nome, per
  esempio quello di un responsabile del procedimento. Le 28 schede del campione non sono
  state esaminate con questo scopo. Dichiararlo «nessun dato personale» sarebbe
  un'affermazione assoluta senza misura dietro; la verifica e' da fare prima che il servizio
  sia usato da persone diverse dall'autore.
- Le graduatorie e gli elenchi nominativi, quando pubblicati come allegati, non vanno
  raccolti ne' inviati a un modello linguistico (SPEC §8.2). Oggi gli allegati non sono
  scaricabili, quindi il caso non si presenta **per come e' configurato il sistema** — non
  per una proprieta' intrinseca: la regola resta necessaria e va applicata se l'accesso
  agli allegati venisse autorizzato.
- I dati personali trattati dal sistema sono quelli che gli utenti forniscono al bot. Non
  riguardano questo documento, ma l'informativa del bot (M2).

L'informativa di inPA descrive il trattamento dei candidati sul portale: **non e'**
un'autorizzazione al riuso automatizzato, e non viene usata come tale.

---

## 5. Cosa chiedere al gestore prima del funzionamento continuativo

La specifica (§3.1) chiede di chiarire le condizioni ambigue prima del polling di produzione.
Contatto indicato nelle pagine ufficiali: `inpa@funzionepubblica.it`
(per il sito: `redazioneweb@funzionepubblica.it`).

Tre domande, in ordine di importanza:

1. **Allegati.** Esiste un modo autorizzato di scaricare in automatico i documenti dei bandi,
   dato che `portale.inpa.gov.it` li vieta via `robots.txt`? Un servizio dedicato, un
   user-agent concordato, o una conferma che il divieto non intende coprire il recupero dei
   soli documenti linkati dalle schede pubbliche?
2. **Ritmo.** Esiste una frequenza di interrogazione ritenuta accettabile per
   `www.inpa.gov.it`? Il progetto usa oggi 1 richiesta ogni 2 secondi, sequenziale.
3. **Note legali.** La licenza CC-BY 3.0 delle Note legali del Dipartimento si applica anche
   ai contenuti di inPA?

Finche' la prima domanda non ha risposta, il progetto resta nel perimetro ridotto descritto
al §6. La deroga e' gia' prevista nel codice ma **disattivata per impostazione predefinita**:
`build_default_client(allow_attachment_host=...)` in `src/inpa_bot/source/adapter.py` la
abilita solo se il chiamante passa esplicitamente la motivazione dell'autorizzazione
ricevuta, e la decisione risultante resta marcata come deroga (`origin="override"`) nei dati,
non nascosta in una configurazione silenziosa.

---

## 6. Perimetro adottato, e cosa costa

**Consentito e adottato:**

```text
www.inpa.gov.it/sitemap.xml  →  sitemap-bandi.xml  →  1680 id di bandi attivi
                             →  /bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=<id>
                             →  metadati completi + riferimenti agli allegati
```

**Escluso:** download degli allegati, e con essi l'estrazione dei requisiti dai documenti.

**Cosa si perde davvero.** La milestone M3 (verifica dei requisiti dai documenti) dipende
interamente dagli allegati: nel campione della specifica il requisito formativo sta
nell'articolo 3 a pagina 5 del PDF, e la scheda sintetica non lo espone. Senza allegati il
bot puo' dire *quali* bandi esistono, dove e con quali scadenze, ma non puo' dire a un utente
se ne soddisfa i requisiti. E' una riduzione sostanziale dell'obiettivo finale, non un
dettaglio: va decisa da chi commissiona il lavoro, non aggirata dal codice.

**Cosa resta possibile e utile da subito.** Monitoraggio, filtri territoriali, scadenze,
promemoria, rilevazione di proroghe e nuovi allegati (la *comparsa* di un documento e' un
evento osservabile dalla scheda anche senza scaricarlo), e il link ufficiale da aprire per
leggere il bando. Per 3-4 utenti e' gia' la parte che fa risparmiare tempo ogni giorno.

---

## 7. Residui e limiti di questa verifica

- `robots.txt` puo' cambiare. Il client lo rilegge ogni 24 ore e rifiuta di proseguire se
  diventa irraggiungibile (rifiuto prudente, non permesso implicito).
- La verifica riguarda una sola giornata e una sola rete. Non dice nulla su blocchi
  geografici o su limiti applicati a volumi maggiori.
- Non e' stata ottenuta una risposta scritta del gestore. Fino ad allora, le conclusioni del
  §3 sulla licenza restano un'interpretazione ragionevole di un testo pubblico, non un
  permesso esplicito per questo specifico impiego.
- Nessuna richiesta e' stata inviata a `portale.inpa.gov.it` oltre al suo `robots.txt`:
  l'endpoint di ricerca candidato indicato in SPEC §2.1 e Appendice A.2 **non e' stato
  collaudato**, perche' collaudarlo avrebbe significato violare la regola appena letta. La
  sua validita' tecnica resta quindi ignota, e questo e' un esito deliberato.
