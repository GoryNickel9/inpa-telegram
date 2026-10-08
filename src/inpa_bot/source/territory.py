"""Anagrafica territoriale e riconoscimento dei territori (SPEC 6).

Tre prescrizioni della specifica hanno qui la forma di codice:

1. Un territorio non si risolve cercando una sigla dentro un titolo. Il
   riconoscimento avviene solo per corrispondenza esatta del valore intero,
   normalizzato: niente sottostringhe, niente fuzzy matching. Un falso
   positivo qui non si manifesta come errore, si manifesta come un bando
   dell'altra parte del paese recapitato a un utente che lo prende per buono.
2. L'anagrafica e' interna, esplicita e datata. La specifica avverte di non
   assumere che un elenco hardcoded trovato altrove sia aggiornato; quello
   che segue resta un elenco hardcoded, ma dichiara la data a cui si
   riferisce (`ANAGRAFICA_VERSION`), cosi' che un riordino amministrativo si
   presenti come dato scaduto da rivedere e non come comportamento anomalo.
3. Alias e valore originale si conservano. La fonte scrive "Emilia Romagna"
   dove la denominazione ufficiale e' "Emilia-Romagna", e "Trentino Alto
   Adige" dove l'ISO scrive "Trentino-Alto Adige/Südtirol": la
   normalizzazione serve a riconoscere quelle forme, non a sostituirle.

`ANAGRAFICA_VERSION` versiona i DATI, non il modulo: va incrementata quando
cambia una sigla, una denominazione o un alias, anche se il codice resta
identico. Chi persiste un `TerritoryValue` dovrebbe registrare accanto la
versione con cui e' stato risolto, perche' un riordino provinciale rende
rileggibile la stessa scheda con esito diverso.

Perimetro: 20 regioni e 107 unita' di livello provinciale (province, citta'
metropolitane, liberi consorzi siciliani, province autonome di Trento e
Bolzano). Non ci sono comuni: la fonte non li espone nel campo
"Area geografica" e un'anagrafica comunale non verificata varrebbe meno di
un dato assente.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from .models import Certainty, Provenance, TerritoryScope, TerritoryValue

ANAGRAFICA_VERSION = "2026-09-19.1"
"""Data dei dati territoriali, non del codice (formato `AAAA-MM-GG.n`)."""

NATIONAL_MARKERS: frozenset[str] = frozenset({"nazionale"})
"""Valori di "Area geografica" che indicano un ambito nazionale.

Osservato il 19/09/2026 sulla ricerca pubblica: il menu dei territori espone
"Nazionale" con id `-1`, accanto alle 20 regioni, e i bandi cosi' marcati
compaiono nel risultato di OGNI regione. Sono percio' una categoria a se',
non una regione e non un dato mancante.
"""


@dataclass(frozen=True, slots=True)
class Region:
    """Una regione con il suo codice ISO 3166-2:IT."""

    code: str
    name: str
    """Denominazione ufficiale, comprese le forme bilingui."""
    aliases: tuple[str, ...]
    """Varianti realmente osservate o d'uso corrente. Le differenze di sola
    punteggiatura o accento non servono qui: le appiattisce `normalize_key`."""


@dataclass(frozen=True, slots=True)
class Province:
    """Un'unita' di livello provinciale con la sua sigla automobilistica."""

    code: str
    name: str
    region_code: str
    aliases: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Anagrafica
# --------------------------------------------------------------------------

REGIONS: tuple[Region, ...] = (
    Region("IT-65", "Abruzzo", ()),
    Region("IT-77", "Basilicata", ()),
    Region("IT-78", "Calabria", ()),
    Region("IT-72", "Campania", ()),
    Region("IT-45", "Emilia-Romagna", ("Emilia Romagna",)),
    Region("IT-36", "Friuli-Venezia Giulia", ("Friuli Venezia Giulia",)),
    Region("IT-62", "Lazio", ()),
    Region("IT-42", "Liguria", ()),
    Region("IT-25", "Lombardia", ()),
    Region("IT-57", "Marche", ()),
    Region("IT-67", "Molise", ()),
    Region("IT-21", "Piemonte", ()),
    Region("IT-75", "Puglia", ()),
    Region("IT-88", "Sardegna", ()),
    Region("IT-82", "Sicilia", ("Regione Siciliana",)),
    Region("IT-52", "Toscana", ()),
    # "Trentino" da solo non e' qui: nell'uso corrente indica la provincia di
    # Trento, non la regione. Accettarlo come alias regionale trasformerebbe
    # un'imprecisione della fonte in un dato apparentemente certo.
    Region("IT-32", "Trentino-Alto Adige/Südtirol", ("Trentino Alto Adige", "Südtirol")),
    Region("IT-55", "Umbria", ()),
    Region("IT-23", "Valle d'Aosta/Vallée d'Aoste", ("Valle d'Aosta", "Valle Aosta")),
    Region("IT-34", "Veneto", ()),
)

PROVINCES: tuple[Province, ...] = (
    # Abruzzo
    Province("AQ", "L'Aquila", "IT-65"),
    Province("CH", "Chieti", "IT-65"),
    Province("PE", "Pescara", "IT-65"),
    Province("TE", "Teramo", "IT-65"),
    # Basilicata
    Province("MT", "Matera", "IT-77"),
    Province("PZ", "Potenza", "IT-77"),
    # Calabria
    Province("CZ", "Catanzaro", "IT-78"),
    Province("CS", "Cosenza", "IT-78"),
    Province("KR", "Crotone", "IT-78"),
    Province("RC", "Reggio di Calabria", "IT-78", ("Reggio Calabria",)),
    Province("VV", "Vibo Valentia", "IT-78"),
    # Campania
    Province("AV", "Avellino", "IT-72"),
    Province("BN", "Benevento", "IT-72"),
    Province("CE", "Caserta", "IT-72"),
    Province("NA", "Napoli", "IT-72"),
    Province("SA", "Salerno", "IT-72"),
    # Emilia-Romagna
    Province("BO", "Bologna", "IT-45"),
    Province("FE", "Ferrara", "IT-45"),
    Province("FC", "Forlì-Cesena", "IT-45"),
    Province("MO", "Modena", "IT-45"),
    Province("PR", "Parma", "IT-45"),
    Province("PC", "Piacenza", "IT-45"),
    Province("RA", "Ravenna", "IT-45"),
    Province("RE", "Reggio nell'Emilia", "IT-45", ("Reggio Emilia",)),
    Province("RN", "Rimini", "IT-45"),
    # Friuli-Venezia Giulia
    Province("GO", "Gorizia", "IT-36"),
    Province("PN", "Pordenone", "IT-36"),
    Province("TS", "Trieste", "IT-36"),
    Province("UD", "Udine", "IT-36"),
    # Lazio
    Province("FR", "Frosinone", "IT-62"),
    Province("LT", "Latina", "IT-62"),
    Province("RI", "Rieti", "IT-62"),
    Province("RM", "Roma", "IT-62", ("Roma Capitale",)),
    Province("VT", "Viterbo", "IT-62"),
    # Liguria
    Province("GE", "Genova", "IT-42"),
    Province("IM", "Imperia", "IT-42"),
    Province("SP", "La Spezia", "IT-42"),
    Province("SV", "Savona", "IT-42"),
    # Lombardia
    Province("BG", "Bergamo", "IT-25"),
    Province("BS", "Brescia", "IT-25"),
    Province("CO", "Como", "IT-25"),
    Province("CR", "Cremona", "IT-25"),
    Province("LC", "Lecco", "IT-25"),
    Province("LO", "Lodi", "IT-25"),
    Province("MN", "Mantova", "IT-25"),
    Province("MI", "Milano", "IT-25"),
    Province("MB", "Monza e della Brianza", "IT-25", ("Monza e Brianza", "Monza Brianza")),
    Province("PV", "Pavia", "IT-25"),
    Province("SO", "Sondrio", "IT-25"),
    Province("VA", "Varese", "IT-25"),
    # Marche
    Province("AN", "Ancona", "IT-57"),
    Province("AP", "Ascoli Piceno", "IT-57"),
    Province("FM", "Fermo", "IT-57"),
    Province("MC", "Macerata", "IT-57"),
    Province("PU", "Pesaro e Urbino", "IT-57"),
    # Molise
    Province("CB", "Campobasso", "IT-67"),
    Province("IS", "Isernia", "IT-67"),
    # Piemonte
    Province("AL", "Alessandria", "IT-21"),
    Province("AT", "Asti", "IT-21"),
    Province("BI", "Biella", "IT-21"),
    Province("CN", "Cuneo", "IT-21"),
    Province("NO", "Novara", "IT-21"),
    Province("TO", "Torino", "IT-21"),
    Province("VB", "Verbano-Cusio-Ossola", "IT-21", ("Verbania",)),
    Province("VC", "Vercelli", "IT-21"),
    # Puglia
    Province("BA", "Bari", "IT-75"),
    Province("BT", "Barletta-Andria-Trani", "IT-75"),
    Province("BR", "Brindisi", "IT-75"),
    Province("FG", "Foggia", "IT-75"),
    Province("LE", "Lecce", "IT-75"),
    Province("TA", "Taranto", "IT-75"),
    # Sardegna
    Province("CA", "Cagliari", "IT-88"),
    Province("NU", "Nuoro", "IT-88"),
    Province("OR", "Oristano", "IT-88"),
    Province("SS", "Sassari", "IT-88"),
    Province("SU", "Sud Sardegna", "IT-88"),
    # Sicilia
    Province("AG", "Agrigento", "IT-82"),
    Province("CL", "Caltanissetta", "IT-82"),
    Province("CT", "Catania", "IT-82"),
    Province("EN", "Enna", "IT-82"),
    Province("ME", "Messina", "IT-82"),
    Province("PA", "Palermo", "IT-82"),
    Province("RG", "Ragusa", "IT-82"),
    Province("SR", "Siracusa", "IT-82"),
    Province("TP", "Trapani", "IT-82"),
    # Toscana
    Province("AR", "Arezzo", "IT-52"),
    Province("FI", "Firenze", "IT-52"),
    Province("GR", "Grosseto", "IT-52"),
    Province("LI", "Livorno", "IT-52"),
    Province("LU", "Lucca", "IT-52"),
    Province("MS", "Massa-Carrara", "IT-52", ("Massa e Carrara",)),
    Province("PI", "Pisa", "IT-52"),
    Province("PT", "Pistoia", "IT-52"),
    Province("PO", "Prato", "IT-52"),
    Province("SI", "Siena", "IT-52"),
    # Trentino-Alto Adige/Südtirol
    Province("BZ", "Bolzano/Bozen", "IT-32", ("Bolzano", "Bozen", "Alto Adige")),
    Province("TN", "Trento", "IT-32", ("Provincia autonoma di Trento",)),
    # Umbria
    Province("PG", "Perugia", "IT-55"),
    Province("TR", "Terni", "IT-55"),
    # Valle d'Aosta/Vallée d'Aoste
    Province("AO", "Aosta", "IT-23"),
    # Veneto
    Province("BL", "Belluno", "IT-34"),
    Province("PD", "Padova", "IT-34"),
    Province("RO", "Rovigo", "IT-34"),
    Province("TV", "Treviso", "IT-34"),
    Province("VE", "Venezia", "IT-34"),
    Province("VR", "Verona", "IT-34"),
    Province("VI", "Vicenza", "IT-34"),
)


# --------------------------------------------------------------------------
# Normalizzazione e ricerca
# --------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[-/'’ʼ`.,_()\[\]]+")
"""Punteggiatura e separatori resi equivalenti a uno spazio. Include
l'apostrofo tipografico perche' l'HTML della fonte alterna `'` e `’`."""


def normalize_key(raw: str) -> str:
    """Chiave di confronto: minuscolo, senza accenti, senza punteggiatura.

    Serve perche' la fonte scrive "Emilia Romagna" dove la denominazione
    ufficiale e' "Emilia-Romagna": senza questo appiattimento servirebbe un
    alias per ogni combinazione di trattino, apostrofo e accento.

    La decomposizione e' NFKD con successiva rimozione dei segni combinanti,
    non una tabella di sostituzione: `casefold` invece di `lower` perche'
    tratta anche le forme tedesche dei toponimi altoatesini.
    """
    decomposed = unicodedata.normalize("NFKD", raw)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    flattened = _SEPARATORS.sub(" ", unaccented.casefold())
    return " ".join(flattened.split())


_Entry = TypeVar("_Entry", Region, Province)


def _register(index: dict[str, _Entry], entry: _Entry, keys: Iterable[str]) -> None:
    """Indicizza una voce, rifiutando le chiavi ambigue.

    Una collisione fra due voci diverse non e' recuperabile a runtime: una
    delle due sparirebbe in silenzio e il riconoscimento darebbe sempre
    l'altra. Meglio un errore all'import, che si vede in fase di sviluppo.
    """
    for key in keys:
        normalized = normalize_key(key)
        if not normalized:
            raise ValueError(
                f"anagrafica {ANAGRAFICA_VERSION}: chiave vuota per {entry.code}"
            )
        previous = index.get(normalized)
        if previous is not None and previous is not entry:
            raise ValueError(
                f"anagrafica {ANAGRAFICA_VERSION}: chiave '{normalized}' ambigua "
                f"fra {previous.code} e {entry.code}"
            )
        index[normalized] = entry


def _check_anagrafica() -> None:
    """Verifica di consistenza eseguita all'import.

    I tre errori plausibili in una tabella scritta a mano sono: una voce
    dimenticata, una sigla ripetuta e una provincia agganciata a una regione
    inesistente. Nessuno dei tre si manifesterebbe come eccezione piu' tardi:
    diventerebbe un bando filtrato male.
    """
    if len(REGIONS) != 20:
        raise ValueError(f"attese 20 regioni, presenti {len(REGIONS)}")
    if len(PROVINCES) != 107:
        raise ValueError(f"attese 107 province, presenti {len(PROVINCES)}")
    region_codes = {region.code for region in REGIONS}
    if len(region_codes) != len(REGIONS):
        raise ValueError("codici regione duplicati nell'anagrafica")
    province_codes = {province.code for province in PROVINCES}
    if len(province_codes) != len(PROVINCES):
        raise ValueError("sigle provinciali duplicate nell'anagrafica")
    orphans = sorted(p.code for p in PROVINCES if p.region_code not in region_codes)
    if orphans:
        raise ValueError(f"province senza regione corrispondente: {', '.join(orphans)}")


_check_anagrafica()

_REGION_BY_CODE: dict[str, Region] = {region.code: region for region in REGIONS}

_REGION_INDEX: dict[str, Region] = {}
_PROVINCE_INDEX: dict[str, Province] = {}

for _region in REGIONS:
    # Anche il codice e' una chiave: un filtro utente viaggia come "IT-88",
    # non come "Sardegna". Resta una corrispondenza sul valore intero.
    _register(_REGION_INDEX, _region, (_region.code, _region.name, *_region.aliases))

for _province in PROVINCES:
    _register(
        _PROVINCE_INDEX, _province,
        (_province.code, _province.name, *_province.aliases),
    )

del _region, _province


def find_region(raw: str) -> Region | None:
    """Regione il cui nome, alias o codice coincide esattamente con `raw`.

    Solo corrispondenza esatta della chiave normalizzata. Nessun fuzzy
    matching e nessuna ricerca per sottostringa: "Regione Lombardia e
    Provincia di Como" non e' la Lombardia, e indovinarlo produrrebbe un dato
    falso ma indistinguibile da uno vero (SPEC 6).
    """
    return _REGION_INDEX.get(normalize_key(raw))


def find_province(raw: str) -> Province | None:
    """Provincia il cui nome, alias o sigla coincide esattamente con `raw`.

    La sigla e' accettata solo quando e' l'intero valore: e' il caso di un
    filtro utente o del parametro `provinciaCodice` (SPEC appendice A.3).
    Cercare la stessa sigla dentro un testo piu' lungo e' invece la pratica
    che la specifica vieta: "PA" in un titolo e' quasi sempre "pubblica
    amministrazione", non Palermo.
    """
    return _PROVINCE_INDEX.get(normalize_key(raw))


# --------------------------------------------------------------------------
# Lettura del campo "Area geografica"
# --------------------------------------------------------------------------


def parse_area_geografica(
    raw: str, *, scope: TerritoryScope, provenance: Provenance
) -> tuple[TerritoryValue, ...]:
    """Scompone il campo "Area geografica" in territori normalizzati.

    Il campo osservato e' un elenco separato da ", " (fino a 14 regioni in un
    solo bando). Di ogni elemento si conserva il testo originale: la
    normalizzazione si affianca al valore grezzo, non lo sostituisce (SPEC 6).

    Un elemento non riconosciuto resta `AMBIGUOUS` senza codici. E' la regola
    "nessun valore inventato" di SPEC 4.1 applicata al territorio: meglio un
    "territorio da verificare" che una regione plausibile.

    La certezza dei valori riconosciuti e' `DERIVED`, non `OBSERVED`: la
    fonte pubblica un nome, non un codice ISO o una sigla. Il codice e'
    prodotto da questa anagrafica ed e' vero quanto lo e' la versione
    `ANAGRAFICA_VERSION`.

    `scope` arriva dal chiamante e non viene indovinato: il parser della
    scheda passa `UNSPECIFIED` perche' la fonte non dichiara se
    "Area geografica" sia sede di lavoro, ambito del bando o sede d'esame, e
    trattarla come sede di lavoro sarebbe la confusione vietata da SPEC 6.
    """
    values: list[TerritoryValue] = []
    for chunk in raw.split(","):
        element = chunk.strip()
        if not element:
            continue
        if normalize_key(element) in NATIONAL_MARKERS:
            values.append(TerritoryValue(
                raw=element, scope=scope, provenance=provenance,
                national=True, certainty=Certainty.OBSERVED,
            ))
            continue
        region = find_region(element)
        if region is not None:
            values.append(TerritoryValue(
                raw=element, scope=scope, provenance=provenance,
                region_code=region.code, region_name=region.name,
                certainty=Certainty.DERIVED,
            ))
            continue
        province = find_province(element)
        if province is not None:
            parent = _REGION_BY_CODE[province.region_code]
            values.append(TerritoryValue(
                raw=element, scope=scope, provenance=provenance,
                region_code=parent.code, region_name=parent.name,
                province_code=province.code, province_name=province.name,
                certainty=Certainty.DERIVED,
            ))
            continue
        values.append(TerritoryValue(
            raw=element, scope=scope, provenance=provenance,
            certainty=Certainty.AMBIGUOUS,
        ))
    return tuple(values)


# --------------------------------------------------------------------------
# Filtro utente
# --------------------------------------------------------------------------


def matches_filter(
    territories: Sequence[TerritoryValue], *,
    region_codes: Collection[str] = (),
    province_codes: Collection[str] = (),
    include_unresolved: bool = True,
    include_national: bool = True,
) -> bool:
    """Vero se questi territori soddisfano la preferenza geografica.

    Il confronto e' deliberatamente asimmetrico fra i due livelli:

    * una provincia richiesta e' soddisfatta solo da un territorio risolto a
      livello provinciale. Un territorio noto come "Sardegna" NON soddisfa la
      richiesta "SS": i posti potrebbero essere tutti a Cagliari, e SPEC 6
      vieta di assegnare indistintamente a una provincia i posti di un bando
      regionale o multiregionale;
    * una regione richiesta e' soddisfatta anche da un territorio risolto a
      livello provinciale al suo interno. Qui non si deduce nulla: Sassari e'
      in Sardegna in ogni caso.

    `include_unresolved` governa il caso "localita' assente o ambigua", che
    SPEC 6 vuole segnalato come "territorio da verificare" e non scartato in
    silenzio. Con il valore predefinito un bando dal territorio irrisolto
    passa il filtro e resta visibile; spetta al livello di presentazione
    marcarlo come da verificare. L'assenza totale di territori e' trattata
    allo stesso modo: e' la stessa ignoranza, espressa con zero elementi.

    Un territorio risolto alla sola regione, di fronte a una richiesta
    provinciale, non e' ne' una corrispondenza ne' un irrisolto: e' un dato
    certo a una granularita' piu' grossa. Chi vuole vedere anche quei bandi
    aggiunge la regione ai propri `region_codes`.

    `include_national` riguarda i bandi marcati "Nazionale". Il valore
    predefinito li fa passare, per due ragioni: e' il comportamento della
    ricerca pubblica di inPA (filtrando per una regione qualsiasi i bandi
    nazionali compaiono comunque), e per chi cerca lavoro un concorso
    nazionale e' pertinente ovunque risieda. Resta un interruttore separato
    da `include_unresolved` perche' le due situazioni non si somigliano: un
    bando nazionale ha un ambito noto e ampio, un bando irrisolto ha un
    ambito ignoto.
    """
    wanted_regions = {code.strip().upper() for code in region_codes if code.strip()}
    wanted_provinces = {code.strip().upper() for code in province_codes if code.strip()}
    if not wanted_regions and not wanted_provinces:
        return True
    unresolved = not territories
    for territory in territories:
        if territory.national:
            if include_national:
                return True
            continue
        if (territory.province_code is not None
                and territory.province_code.upper() in wanted_provinces):
            return True
        if (territory.region_code is not None
                and territory.region_code.upper() in wanted_regions):
            return True
        if not territory.normalized:
            unresolved = True
    return unresolved and include_unresolved
