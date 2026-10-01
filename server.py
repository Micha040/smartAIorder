"""MCP-Server "Bestell-Mail -> Kundenauftrag" (Teamtag 2026).

Start:  python server.py           (stdio – Standard für die meisten MCP-Clients)
        python server.py --http    (Streamable HTTP auf MCP_HOST:MCP_PORT, Endpoint /mcp – z.B. für Open WebUI)

WICHTIG bei stdio: niemals print() – stdout gehört dem MCP-Protokoll. Logging geht auf stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from datum import wunschtermin_zu_datum
from sap_client import SapClient, SapError

log = logging.getLogger("sap-order-mcp")

mcp = FastMCP(
    "sap-order-mcp",
    instructions=(
        "Werkzeuge, um aus einer Bestell-Mail einen SAP-Kundenauftrag anzulegen. Ablauf: "
        "1) Kunde, Kundenbestellnummer, Wunschtermin und Positionen aus der Mail extrahieren. "
        "2) Bei 'wie letztes Mal' oder unbekannter Produktnummer letzten_auftrag_holen nutzen. "
        "3) dublette_pruefen aufrufen. "
        "4) auftrag_anlegen mit bestaetigt=false aufrufen und dem Nutzer die Vorschau zeigen. "
        "5) Erst nach ausdrücklicher Zustimmung des Nutzers auftrag_anlegen mit bestaetigt=true aufrufen. "
        "Nichts erfinden: fehlende Angaben beim Nutzer erfragen."
    ),
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
)

_sap: SapClient | None = None


def sap() -> SapClient:
    """SAP-Client erst beim ersten Tool-Aufruf bauen – so startet der Server auch ohne .env und meldet den Fehler sauber."""
    global _sap
    if _sap is None:
        _sap = SapClient()
    return _sap


def _positionen(order: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "position": i.get("SalesOrderItem"),
            "produkt": i.get("Product"),
            "text": i.get("SalesOrderItemText"),
            "menge": i.get("RequestedQuantity"),
            "einheit": i.get("RequestedQuantityUnit"),
        }
        for i in order.get("_Item") or []
    ]


# --- Tools ---------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def letzten_auftrag_holen(
    kunde: Annotated[str, Field(description="SAP-Kundennummer (SoldToParty)")],
) -> dict[str, Any]:
    """Liefert den letzten Kundenauftrag eines Kunden mit Positionen. Nutzen bei 'wie letztes Mal' oder um Produktnummern zu finden."""
    o = await sap().get_last_order(kunde)
    if not o:
        return {"gefunden": False, "hinweis": f"Kein Auftrag für Kunde {kunde} gefunden."}
    return {
        "gefunden": True,
        "auftrag": o.get("SalesOrder"),
        "datum": o.get("CreationDate"),
        "bestellnummer": o.get("PurchaseOrderByCustomer"),
        "positionen": _positionen(o),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def dublette_pruefen(
    bestellnummer: Annotated[str, Field(description="Bestellnummer des Kunden aus der Mail (PurchaseOrderByCustomer)")],
    kunde: Annotated[str | None, Field(description="SAP-Kundennummer. Leer lassen = über alle Kunden suchen")] = None,
) -> dict[str, Any]:
    """Prüft, ob es zu einer Kundenbestellnummer schon einen Auftrag gibt. IMMER vor dem Anlegen aufrufen."""
    hits = await sap().find_orders_by_po(bestellnummer, kunde or None)
    if hits:
        return {"dublette": True, "warnung": "Achtung: Diese Bestellung wurde schon erfasst!", "vorhandene_auftraege": hits}
    return {"dublette": False}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def auftrag_anzeigen(
    auftrag: Annotated[str, Field(description="SAP-Kundenauftragsnummer (SalesOrder)")],
) -> dict[str, Any]:
    """Zeigt einen Kundenauftrag mit Kopfdaten und Positionen – z.B. um einen frisch angelegten Auftrag zu kontrollieren."""
    o = await sap().get_order(auftrag)
    if not o:
        return {"gefunden": False, "hinweis": f"Auftrag {auftrag} existiert nicht."}
    return {
        "gefunden": True,
        "auftrag": o.get("SalesOrder"),
        "auftragsart": o.get("SalesOrderType"),
        "kunde": o.get("SoldToParty"),
        "bestellnummer": o.get("PurchaseOrderByCustomer"),
        "angelegt_am": o.get("CreationDate"),
        "wunschtermin": o.get("RequestedDeliveryDate"),
        "nettowert": o.get("TotalNetAmount"),
        "waehrung": o.get("TransactionCurrency"),
        "positionen": _positionen(o),
    }


class Position(BaseModel):
    produkt: str = Field(description="SAP-Produktnummer (Product)")
    menge: float = Field(gt=0, description="Bestellmenge")
    einheit: str | None = Field(default=None, description="Mengeneinheit, z.B. 'ST'. Leer = SAP ermittelt sie selbst")


async def _org_daten(kunde: str) -> tuple[dict[str, str], str | None]:
    """Auftragsart, VkOrg, Vertriebsweg, Sparte: vom letzten Auftrag des Kunden, sonst aus .env."""
    vorlage = await sap().get_last_order(kunde)
    keys = ("SalesOrderType", "SalesOrganization", "DistributionChannel", "OrganizationDivision")
    if vorlage and all(vorlage.get(k) for k in keys):
        return {k: vorlage[k] for k in keys}, vorlage.get("SalesOrder")
    env = {
        "SalesOrderType": os.getenv("SAP_DEFAULT_SALES_ORDER_TYPE"),
        "SalesOrganization": os.getenv("SAP_DEFAULT_SALES_ORGANIZATION"),
        "DistributionChannel": os.getenv("SAP_DEFAULT_DISTRIBUTION_CHANNEL"),
        "OrganizationDivision": os.getenv("SAP_DEFAULT_DIVISION"),
    }
    if not all(env.values()):
        raise SapError(
            f"Kunde {kunde} hat noch keinen Auftrag als Vorlage und die SAP_DEFAULT_*-Werte in .env fehlen – "
            "Org-Daten (Auftragsart, Verkaufsorganisation, Vertriebsweg, Sparte) können nicht bestimmt werden."
        )
    return env, None  # type: ignore[return-value]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def auftrag_anlegen(
    kunde: Annotated[str, Field(description="SAP-Kundennummer (SoldToParty)")],
    bestellnummer: Annotated[str, Field(max_length=35, description="Bestellnummer des Kunden aus der Mail")],
    wunschtermin: Annotated[str, Field(description="Liefertermin wie in der Mail, z.B. 'KW 44', '30.10.2026' oder '2026-10-30'")],
    positionen: Annotated[list[Position], Field(min_length=1, description="Bestellte Positionen")],
    bestaetigt: Annotated[bool, Field(description="false = nur Vorschau. true NUR nach ausdrücklicher Zustimmung des Nutzers")] = False,
) -> dict[str, Any]:
    """Legt einen Kundenauftrag in SAP an. Erst mit bestaetigt=false aufrufen und die Vorschau dem Nutzer zeigen. Nur wenn der Nutzer ausdrücklich zustimmt, erneut mit bestaetigt=true aufrufen."""
    # Harte Regel im Server, unabhängig davon, was das LLM tut:
    dup = await sap().find_orders_by_po(bestellnummer, kunde)
    if dup:
        return {"abgelehnt": "Dublette – Auftrag mit dieser Bestellnummer existiert bereits", "vorhandene_auftraege": dup}

    liefertermin = wunschtermin_zu_datum(wunschtermin)
    org, vorlage_nr = await _org_daten(kunde)

    payload: dict[str, Any] = {
        **org,
        "SoldToParty": kunde,
        "PurchaseOrderByCustomer": bestellnummer,
        "RequestedDeliveryDate": liefertermin.isoformat(),
        "_Item": [
            {"Product": p.produkt, "RequestedQuantity": p.menge}
            | ({"RequestedQuantityUnit": p.einheit} if p.einheit else {})
            for p in positionen
        ],
    }

    if not bestaetigt:
        vorschau: dict[str, Any] = {
            "vorschau": payload,
            "org_daten_von": f"Auftrag {vorlage_nr}" if vorlage_nr else ".env-Standardwerte",
            "hinweis": "Noch NICHT angelegt. Vorschau dem Nutzer zeigen und um Bestätigung bitten.",
        }
        if liefertermin < date.today():
            vorschau["warnung"] = f"Wunschtermin {liefertermin.isoformat()} liegt in der Vergangenheit."
        return vorschau

    res = await sap().create_order(payload)
    log.info("Auftrag %s angelegt (Kunde %s, Bestellnr. %s)", res.get("SalesOrder"), kunde, bestellnummer)
    return {
        "angelegt": True,
        "auftrag": res.get("SalesOrder"),
        "nettowert": res.get("TotalNetAmount"),
        "waehrung": res.get("TransactionCurrency"),
    }


if __name__ == "__main__":
    sys.stderr.reconfigure(encoding="utf-8")  # Windows: Umlaute in Client-Logs nicht zerschießen
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    if "--http" in sys.argv[1:]:
        log.info("sap-order-mcp läuft (Streamable HTTP) auf http://%s:%s/mcp", mcp.settings.host, mcp.settings.port)
        mcp.run(transport="streamable-http")
    else:
        log.info("sap-order-mcp läuft (stdio)")
        mcp.run()
