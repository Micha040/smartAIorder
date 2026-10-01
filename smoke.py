"""Verbindungs- und Funktionstest ohne MCP/LLM. Ruft dieselben Funktionen auf wie die Tools.

  python smoke.py                               irgendein Auftrag aus XAN100 (zeigt echte Feldwerte + Org-Daten)
  python smoke.py kunde <KUNDE>                 letzter Auftrag des Kunden
  python smoke.py dublette <BESTELLNR> [KUNDE]  Dublettensuche
  python smoke.py anlegen <KUNDE> <PRODUKT> <MENGE> [--wirklich]
                                                Vorschau – mit --wirklich wird der Auftrag in SAP angelegt
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta

import server
from sap_client import SapError


def show(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def main(args: list[str]) -> None:
    try:
        match args:
            case []:
                show(await server.sap().sample_order())
            case ["kunde", kunde]:
                show(await server.letzten_auftrag_holen(kunde))
            case ["dublette", po, *rest]:
                show(await server.dublette_pruefen(po, rest[0] if rest else None))
            case ["anlegen", kunde, produkt, menge, *flags]:
                daten = dict(
                    kunde=kunde,
                    bestellnummer="SMOKE-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
                    wunschtermin=(date.today() + timedelta(days=14)).isoformat(),
                    positionen=[server.Position(produkt=produkt, menge=float(menge.replace(",", ".")))],
                )
                vorschau = await server.auftrag_anlegen(**daten)
                show(vorschau)
                if "--wirklich" in flags and "vorschau_id" in vorschau:
                    print("\n--> lege an ...")
                    show(await server.auftrag_anlegen(**daten, vorschau_id=vorschau["vorschau_id"]))
            case _:
                print(__doc__)
                sys.exit(2)
    finally:
        if server._sap:
            await server._sap.aclose()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main(sys.argv[1:]))
    except (SapError, ValueError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        sys.exit(1)
