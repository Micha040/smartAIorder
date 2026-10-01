"""MCP-Server "Bestell-Mail -> Kundenauftrag" (Teamtag 2026).

Start:  python server.py           (stdio – Standard, z.B. für Odysseus)
        python server.py --http    (Streamable HTTP auf MCP_HOST:MCP_PORT, Endpoint /mcp – z.B. für Open WebUI)

WICHTIG bei stdio: niemals print() – stdout gehört dem MCP-Protokoll. Logging geht auf stderr.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field, model_validator

from datum import wunschtermin_zu_datum
from sap_client import SapClient, SapError  # lädt auch die .env

log = logging.getLogger("sap-order-mcp")

_HOST = os.getenv("MCP_HOST", "127.0.0.1")
# Nur für --http. Lokal gebunden: Schutz gegen DNS-Rebinding (host.docker.internal erlaubt).
# MCP_HOST=0.0.0.0 (Odysseus/Open WebUI in Docker, WSL oder auf anderem Rechner): kein Host-Check,
# denn dann kommen Anfragen mit wechselnden Hostnamen/IPs.
_SECURITY = (
    TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    if _HOST in ("127.0.0.1", "localhost", "::1")
    else TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

mcp = FastMCP(
    "sap-order-mcp",
    instructions=(
        "Werkzeuge, um aus einer Bestell-Mail einen SAP-Kundenauftrag anzulegen. Ablauf: "
        "1) Kunde, Kundenbestellnummer, Wunschtermin und Positionen aus der Mail extrahieren. "
        "2) Bei 'wie letztes Mal' oder unbekannter Produktnummer letzten_auftrag_holen nutzen. "
        "3) dublette_pruefen aufrufen. "
        "4) auftrag_anlegen OHNE vorschau_id aufrufen und dem Nutzer die Vorschau zeigen. "
        "5) Erst nach ausdrücklicher Zustimmung des Nutzers auftrag_anlegen mit denselben Daten und der vorschau_id erneut aufrufen. "
        "Nichts erfinden: fehlende Angaben (z.B. Bestellnummer, Kundennummer) beim Nutzer erfragen."
    ),
    host=_HOST,
    port=int(os.getenv("MCP_PORT", "8000")),
    transport_security=_SECURITY,
)

_sap: SapClient | None = None


def sap() -> SapClient:
    """SAP-Client erst beim ersten Tool-Aufruf bauen – so startet der Server auch ohne .env und meldet den Fehler sauber."""
    global _sap
    if _sap is None:
        _sap = SapClient()
    return _sap


def _protokoll(fn):
    """Jeden Tool-Aufruf mit Argumenten und Ergebnis/Fehler ins Log schreiben (server.log)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        log.info("Tool %s %s", fn.__name__, kwargs or args)
        try:
            res = await fn(*args, **kwargs)
        except Exception as e:
            log.warning("Tool %s -> Fehler: %s", fn.__name__, e)
            raise
        log.info("Tool %s -> ok", fn.__name__)
        return res

    return wrapper


# --- Tolerante Eingabetypen: lokale Modelle schicken Nummern gern als Zahl, Mengen als "1,5" ---


def _als_text(v: Any) -> Any:
    if v is None or isinstance(v, str):
        return v.strip() if v else v
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v) if isinstance(v, int) else v


def _als_zahl(v: Any) -> Any:
    return v.strip().replace(",", ".") if isinstance(v, str) else v


def _als_liste(v: Any) -> Any:
    return [v] if isinstance(v, dict) else v


Text = Annotated[str, BeforeValidator(_als_text)]
Nummer = Annotated[str, BeforeValidator(_als_text), Field(min_length=1)]


def _positionen(order: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "position": i.get("SalesOrderItem"),
            "produkt": i.get("Product"),
            "text": i.get("SalesOrderItemText"),
            "menge": i.get("RequestedQuantity"),
            "einheit": i.get("RequestedQuantitySAPUnit"),
        }
        for i in order.get("_Item") or []
    ]


# --- Tools ---------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_protokoll
async def letzten_auftrag_holen(
    kunde: Annotated[Nummer, Field(description="SAP-Kundennummer (SoldToParty)")],
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
@_protokoll
async def dublette_pruefen(
    bestellnummer: Annotated[Nummer, Field(max_length=35, description="Bestellnummer des Kunden aus der Mail (PurchaseOrderByCustomer)")],
    kunde: Annotated[Text | None, Field(description="SAP-Kundennummer. Leer lassen = über alle Kunden suchen")] = None,
) -> dict[str, Any]:
    """Prüft, ob es zu einer Kundenbestellnummer schon einen Auftrag gibt. IMMER vor dem Anlegen aufrufen."""
    hits = await sap().find_orders_by_po(bestellnummer, kunde or None)
    if hits:
        return {"dublette": True, "warnung": "Achtung: Diese Bestellung wurde schon erfasst!", "vorhandene_auftraege": hits}
    return {"dublette": False}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_protokoll
async def auftrag_anzeigen(
    auftrag: Annotated[Nummer, Field(description="SAP-Kundenauftragsnummer (SalesOrder)")],
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


# Feldnamen, die lokale Modelle für Positionen erfinden. Schlüssel normalisiert: klein, ohne _ - Leerzeichen.
_POS_ALIASE = {
    "produkt": ("produkt", "product", "material", "artikel", "produktnummer", "productnumber", "productid",
                "artikelnummer", "artikelnr", "materialnummer", "materialnr", "sku"),
    "menge": ("menge", "quantity", "qty", "anzahl", "stueck", "stück", "requestedquantity"),
    "einheit": ("einheit", "unit", "uom", "mengeneinheit", "requestedquantitysapunit"),
}
# Nur wenn sonst kein Produkt erkennbar ist – so nennen kleine Modelle die Produktnummer auch gern (z.B. "pos_nummer").
_PRODUKT_NOTNAGEL = ("posnummer", "positionsnummer", "nummer", "nr", "id", "item")


class Position(BaseModel):
    produkt: Nummer = Field(description="SAP-Produktnummer (Product), z.B. 'ZJCG920'")
    menge: Annotated[float, BeforeValidator(_als_zahl)] = Field(gt=0, description="Bestellmenge")
    einheit: Text | None = Field(
        default=None, description="SAP-Mengeneinheit, z.B. 'ST' oder 'KG'. Leer lassen = SAP ermittelt sie selbst"
    )

    @model_validator(mode="before")
    @classmethod
    def _felder_zuordnen(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        roh = {re.sub(r"[\s_\-]", "", str(k)).lower(): w for k, w in v.items()}
        pos = {feld: next((roh[a] for a in aliase if roh.get(a) is not None), None) for feld, aliase in _POS_ALIASE.items()}
        if pos["produkt"] is None:
            pos["produkt"] = next((roh[a] for a in _PRODUKT_NOTNAGEL if roh.get(a) is not None), None)
        if pos["produkt"] is None:
            # Klartext statt "Field required" – damit kann auch ein kleines Modell den Aufruf selbst korrigieren.
            raise ValueError(
                f"Position ohne Produktnummer (erhaltene Felder: {', '.join(map(str, v)) or 'keine'}). "
                'Jede Position braucht "produkt" und "menge", z.B. {"produkt": "ZJCG920", "menge": 200}'
            )
        return {k: w for k, w in pos.items() if w is not None}


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


def _vorschau_id(payload: dict[str, Any]) -> str:
    """Fingerabdruck des Payloads: Angelegt wird nur genau das, was der Nutzer in der Vorschau gesehen hat."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
@_protokoll
async def auftrag_anlegen(
    kunde: Annotated[Nummer, Field(description="SAP-Kundennummer (SoldToParty)")],
    bestellnummer: Annotated[Nummer, Field(max_length=35, description="Bestellnummer des Kunden aus der Mail")],
    wunschtermin: Annotated[Text, Field(description="Liefertermin WÖRTLICH wie in der Mail, z.B. 'KW 44' oder '30.10.2026'. NICHT selbst umrechnen – das macht der Server")],
    positionen: Annotated[list[Position], BeforeValidator(_als_liste), Field(min_length=1, description="Bestellte Positionen")],
    vorschau_id: Annotated[
        Text | None,
        Field(description="Leer lassen = nur Vorschau. Zum Anlegen die vorschau_id aus der Vorschau angeben – NUR nach ausdrücklicher Zustimmung des Nutzers"),
    ] = None,
) -> dict[str, Any]:
    """Legt einen Kundenauftrag in SAP an – in zwei Schritten: 1) ohne vorschau_id aufrufen -> Vorschau, dem Nutzer zeigen. 2) Erst wenn der Nutzer ausdrücklich zustimmt, mit denselben Daten und der vorschau_id erneut aufrufen."""
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
            | ({"RequestedQuantitySAPUnit": p.einheit} if p.einheit else {})
            for p in positionen
        ],
    }
    vid = _vorschau_id(payload)

    if vorschau_id != vid:
        vorschau: dict[str, Any] = {
            "vorschau": payload,
            "vorschau_id": vid,
            "wunschtermin_erkannt": f"{wunschtermin!r} -> {liefertermin:%d.%m.%Y} (KW {liefertermin.isocalendar()[1]})",
            "org_daten_von": f"Auftrag {vorlage_nr}" if vorlage_nr else ".env-Standardwerte",
            "hinweis": "Noch NICHT angelegt. Vorschau dem Nutzer zeigen und um Bestätigung bitten. "
            "Nach Zustimmung auftrag_anlegen mit denselben Daten und dieser vorschau_id aufrufen.",
        }
        if vorschau_id:
            vorschau["achtung"] = "Die Daten weichen von der bestätigten Vorschau ab – bitte diese neue Vorschau bestätigen lassen."
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
    import atexit
    from pathlib import Path

    sys.stderr.reconfigure(encoding="utf-8")  # Windows: Umlaute in Client-Logs nicht zerschießen
    # Zusätzlich in server.log neben dieser Datei – stderr landet bei Odysseus & Co. oft im Nirgendwo.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [pid %(process)d] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(Path(__file__).with_name("server.log"), encoding="utf-8"),
        ],
        force=True,  # FastMCP richtet beim Import schon Logging ein – sonst wäre das hier wirkungslos
    )
    log.info("Start: %s %s | SAP_BASE_URL %s | SAP_PASSWORD %s", sys.executable, " ".join(sys.argv),
             "gesetzt" if os.getenv("SAP_BASE_URL") else "FEHLT", "gesetzt" if os.getenv("SAP_PASSWORD") else "FEHLT")
    atexit.register(lambda: log.info("Server-Prozess beendet"))
    sys.excepthook = lambda *exc: log.critical("Absturz", exc_info=exc)
    if "--http" in sys.argv[1:]:
        log.info("sap-order-mcp läuft (Streamable HTTP) auf http://%s:%s/mcp", mcp.settings.host, mcp.settings.port)
        mcp.run(transport="streamable-http")
    else:
        log.info("sap-order-mcp läuft (stdio)")
        mcp.run()
