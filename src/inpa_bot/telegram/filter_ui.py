"""Filter screens: overview, regions and province selection by region."""

from __future__ import annotations

from inpa_bot.source.territory import PROVINCES, REGIONS

from .views import Preferences


def filter_screen(
    prefs: Preferences, section: str = "", region: str = ""
) -> tuple[str, list[list[dict[str, str]]]]:
    selected_regions = [r.name for r in REGIONS if r.code in prefs.regions]
    selected = []
    if selected_regions:
        selected.append("Regioni: " + ", ".join(selected_regions))
    if prefs.provinces:
        selected.append("Province: " + ", ".join(prefs.provinces))
    summary = "\n".join(selected) or "Regioni/province: tutte"
    if prefs.keywords:
        summary += "\nParole: " + prefs.keywords
    if prefs.regions and prefs.provinces:
        summary += "\nPer restringere alle province, togli le regioni."
    footer = [
        [
            {"text": "Indietro", "callback_data": "filters:"},
            {"text": "Menu", "callback_data": "menu:home"},
        ],
    ]
    if section == "regioni":
        items = [
            {
                "text": ("✓ " if r.code in prefs.regions else "") + r.name,
                "callback_data": f"region:{r.code}",
            }
            for r in REGIONS
        ]
        heading = "Regioni"
    elif section == "province" and not region:
        items = [
            {"text": r.name, "callback_data": f"provinces:{r.code}"} for r in REGIONS
        ]
        heading = "Province · scegli la regione"
    elif section == "province":
        name = next((r.name for r in REGIONS if r.code == region), None)
        if name is None:
            raise ValueError("Regione non riconosciuta")
        items = [
            {
                "text": ("✓ " if p.code in prefs.provinces else "")
                + f"{p.name} ({p.code})",
                "callback_data": f"province:{p.code}",
            }
            for p in sorted(PROVINCES, key=lambda p: p.name)
            if p.region_code == region
        ]
        heading = f"Province · {name}"
        footer.insert(
            0, [{"text": "Altra regione", "callback_data": "filters:province"}]
        )
    else:
        markup = [
            [
                {
                    "text": f"Nazionali: {'sì ✓' if prefs.include_national else 'no'}",
                    "callback_data": "flag:nazionali",
                }
            ],
            [
                {
                    "text": f"Territori incerti: {'sì ✓' if prefs.include_unresolved else 'no'}",
                    "callback_data": "flag:incerti",
                }
            ],
            [{"text": "Regioni", "callback_data": "filters:regioni"}],
            [{"text": "Provincia", "callback_data": "filters:province"}],
            [
                {"text": "Azzera filtri", "callback_data": "filters:reset"},
                {"text": "Vedi concorsi", "callback_data": "menu:concorsi"},
            ],
            [{"text": "Menu", "callback_data": "menu:home"}],
        ]
        return (
            "Filtri\n" + summary,
            markup,
        )
    markup = [items[i : i + 2] for i in range(0, len(items), 2)] + footer
    return heading + "\n\n" + summary, markup
