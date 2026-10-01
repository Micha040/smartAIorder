"""Dünne Schicht über die Sales Order A2X API (OData V4) auf XAN100."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

# .env neben dieser Datei laden – nicht aus dem CWD, denn MCP-Clients starten den Server oft woanders.
load_dotenv(Path(__file__).with_name(".env"))

ITEM_FIELDS = "SalesOrderItem,Product,SalesOrderItemText,RequestedQuantity,RequestedQuantityUnit"
PO_HIT_FIELDS = "SalesOrder,SoldToParty,PurchaseOrderByCustomer,CreationDate,TotalNetAmount,TransactionCurrency"


class SapError(RuntimeError):
    """Fehler aus SAP, lesbar formatiert – landet als Tool-Fehler beim LLM."""


def esc(value: str) -> str:
    """OData-String-Literal escapen (' -> '')."""
    return str(value).replace("'", "''")


def _qs(params: dict[str, str]) -> str:
    # Selbst kodieren: Leerzeichen als %20 (nicht '+'), Keys wie $filter bleiben unverändert.
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())


def _verify_setting() -> bool | str:
    """SAP_VERIFY_SSL: true (Standard), false, oder Pfad zu einem CA-Bundle (Firmen-Proxy)."""
    v = os.getenv("SAP_VERIFY_SSL", "true").strip()
    if v.lower() in ("false", "0", "no", "nein"):
        return False
    if v.lower() in ("", "true", "1", "yes", "ja"):
        return True
    return v


def _raise_for_status(res: httpx.Response, op: str) -> None:
    if res.is_success:
        return
    msg = res.text[:1500]
    try:
        err = res.json()["error"]
        main = err.get("message")
        if isinstance(main, dict):  # OData-V2-Stil, falls doch mal
            main = main.get("value")
        parts = [main] + [d.get("message") for d in err.get("details", [])]
        msg = " | ".join(p for p in parts if p)
    except Exception:
        pass
    if res.status_code == 401:
        msg += " (Benutzer/Passwort in .env prüfen)"
    raise SapError(f"SAP {op} HTTP {res.status_code}: {msg}")


class SapClient:
    def __init__(
        self,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,  # für Tests
    ) -> None:
        base_url = base_url or os.getenv("SAP_BASE_URL", "")
        user = user or os.getenv("SAP_USER", "")
        password = password or os.getenv("SAP_PASSWORD", "")
        if not (base_url and user and password):
            raise SapError("SAP_BASE_URL, SAP_USER oder SAP_PASSWORD fehlt – .env anlegen (Vorlage: .env.example).")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            auth=(user, password),
            headers={"Accept": "application/json"},
            timeout=30.0,
            verify=_verify_setting(),
            transport=transport,
        )
        self._csrf: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- HTTP-Grundlagen -------------------------------------------------

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = path + ("?" + _qs(params) if params else "")
        res = await self._http.get(url)
        _raise_for_status(res, "GET")
        return res.json()

    async def _fetch_csrf(self) -> str:
        # Schreibende Requests brauchen ein CSRF-Token. Die Session-Cookies dazu merkt sich der httpx-Client selbst.
        res = await self._http.get("", headers={"x-csrf-token": "fetch"})
        token = res.headers.get("x-csrf-token")
        if not token or token.lower() == "required":
            _raise_for_status(res, "CSRF-Fetch")
            raise SapError(f"Kein CSRF-Token erhalten (HTTP {res.status_code})")
        return token

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            if not self._csrf:
                self._csrf = await self._fetch_csrf()
            res = await self._http.post(path, json=body, headers={"x-csrf-token": self._csrf})
            # Token abgelaufen -> einmal neu holen und wiederholen
            if res.status_code == 403 and res.headers.get("x-csrf-token", "").lower() == "required" and attempt == 1:
                self._csrf = None
                continue
            break
        _raise_for_status(res, "POST")
        return res.json()

    # --- Fachliche Abfragen ----------------------------------------------

    async def find_orders_by_po(self, po: str, sold_to: str | None = None) -> list[dict[str, Any]]:
        """Aufträge mit dieser Kundenbestellnummer – optional nur für einen Kunden."""
        flt = f"PurchaseOrderByCustomer eq '{esc(po)}'"
        if sold_to:
            flt = f"SoldToParty eq '{esc(sold_to)}' and " + flt
        data = await self.get("SalesOrder", {"$filter": flt, "$select": PO_HIT_FIELDS, "$top": "20"})
        return data["value"]

    async def get_last_order(self, sold_to: str) -> dict[str, Any] | None:
        """Jüngster Auftrag des Kunden inkl. Positionen (alle Kopffelder, also auch Org-Daten)."""
        data = await self.get(
            "SalesOrder",
            {
                "$filter": f"SoldToParty eq '{esc(sold_to)}'",
                "$orderby": "CreationDate desc,SalesOrder desc",
                "$top": "1",
                "$expand": f"_Item($select={ITEM_FIELDS})",
            },
        )
        return data["value"][0] if data["value"] else None

    async def get_order(self, sales_order: str) -> dict[str, Any] | None:
        try:
            return await self.get(f"SalesOrder('{esc(sales_order)}')", {"$expand": f"_Item($select={ITEM_FIELDS})"})
        except SapError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    async def sample_order(self) -> dict[str, Any] | None:
        """Zum Erkunden: irgendein Auftrag inkl. Positionen (zeigt echte Feldwerte von XAN100)."""
        data = await self.get("SalesOrder", {"$top": "1", "$orderby": "CreationDate desc", "$expand": "_Item"})
        return data["value"][0] if data["value"] else None

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post("SalesOrder", payload)  # Deep Insert: Kopf + _Item in einem Request
