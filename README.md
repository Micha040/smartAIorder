# sap-order-mcp

Bestell-Mail rein, Kundenauftrag in XAN100 raus – mit Dublettencheck und Bestätigungsschritt.

```
Kundenmail ─▶ LLM extrahiert Daten ─▶ dublette_pruefen ─▶ auftrag_anlegen (Vorschau) ─▶ Nutzer sagt "ja" ─▶ auftrag_anlegen (bestaetigt) ─▶ SAP
```

## Setup (Windows, PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # Passwort des Kommunikationsbenutzers eintragen (aus dem sicheren Kanal, nie committen)
python smoke.py               # zeigt einen echten Auftrag aus XAN100 -> Verbindung steht
```

## Früh testen – vor allem das Anlegen

```powershell
python smoke.py kunde 1000022                  # letzter Auftrag eines Kunden
python smoke.py dublette 4711                   # Dublettensuche über alle Kunden
python smoke.py anlegen 1000022 ZJCG920 5         # Vorschau des Payloads (legt nichts an)
python smoke.py anlegen 1000022 ZJCG920 5 --wirklich   # legt wirklich einen Testauftrag an (Bestellnr. SMOKE-...)
pytest                                          # Unit-Tests gegen simuliertes SAP, ohne Netz
```

Läuft `anlegen --wirklich` durch, sind Pflichtfelder, Berechtigungen und CSRF geklärt.

## Tools ohne LLM testen (MCP Inspector)

```powershell
npx @modelcontextprotocol/inspector .venv\Scripts\python.exe server.py
```

## Im KI-Interface einbinden

**stdio** (Client startet den Server selbst) – immer absolute Pfade verwenden:

```json
{
  "mcpServers": {
    "sap-order": {
      "command": "C:\\DEV\\Teamtag\\smartAIorder\\.venv\\Scripts\\python.exe",
      "args": ["C:\\DEV\\Teamtag\\smartAIorder\\server.py"]
    }
  }
}
```

**HTTP** (Transport „Streamable HTTP“, z. B. Odysseus oder Open WebUI ≥ 0.6.31 als Admin unter Admin-Einstellungen → Integrationen):

```powershell
python server.py --http       # http://127.0.0.1:8000/mcp – nur wenn der Client direkt unter Windows läuft
```

Läuft der Client in **Docker, WSL oder auf einem anderen Rechner**, ist `127.0.0.1` dort nicht dieser PC.
Dann den Server für das Netzwerk öffnen:

```powershell
$env:MCP_HOST="0.0.0.0"; python server.py --http   # Windows-Firewall-Abfrage erlauben
```

| Client läuft in | URL |
|---|---|
| Docker | `http://host.docker.internal:8000/mcp` |
| WSL / anderem Rechner | `http://<IP dieses PCs>:8000/mcp` (IP aus `ipconfig`) |

Ob der Client ankommt, zeigt das Server-Log: Dort muss `POST /mcp ... 200` erscheinen.
Achtung: Mit `0.0.0.0` ist der Server ohne Passwort im Netz erreichbar – nach der Demo beenden.

## Tools

| Tool | Zweck |
|---|---|
| `letzten_auftrag_holen(kunde)` | "wie letztes Mal" – Positionen/Produktnummern des letzten Auftrags |
| `dublette_pruefen(bestellnummer, kunde?)` | Gibt es die Kundenbestellnummer schon? Ohne Kunde: Suche über alle Kunden |
| `auftrag_anlegen(kunde, bestellnummer, wunschtermin, positionen, bestaetigt)` | `bestaetigt=false` → Vorschau, `true` → Anlage. Prüft Dubletten selbst noch einmal hart |
| `auftrag_anzeigen(auftrag)` | Angelegten Auftrag kontrollieren (gut für die Demo) |

Designentscheidungen:
- **Org-Daten** (Auftragsart, Verkaufsorganisation, Vertriebsweg, Sparte) kommen vom letzten Auftrag des Kunden, für Neukunden aus `SAP_DEFAULT_*` in `.env`. Das LLM muss sie nicht raten.
- **Wunschtermin** rechnet der Server um (`KW 44`, `44. KW`, `30.10.2026`, `30.10.`, `2026-10-30`), nicht das LLM. KW → Montag der Woche.
- **Dublettenschutz** steckt im Server, nicht nur im Prompt: `auftrag_anlegen` verweigert bei vorhandener Bestellnummer.

## Dateien

| Datei | Inhalt |
|---|---|
| `server.py` | MCP-Server und Tool-Definitionen |
| `sap_client.py` | HTTP-Schicht zur Sales Order API (OData V4, CSRF, Fehlertexte) |
| `datum.py` | Wunschtermin → Datum |
| `smoke.py` | Verbindungs- und Funktionstest ohne LLM |
| `tests/` | pytest mit simuliertem SAP |

> Bei stdio niemals `print()` im Server – stdout gehört dem MCP-Protokoll. Logging geht auf stderr.
