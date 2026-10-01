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
from datetime import date, datetime, timedelta
from pathlib import Path
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
        "Aus einer Bestell-Mail einen SAP-Kundenauftrag anlegen – in genau zwei Schritten: "
        "1) auftrag_vorschau aufrufen mit kunde, bestellnummer, wunschtermin (wörtlich aus der Mail, z.B. 'KW 50') "
        "und positionen. Bei 'wie letztes Mal' das Produkt weglassen und nur die Menge angeben. Der Server prüft "
        "Dubletten, ergänzt Produkt/Menge/Org-Daten vom letzten Auftrag und rechnet den Termin selbst um – "
        "dafür vorher KEINE anderen Tools aufrufen und nichts selbst umrechnen. "
        "2) Die 'zusammenfassung' dem Nutzer zeigen. Erst wenn er ausdrücklich zustimmt: auftrag_bestaetigen(vorschau_id). "
        "Beim Nutzer nur nachfragen, wenn Kundennummer oder Bestellnummer fehlen oder ein Tool ausdrücklich um eine Angabe bittet."
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
    """Zeigt den letzten Kundenauftrag eines Kunden mit Positionen. Für 'wie letztes Mal' NICHT nötig – das ergänzt auftrag_vorschau selbst."""
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
    """Prüft, ob es zu einer Kundenbestellnummer schon einen Auftrag gibt. Vor dem Anlegen NICHT nötig – das macht auftrag_vorschau selbst."""
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
    "einheit": ("einheit", "unit", "uom", "me", "mengeneinheit", "requestedquantitysapunit"),
}
# Nur wenn sonst kein Produkt erkennbar ist – so nennen kleine Modelle die Produktnummer auch gern (z.B. "pos_nummer").
_PRODUKT_NOTNAGEL = ("posnummer", "positionsnummer", "nummer", "nr", "id", "item")
# "Stück" in jeder Schreibweise nicht an SAP schicken: RequestedQuantitySAPUnit kennt weder ISO (PCE) noch "STK".
# Ohne Einheit nimmt SAP die Verkaufseinheit aus dem Produktstamm.
_STUECK = {"ST", "STK", "STCK", "STÜCK", "STUECK", "STUCK", "PC", "PCE", "PCS", "EA", "EACH", "PIECE", "PIECES"}


class Position(BaseModel):
    produkt: Text | None = Field(default=None, description="SAP-Produktnummer, z.B. 'ZJCG920'. Leer lassen = Produkt vom letzten Auftrag")
    menge: Annotated[float | None, BeforeValidator(_als_zahl)] = Field(
        default=None, gt=0, description="Bestellmenge. Leer lassen = Menge vom letzten Auftrag"
    )
    einheit: Text | None = Field(default=None, description="Nur bei Nicht-Stückware, z.B. 'KG'. Sonst weglassen")

    @model_validator(mode="before")
    @classmethod
    def _felder_zuordnen(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        roh = {re.sub(r"[\s_\-]", "", str(k)).lower(): w for k, w in v.items() if w not in (None, "")}
        pos = {feld: next((roh[a] for a in aliase if a in roh), None) for feld, aliase in _POS_ALIASE.items()}
        if pos["produkt"] is None:
            pos["produkt"] = next((roh[a] for a in _PRODUKT_NOTNAGEL if a in roh), None)
        if pos["produkt"] is None and pos["menge"] is None:
            # Klartext statt "Field required" – damit kann auch ein kleines Modell den Aufruf selbst korrigieren.
            raise ValueError(
                f"Position ohne Produkt und Menge (erhaltene Felder: {', '.join(map(str, v)) or 'keine'}). "
                'Beispiel: {"produkt": "ZJCG920", "menge": 200}'
            )
        return {k: w for k, w in pos.items() if w is not None}


_ORG_FELDER = ("SalesOrderType", "SalesOrganization", "DistributionChannel", "OrganizationDivision")


def _org_daten(kunde: str, vorlage: dict[str, Any] | None) -> tuple[dict[str, str], str]:
    """Auftragsart, VkOrg, Vertriebsweg, Sparte: vom letzten Auftrag des Kunden, sonst aus .env. Liefert (Werte, Quelle)."""
    if vorlage and all(vorlage.get(k) for k in _ORG_FELDER):
        return {k: vorlage[k] for k in _ORG_FELDER}, f"Auftrag {vorlage.get('SalesOrder')}"
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
    return env, ".env-Standardwerte"  # type: ignore[return-value]


def _positionen_aufloesen(positionen: list[Position] | None, vorlage: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """'Wie letztes Mal' im Server auflösen: fehlende Positionen, Produkte oder Mengen kommen vom letzten Auftrag."""
    alt = (vorlage or {}).get("_Item") or []
    nr = (vorlage or {}).get("SalesOrder")
    produkte = list(dict.fromkeys(i["Product"] for i in alt if i.get("Product")))
    hinweise: list[str] = []

    if not positionen:
        if not alt:
            raise ValueError("Keine Positionen angegeben und der Kunde hat keinen früheren Auftrag – bitte Produkt und Menge angeben.")
        positionen = [
            Position(produkt=i.get("Product"), menge=i.get("RequestedQuantity"), einheit=i.get("RequestedQuantitySAPUnit"))
            for i in alt
        ]
        hinweise.append(f"Positionen wie im letzten Auftrag {nr} übernommen.")

    items = []
    for n, p in enumerate(positionen, 1):
        produkt, menge = p.produkt, p.menge
        if not produkt:
            if len(produkte) != 1:
                grund = f"der letzte Auftrag {nr} hat mehrere: {', '.join(produkte)}" if produkte else "es gibt keinen früheren Auftrag"
                raise ValueError(f"Position {n}: Produkt fehlt und {grund}. Bitte 'produkt' angeben.")
            produkt = produkte[0]
            hinweise.append(f"Position {n}: Produkt {produkt} vom letzten Auftrag {nr} übernommen.")
        if menge is None:
            frueher = next((i for i in alt if i.get("Product") == produkt), None)
            if not frueher:
                raise ValueError(f"Position {n}: Menge für {produkt} fehlt. Bitte 'menge' angeben.")
            menge = float(frueher["RequestedQuantity"])
            hinweise.append(f"Position {n}: Menge {menge:g} vom letzten Auftrag {nr} übernommen.")
        item: dict[str, Any] = {"Product": produkt, "RequestedQuantity": menge}
        einheit = (p.einheit or "").upper()
        if einheit and einheit not in _STUECK:
            item["RequestedQuantitySAPUnit"] = einheit
        items.append(item)
    return items, hinweise


def _vorschau_id(payload: dict[str, Any]) -> str:
    """Fingerabdruck des Payloads: Angelegt wird nur genau das, was der Nutzer in der Vorschau gesehen hat."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]


# Vorschauen liegen auf Platte, nicht im Speicher: manche Clients starten den stdio-Server pro Anfrage neu.
VORSCHAU_DATEI = Path(__file__).with_name("vorschauen.json")
_VORSCHAU_GUELTIG = timedelta(hours=24)


def _vorschauen() -> dict[str, Any]:
    try:
        alle = json.loads(VORSCHAU_DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    grenze = (datetime.now() - _VORSCHAU_GUELTIG).isoformat(timespec="seconds")
    return {k: v for k, v in alle.items() if v.get("zeit", "") > grenze}


def _vorschauen_speichern(alle: dict[str, Any]) -> None:
    VORSCHAU_DATEI.write_text(json.dumps(alle, ensure_ascii=False, indent=1), encoding="utf-8")


def _zusammenfassung(payload: dict[str, Any], termin: str, org_quelle: str, vorlage: dict[str, Any] | None) -> str:
    """Fertiger Text für den Nutzer – kleine Modelle geben ihn einfach weiter, statt selbst zu formulieren."""
    alt = {i.get("Product"): i for i in (vorlage or {}).get("_Item") or []}
    zeilen = [
        f"Kunde: {payload['SoldToParty']}",
        f"Bestellnummer: {payload['PurchaseOrderByCustomer']}",
        f"Liefertermin: {termin}",
        "Positionen:",
    ]
    for n, it in enumerate(payload["_Item"], 1):
        a = alt.get(it["Product"], {})
        einheit = it.get("RequestedQuantitySAPUnit") or a.get("RequestedQuantitySAPUnit") or "Stück"
        text = f" ({a['SalesOrderItemText']})" if a.get("SalesOrderItemText") else ""
        zeilen.append(f"  {n}. {it['RequestedQuantity']:g} {einheit} {it['Product']}{text}")
    zeilen.append("Org-Daten: " + " / ".join(payload[k] for k in _ORG_FELDER) + f" (von {org_quelle})")
    return "\n".join(zeilen)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_protokoll
async def auftrag_vorschau(
    kunde: Annotated[Nummer, Field(description="SAP-Kundennummer (SoldToParty)")],
    bestellnummer: Annotated[Nummer, Field(max_length=35, description="Bestellnummer des Kunden aus der Mail")],
    wunschtermin: Annotated[
        Text | None,
        Field(description="Liefertermin WÖRTLICH aus der Mail, z.B. 'KW 50' oder '30.10.2026'. NICHT umrechnen. Steht keiner in der Mail: weglassen"),
    ] = None,
    positionen: Annotated[
        list[Position] | None,
        BeforeValidator(_als_liste),
        Field(description="z.B. [{\"produkt\": \"ZJCG920\", \"menge\": 200}]. Bei 'wie letztes Mal' Produkt weglassen "
                          "(nur Menge) oder ganz weglassen – der Server ergänzt es vom letzten Auftrag"),
    ] = None,
) -> dict[str, Any]:
    """SCHRITT 1 von 2: Bereitet einen Kundenauftrag vor und liefert eine Zusammenfassung. Legt NICHTS an.
    Prüft Dubletten selbst, ergänzt 'wie letztes Mal' und Org-Daten vom letzten Auftrag und rechnet den Termin um.
    Pflicht sind nur kunde und bestellnummer."""
    dup = await sap().find_orders_by_po(bestellnummer, kunde)
    if dup:
        return {"abgelehnt": "Dublette – Auftrag mit dieser Bestellnummer existiert bereits", "vorhandene_auftraege": dup}

    liefertermin = wunschtermin_zu_datum(wunschtermin) if wunschtermin else None
    vorlage = await sap().get_last_order(kunde)
    org, org_quelle = _org_daten(kunde, vorlage)
    items, hinweise = _positionen_aufloesen(positionen, vorlage)

    payload: dict[str, Any] = {**org, "SoldToParty": kunde, "PurchaseOrderByCustomer": bestellnummer, "_Item": items}
    if liefertermin:
        payload["RequestedDeliveryDate"] = liefertermin.isoformat()
        termin = f"{liefertermin:%d.%m.%Y} (KW {liefertermin.isocalendar()[1]}, aus '{wunschtermin}')"
        if liefertermin < date.today():
            hinweise.append("ACHTUNG: Der Wunschtermin liegt in der Vergangenheit.")
    else:
        termin = "keiner angegeben – SAP setzt den Standardtermin"

    vid = _vorschau_id(payload)
    alle = _vorschauen()
    alle[vid] = {"zeit": datetime.now().isoformat(timespec="seconds"), "payload": payload}
    _vorschauen_speichern(alle)
    log.info("Vorschau %s: %s", vid, json.dumps(payload, ensure_ascii=False))

    res: dict[str, Any] = {"zusammenfassung": _zusammenfassung(payload, termin, org_quelle, vorlage)}
    if hinweise:
        res["hinweise"] = hinweise
    res["vorschau_id"] = vid
    res["naechster_schritt"] = (
        "Noch NICHT angelegt. Zeige dem Nutzer die Zusammenfassung und frage, ob der Auftrag angelegt werden soll. "
        f'Erst bei Zustimmung: auftrag_bestaetigen(vorschau_id="{vid}").'
    )
    return res


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
@_protokoll
async def auftrag_bestaetigen(
    vorschau_id: Annotated[Nummer, Field(description="vorschau_id aus auftrag_vorschau")],
) -> dict[str, Any]:
    """SCHRITT 2 von 2: Legt den Auftrag aus der Vorschau in SAP an. NUR aufrufen, wenn der Nutzer ausdrücklich zugestimmt hat."""
    vid = vorschau_id.strip("'\" ").lower()
    alle = _vorschauen()
    if vid not in alle:
        raise ValueError(f"Vorschau {vorschau_id!r} unbekannt oder abgelaufen – bitte auftrag_vorschau erneut aufrufen.")
    payload = alle[vid]["payload"]

    # Harte Regel im Server: auch zwischen Vorschau und Bestätigung kann jemand anderes angelegt haben.
    dup = await sap().find_orders_by_po(payload["PurchaseOrderByCustomer"], payload["SoldToParty"])
    if dup:
        _vorschauen_speichern({k: v for k, v in _vorschauen().items() if k != vid})
        return {"abgelehnt": "Dublette – Auftrag mit dieser Bestellnummer existiert bereits", "vorhandene_auftraege": dup}

    res = await sap().create_order(payload)
    _vorschauen_speichern({k: v for k, v in _vorschauen().items() if k != vid})
    log.info("Auftrag %s angelegt (Kunde %s, Bestellnr. %s)", res.get("SalesOrder"), payload["SoldToParty"], payload["PurchaseOrderByCustomer"])
    return {
        "angelegt": True,
        "auftrag": res.get("SalesOrder"),
        "nettowert": res.get("TotalNetAmount"),
        "waehrung": res.get("TransactionCurrency"),
    }


if __name__ == "__main__":
    import atexit

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
