"""Plain-text views; public source text never becomes HTML or executable data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from inpa_bot.source.detail import build_detail_url
from inpa_bot.source.models import NoticeSnapshot, NoticeStatus
from inpa_bot.source.sitemap import NOTICE_ID_RE
from inpa_bot.source.territory import PROVINCES, REGIONS, matches_filter


@dataclass(frozen=True, slots=True)
class Preferences:
    regions: tuple[str, ...] = ()
    provinces: tuple[str, ...] = ()
    include_national: bool = True
    include_unresolved: bool = False
    keywords: str = ""


def clean(value: str | None, limit: int = 1000) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value or "Non dichiarato")
    encoded = text.encode("utf-16-le")
    return (
        text
        if len(encoded) <= 2 * limit
        else encoded[: 2 * (limit - 1)].decode("utf-16-le", errors="ignore") + "…"
    )


def notice_id_from_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.inpa.gov.it", "inpa.gov.it"}
        or parsed.path != "/bandi-e-avvisi/dettaglio-bando-avviso/"
        or parsed.fragment
    ):
        raise ValueError("Usa il link HTTPS ufficiale di dettaglio inPA")
    params = parse_qs(parsed.query, keep_blank_values=True)
    if set(params) != {"concorso_id"} or len(params["concorso_id"]) != 1:
        raise ValueError("Il link deve contenere un solo concorso_id")
    notice_id = params["concorso_id"][0]
    if not NOTICE_ID_RE.fullmatch(notice_id):
        raise ValueError("concorso_id non valido")
    return notice_id


def is_open(snapshot: NoticeSnapshot, now: datetime) -> bool:
    if snapshot.status_normalized != NoticeStatus.OPEN:
        return False
    closing = snapshot.closing_at
    if closing and closing.instant is not None:
        return closing.instant >= now
    if closing and closing.date_only is not None:
        return closing.date_only >= now.astimezone(ZoneInfo("Europe/Rome")).date()
    return True


def matches(snapshot: NoticeSnapshot, prefs: Preferences) -> bool:
    all_known = not prefs.regions and not prefs.provinces
    return matches_filter(
        snapshot.territories,
        region_codes=tuple(r.code for r in REGIONS) if all_known else prefs.regions,
        province_codes=tuple(p.code for p in PROVINCES)
        if all_known
        else prefs.provinces,
        include_national=prefs.include_national,
        include_unresolved=prefs.include_unresolved,
    ) and all(
        word.casefold() in (snapshot.title or "").casefold()
        for word in prefs.keywords.split()
    )


def card(snapshot: NoticeSnapshot, heading: str = "Bando") -> str:
    territories = ", ".join(t.raw for t in snapshot.territories) or "Da verificare"
    status = {"open": "Aperto", "closed": "Chiuso", "unknown": "Da verificare"}.get(
        snapshot.status_normalized.value, snapshot.status_normalized.value
    )
    return (
        (f"{heading}\n" if heading != "Bando" else "") + f"{clean(snapshot.title)}\n"
        f"Ente: {clean(', '.join(snapshot.enti), 400)}\n"
        f"Area dichiarata: {clean(territories, 400)}\n"
        f"Scadenza: {clean(snapshot.closing_at.raw if snapshot.closing_at else None, 150)}\n"
        f"Stato: {status}\n"
        "Sede e requisiti: verifica il bando."
    )


def buttons(notice_id: str) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "Apri inPA", "url": build_detail_url(notice_id)},
            {"text": "Segui", "callback_data": f"follow:{notice_id}"},
            {"text": "Nascondi", "callback_data": f"hide:{notice_id}"},
        ]
    ]
