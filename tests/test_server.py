"""Tool-Logik gegen ein simuliertes SAP (httpx.MockTransport) – läuft ohne Netz und ohne Zugangsdaten."""

import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest

import server
from sap_client import SapClient, SapError

VORLAGE = {
    "SalesOrder": "100",
    "SalesOrderType": "TA",
    "SalesOrganization": "1010",
    "DistributionChannel": "10",
    "OrganizationDivision": "00",
    "SoldToParty": "1000022",
    "PurchaseOrderByCustomer": "4700",
    "CreationDate": "2026-09-01",
    "_Item": [{"SalesOrderItem": "10", "Product": "ZJCG920", "RequestedQuantity": 200, "RequestedQuantitySAPUnit": "ST"}],
}


class FakeSap:
    def __init__(self, dubletten=None, csrf_abgelaufen=False):
        self.dubletten = dubletten or []
        self.csrf_abgelaufen = csrf_abgelaufen
        self.posts = []
        self.csrf_fetches = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = unquote(str(req.url))
        if req.method == "GET" and req.headers.get("x-csrf-token") == "fetch":
            self.csrf_fetches += 1
            return httpx.Response(200, headers={"x-csrf-token": f"tok{self.csrf_fetches}", "set-cookie": "sess=1; Path=/"})
        if req.method == "GET" and "PurchaseOrderByCustomer eq" in url:
            return httpx.Response(200, json={"value": self.dubletten})
        if req.method == "GET" and "SoldToParty eq '1000022'" in url:
            return httpx.Response(200, json={"value": [VORLAGE]})
        if req.method == "GET" and "SoldToParty eq" in url:
            return httpx.Response(200, json={"value": []})
        if req.method == "POST":
            if self.csrf_abgelaufen and req.headers["x-csrf-token"] == "tok1":
                return httpx.Response(403, headers={"x-csrf-token": "Required"})
            assert req.headers["cookie"] == "sess=1"
            body = json.loads(req.content)
            self.posts.append(body)
            return httpx.Response(201, json={**body, "SalesOrder": "4711", "TotalNetAmount": 1234.5, "TransactionCurrency": "EUR"})
        return httpx.Response(404, json={"error": {"code": "X", "message": f"unerwartet: {req.method} {url}"}})


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeSap(**kw)
        client = SapClient("https://sap.example/api/", "u", "p", transport=httpx.MockTransport(f))
        monkeypatch.setattr(server, "_sap", client)
        return f

    return install


def run(coro):
    return asyncio.run(coro)


POS = [server.Position(produkt="ZJCG920", menge=200)]


def test_ohne_passende_vorschau_id_wird_nichts_angelegt(fake):
    f = fake()
    r = run(server.auftrag_anlegen("1000022", "4711", "2099-01-15", POS, vorschau_id="true"))
    assert "vorschau" in r and "achtung" in r and f.posts == []
    # Menge nach der Vorschau geändert -> alte ID passt nicht mehr
    vid = run(server.auftrag_anlegen("1000022", "4711", "2099-01-15", POS))["vorschau_id"]
    r = run(server.auftrag_anlegen("1000022", "4711", "2099-01-15", [server.Position(produkt="ZJCG920", menge=999)], vorschau_id=vid))
    assert "vorschau" in r and f.posts == []


def test_vorschau_legt_nichts_an(fake):
    f = fake()
    r = run(server.auftrag_anlegen("1000022", "4711", "2099-01-15", POS))
    assert r["vorschau"]["SalesOrganization"] == "1010"
    assert r["vorschau"]["RequestedDeliveryDate"] == "2099-01-15"
    assert r["org_daten_von"] == "Auftrag 100"
    assert f.posts == []


def anlegen(*args):
    """Ablauf wie das LLM: Vorschau holen, dann mit vorschau_id bestätigen."""
    vid = run(server.auftrag_anlegen(*args))["vorschau_id"]
    return run(server.auftrag_anlegen(*args, vorschau_id=vid))


def test_anlegen_mit_bestaetigung(fake):
    f = fake()
    r = anlegen("1000022", "4711", "2099-01-15", POS)
    assert r == {"angelegt": True, "auftrag": "4711", "nettowert": 1234.5, "waehrung": "EUR"}
    assert f.posts[0]["_Item"] == [{"Product": "ZJCG920", "RequestedQuantity": 200.0}]
    assert f.posts[0]["SalesOrderType"] == "TA"


def test_dublette_blockiert_anlegen(fake):
    f = fake(dubletten=[{"SalesOrder": "999"}])
    r = run(server.auftrag_anlegen("1000022", "4711", "KW 44", POS, vorschau_id="egal"))
    assert "abgelehnt" in r
    assert f.posts == []


def test_csrf_token_wird_erneuert(fake):
    f = fake(csrf_abgelaufen=True)
    r = anlegen("1000022", "4711", "2099-01-15", POS)
    assert r["angelegt"] and f.csrf_fetches == 2


def test_neukunde_ohne_vorlage_und_ohne_defaults(fake, monkeypatch):
    fake()
    for k in ("SAP_DEFAULT_SALES_ORDER_TYPE", "SAP_DEFAULT_SALES_ORGANIZATION", "SAP_DEFAULT_DISTRIBUTION_CHANNEL", "SAP_DEFAULT_DIVISION"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SapError, match="Vorlage"):
        run(server.auftrag_anlegen("NEU", "4711", "2099-01-15", POS))


def test_neukunde_mit_defaults(fake, monkeypatch):
    fake()
    for k, v in {"SAP_DEFAULT_SALES_ORDER_TYPE": "OR", "SAP_DEFAULT_SALES_ORGANIZATION": "1010",
                 "SAP_DEFAULT_DISTRIBUTION_CHANNEL": "10", "SAP_DEFAULT_DIVISION": "00"}.items():
        monkeypatch.setenv(k, v)
    r = run(server.auftrag_anlegen("NEU", "4711", "2099-01-15", POS))
    assert r["org_daten_von"] == ".env-Standardwerte"


def test_dublette_ohne_kunde_sucht_global(fake):
    fake(dubletten=[{"SalesOrder": "999"}])
    assert run(server.dublette_pruefen("4711"))["dublette"] is True


def test_sap_fehlermeldung_lesbar(fake):
    fake()
    with pytest.raises(SapError, match="unerwartet"):
        run(server.sap().get("Gibtsnicht"))


def test_odata_escaping(fake):
    seen = []
    client = SapClient("https://sap.example/api/", "u", "p",
                       transport=httpx.MockTransport(lambda r: seen.append(str(r.url)) or httpx.Response(200, json={"value": []})))
    run(client.find_orders_by_po("O'Brien 1"))
    assert "%27O%27%27Brien%201%27" in seen[0] and "+" not in seen[0]


def test_tools_registriert():
    names = {t.name for t in run(server.mcp.list_tools())}
    assert names == {"letzten_auftrag_holen", "dublette_pruefen", "auftrag_anzeigen", "auftrag_anlegen"}


# --- Über den echten MCP-Aufrufpfad (Validierung wie bei einem lokalen LLM) ---


def call(name, args):
    """Tool so aufrufen, wie es der MCP-Client tut. Liefert (ist_fehler, text)."""
    from mcp.server.fastmcp.exceptions import ToolError

    try:
        res = run(server.mcp.call_tool(name, args))
    except ToolError as e:
        return True, str(e)
    content = res[0] if isinstance(res, tuple) else res
    return False, content[0].text


def test_llm_schickt_nummern_als_zahl(fake):
    fake()
    fehler, text = call("letzten_auftrag_holen", {"kunde": 1000022})
    assert not fehler and '"gefunden": true' in text


def test_llm_schlampige_positionen(fake):
    f = fake()
    fehler, text = call("auftrag_anlegen", {
        "kunde": 1000022, "bestellnummer": 4711, "wunschtermin": "KW 44",
        "positionen": '{"product": 4711, "quantity": "1,5"}',  # JSON-String, Einzelobjekt, englische Keys, Komma
    })
    assert not fehler, text
    assert '"RequestedQuantity": 1.5' in text and '"Product": "4711"' in text and f.posts == []


def test_llm_erfindet_feldnamen(fake):
    # Echter Aufruf von gemma4:e4b: Produktnummer steckt in "pos_nummer"
    fake()
    fehler, text = call("auftrag_anlegen", {
        "kunde": "1000022", "bestellnummer": "TEAMTAG-001", "wunschtermin": "KW 50",
        "positionen": [{"einheit": "ST", "menge": 200, "pos_nummer": "ZJCG920"}],
    })
    assert not fehler, text
    assert '"Product": "ZJCG920"' in text and "KW 50" in text


def test_position_ohne_produkt_klare_meldung(fake):
    fake()
    fehler, text = call("auftrag_anlegen", {
        "kunde": "1000022", "bestellnummer": "4711", "wunschtermin": "KW 50",
        "positionen": [{"menge": 200, "text": "Schrauben"}],
    })
    assert fehler and '"produkt"' in text and "menge, text" in text


def test_leere_bestellnummer_abgelehnt(fake):
    fake()
    fehler, text = call("dublette_pruefen", {"bestellnummer": "  "})
    assert fehler


def test_timeout_wird_lesbar(monkeypatch):
    def boom(req):
        raise httpx.ReadTimeout("", request=req)

    client = SapClient("https://sap.example/api/", "u", "p", transport=httpx.MockTransport(boom))
    with pytest.raises(SapError, match="Zeitüberschreitung"):
        run(client.find_orders_by_po("4711"))
    with pytest.raises(SapError, match="NICHT erneut anlegen"):
        client._csrf = "x"
        run(client.create_order({}))


def test_smoke_anlegen_wirklich(fake, capsys):
    import smoke

    f = fake()
    run(smoke.main(["anlegen", "1000022", "ZJCG920", "5", "--wirklich"]))
    out = capsys.readouterr().out
    assert "vorschau_id" in out and '"angelegt": true' in out
    assert len(f.posts) == 1
