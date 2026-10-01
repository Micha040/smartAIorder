from datetime import date

import pytest

from datum import wunschtermin_zu_datum as w

HEUTE = date(2026, 10, 1)  # Donnerstag, KW 40


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("2026-10-30", date(2026, 10, 30)),
        ("30.10.2026", date(2026, 10, 30)),
        ("30.10.26", date(2026, 10, 30)),
        ("30.10.", date(2026, 10, 30)),
        ("15.01.", date(2027, 1, 15)),  # ohne Jahr, schon vorbei -> nächstes Jahr
        ("KW 44", date(2026, 10, 26)),
        ("kw44", date(2026, 10, 26)),
        ("Lieferung KW 44", date(2026, 10, 26)),
        ("44. KW", date(2026, 10, 26)),
        ("Kalenderwoche 44", date(2026, 10, 26)),
        ("KW 40", date(2026, 9, 28)),  # laufende Woche bleibt dieses Jahr
        ("KW 2", date(2027, 1, 11)),  # schon vorbei -> nächstes Jahr
        ("KW 44/2027", date(2027, 11, 1)),
        ("2026-W44", date(2026, 10, 26)),
        ("44. KW 2026", date(2026, 10, 26)),  # nicht "KW 20"!
        ("44. Kalenderwoche 2026", date(2026, 10, 26)),
        ("Woche 2026-W44", date(2026, 10, 26)),
        ("KW 44 2027", date(2027, 11, 1)),
        ("30.10", date(2026, 10, 30)),
        ("bis 30.10.2026", date(2026, 10, 30)),
        ("Mo, 26.10.2026", date(2026, 10, 26)),
        ("2026-10-30T00:00:00", date(2026, 10, 30)),
    ],
)
def test_formate(text, erwartet):
    assert w(text, heute=HEUTE) == erwartet


def test_jahreswechsel():
    # 30.12.2026 liegt in KW 53/2026 -> "KW 1" ist die erste Woche 2027
    assert w("KW 1", heute=date(2026, 12, 30)) == date(2027, 1, 4)


@pytest.mark.parametrize("text", ["Ende Oktober", "bald", "KW 60", "31.02.2026", "KW 2026-44", "29.02."])
def test_ungueltig(text):
    with pytest.raises(ValueError):
        w(text, heute=HEUTE)


def test_fehlermeldung_deutsch():
    with pytest.raises(ValueError, match="Ungültiges Datum"):
        w("31.02.2026", heute=HEUTE)
