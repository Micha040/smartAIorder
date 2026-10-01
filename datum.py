"""Wunschtermin aus Mail-Text -> Datum. Rechnen macht der Server, nicht das LLM."""

from __future__ import annotations

import re
from datetime import date

_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_DE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})?\s*$")  # 30.10.2026 / 30.10.26 / 30.10.
_ISO_WEEK = re.compile(r"^\s*(\d{4})-?W(\d{1,2})\s*$", re.I)  # 2026-W44
_KW = re.compile(r"(?:KW|Kalenderwoche|Woche)\s*(\d{1,2})(?:\s*[/.\-,]?\s*(\d{4}))?", re.I)  # KW 44, KW44/2027
_KW_NACH = re.compile(r"(\d{1,2})\.\s*(?:KW|Kalenderwoche|Woche)(?:\s*(\d{4}))?", re.I)  # 44. KW


def _woche(jahr: int, kw: int) -> date:
    try:
        return date.fromisocalendar(jahr, kw, 1)  # Montag der ISO-Woche
    except ValueError:
        raise ValueError(f"KW {kw} gibt es {jahr} nicht") from None


def wunschtermin_zu_datum(text: str, heute: date | None = None) -> date:
    """'KW 44', '44. KW', '2026-W44', '30.10.2026', '30.10.' oder '2026-10-30' -> date.

    KW ergibt den Montag der Woche. Ohne Jahr gilt das aktuelle Jahr – liegt der Termin
    dann schon in der Vergangenheit, das nächste.
    """
    heute = heute or date.today()
    s = str(text).strip()

    if m := _ISO.match(s):
        return date(int(m[1]), int(m[2]), int(m[3]))

    if m := _DE.match(s):
        tag, monat, jahr = int(m[1]), int(m[2]), m[3]
        if jahr:
            return date(int(jahr) + (2000 if len(jahr) == 2 else 0), monat, tag)
        d = date(heute.year, monat, tag)
        return d if d >= heute else date(heute.year + 1, monat, tag)

    if m := _ISO_WEEK.match(s):
        return _woche(int(m[1]), int(m[2]))

    if m := _KW.search(s) or _KW_NACH.search(s):
        kw = int(m[1])
        if m[2]:
            return _woche(int(m[2]), kw)
        jahr = heute.year
        if date.fromisocalendar(*heute.isocalendar()[:2], 1) > _woche(jahr, kw):
            jahr += 1
        return _woche(jahr, kw)

    raise ValueError(f"Wunschtermin nicht erkannt: {text!r} – bitte als 'KW 44', '30.10.2026' oder '2026-10-30' angeben")
