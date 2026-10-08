# Campioni

Piccoli estratti reali, conservati per rendere leggibile il contratto osservato senza
versionare interi dataset (SPEC §3.5).

| File | Cosa contiene |
|---|---|
| `sitemap-bandi.fragment.xml` | Prime 4 voci di `sitemap-bandi.xml` (1680 al 19/09/2026) |
| `detail-page.fragment.html` | Il solo `<article>` della scheda `d3d8d8d7…`, senza intestazione e pie' di pagina |
| `notice-snapshot.example.json` | La stessa scheda dopo il parsing, cioe' il contratto interno |

**Provenienza.** Tutto e' stato recuperato da `www.inpa.gov.it` il 19 settembre 2026 con
`InpaTelegramBot-Probe/0.1`. Sono contenuti pubblici, licenza CC-BY 3.0 IT
([Note legali](https://www.inpa.gov.it/note-legali/)).

**Sanitizzazione.** Non contengono cookie, header di risposta, token ne' dati di candidati:
sono atti amministrativi pubblici. Gli header delle risposte restano in `evidence/`, che e'
escluso dal versionamento.

Due dettagli visibili nel frammento HTML e utili a chi tocchera' il parser:

- dopo «Data chiusura candidature» il `<p>` e' chiuso da `</div>`. Il markup e' malformato in
  quel punto su tutti i campioni osservati: e' la ragione per cui `detail.py` scandisce un
  flusso di token invece di costruire un albero;
- i collegamenti agli allegati puntano a `portale.inpa.gov.it/api/media/<uuid>`, senza
  estensione nell'URL. Il tipo si deduce dal nome file e va confermato sui byte — ammesso di
  poterli scaricare, cosa che oggi `robots.txt` non consente
  (vedi [`../reports/access_conditions.md`](../reports/access_conditions.md)).

Il corpus completo delle 28 schede usato per ricavare il contratto **non** e' nel repository.
Sei schede rappresentative, scelte per i casi limite che coprono, stanno in
`tests/source/fixtures/detail/` e sono usate dai test.
