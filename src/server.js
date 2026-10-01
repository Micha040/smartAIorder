// MCP-Server "Bestell-Mail -> Kundenauftrag".
// WICHTIG bei stdio: niemals console.log (stdout gehört dem MCP-Protokoll) – nur console.error.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { findOrdersByPO, getLastOrder, createOrder } from "./sap.js";

const server = new McpServer({ name: "sap-order-mcp", version: "0.1.0" });
const text = (obj) => ({ content: [{ type: "text", text: typeof obj === "string" ? obj : JSON.stringify(obj, null, 2) }] });

// "KW 44", "2026-W44" oder "2026-10-30" -> "YYYY-MM-DD" (Montag der Woche). Rechnen macht der Server, nicht das LLM.
function toDate(input) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(input)) return input;
  const m = String(input).match(/(?:(\d{4})-?W|KW\s*)(\d{1,2})/i);
  if (!m) throw new Error(`Datum nicht erkannt: ${input}`);
  const year = Number(m[1] || new Date().getFullYear());
  const week = Number(m[2]);
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const monday = new Date(jan4);
  monday.setUTCDate(jan4.getUTCDate() - ((jan4.getUTCDay() || 7) - 1) + (week - 1) * 7);
  return monday.toISOString().slice(0, 10);
}

server.registerTool(
  "letzten_auftrag_holen",
  {
    description: "Liefert den letzten Kundenauftrag eines Kunden mit Positionen. Nutzen bei 'wie letztes Mal' oder um Produktnummern zu finden.",
    inputSchema: { kunde: z.string().describe("SAP-Kundennummer (SoldToParty)") },
  },
  async ({ kunde }) => {
    const o = await getLastOrder(kunde);
    if (!o) return text(`Kein Auftrag für Kunde ${kunde} gefunden.`);
    return text({
      auftrag: o.SalesOrder,
      datum: o.CreationDate,
      bestellnummer: o.PurchaseOrderByCustomer,
      positionen: (o._Item || []).map((i) => ({ produkt: i.Product, text: i.SalesOrderItemText, menge: i.RequestedQuantity, einheit: i.RequestedQuantityUnit })),
    });
  }
);

server.registerTool(
  "dublette_pruefen",
  {
    description: "Prüft, ob es für Kunde + Kundenbestellnummer schon einen Auftrag gibt. IMMER vor dem Anlegen aufrufen.",
    inputSchema: { kunde: z.string(), bestellnummer: z.string().describe("Bestellnummer des Kunden aus der Mail") },
  },
  async ({ kunde, bestellnummer }) => {
    const hits = await findOrdersByPO(kunde, bestellnummer);
    return text(hits.length ? { dublette: true, vorhandene_auftraege: hits } : { dublette: false });
  }
);

server.registerTool(
  "auftrag_anlegen",
  {
    description:
      "Legt einen Kundenauftrag an. Erst mit bestaetigt=false aufrufen und die Vorschau dem Nutzer zeigen. Nur wenn der Nutzer ausdrücklich zustimmt, erneut mit bestaetigt=true aufrufen.",
    inputSchema: {
      kunde: z.string(),
      bestellnummer: z.string(),
      wunschtermin: z.string().describe("z.B. 'KW 44' oder '2026-10-30'"),
      positionen: z.array(z.object({ produkt: z.string(), menge: z.number().positive() })).min(1),
      bestaetigt: z.boolean().default(false),
    },
  },
  async ({ kunde, bestellnummer, wunschtermin, positionen, bestaetigt }) => {
    // Harte Regel im Server, unabhängig davon, was das LLM tut:
    const dup = await findOrdersByPO(kunde, bestellnummer);
    if (dup.length) return text({ abgelehnt: "Dublette – Auftrag existiert bereits", vorhandene_auftraege: dup });

    // Org-Daten (Auftragsart, VkOrg, Vertriebsweg, Sparte) vom letzten Auftrag des Kunden übernehmen,
    // spart das Raten der Pflichtfelder.
    const vorlage = await getLastOrder(kunde);
    if (!vorlage) return text("Kein Vorlage-Auftrag für diesen Kunden – Org-Daten bitte in .env/Code festlegen.");

    const payload = {
      SalesOrderType: vorlage.SalesOrderType,
      SalesOrganization: vorlage.SalesOrganization,
      DistributionChannel: vorlage.DistributionChannel,
      OrganizationDivision: vorlage.OrganizationDivision,
      SoldToParty: kunde,
      PurchaseOrderByCustomer: bestellnummer,
      RequestedDeliveryDate: toDate(wunschtermin),
      _Item: positionen.map((p) => ({ Product: p.produkt, RequestedQuantity: p.menge })),
    };

    if (!bestaetigt) return text({ vorschau: payload, hinweis: "Noch nicht angelegt. Nutzer um Bestätigung bitten." });

    const res = await createOrder(payload);
    return text({ angelegt: true, auftrag: res.SalesOrder, nettowert: res.TotalNetAmount, waehrung: res.TransactionCurrency });
  }
);

await server.connect(new StdioServerTransport());
console.error("sap-order-mcp läuft (stdio)");
