// Dünne Schicht über die Sales Order A2X API (OData V4).
import "dotenv/config";

const BASE = (process.env.SAP_BASE_URL || "").replace(/\/?$/, "/");
const AUTH =
  "Basic " +
  Buffer.from(`${process.env.SAP_USER}:${process.env.SAP_PASSWORD}`).toString("base64");

const esc = (s) => String(s).replace(/'/g, "''"); // OData-String-Escaping
const qs = (params) =>
  Object.entries(params)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join("&");

async function sapGet(path) {
  const res = await fetch(BASE + path, {
    headers: { Authorization: AUTH, Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`SAP GET ${res.status}: ${(await res.text()).slice(0, 800)}`);
  return res.json();
}

// Schreibende Requests brauchen ein CSRF-Token + die Session-Cookies aus dem Fetch-Call.
async function fetchCsrf() {
  const res = await fetch(BASE, {
    headers: { Authorization: AUTH, Accept: "application/json", "x-csrf-token": "fetch" },
  });
  const token = res.headers.get("x-csrf-token");
  if (!token) throw new Error(`Kein CSRF-Token erhalten (HTTP ${res.status})`);
  const cookies = res.headers.getSetCookie().map((c) => c.split(";")[0]).join("; ");
  return { token, cookies };
}

async function sapPost(path, body) {
  const { token, cookies } = await fetchCsrf();
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: {
      Authorization: AUTH,
      Accept: "application/json",
      "Content-Type": "application/json",
      "x-csrf-token": token,
      Cookie: cookies,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`SAP POST ${res.status}: ${(await res.text()).slice(0, 1500)}`);
  return res.json();
}

export async function findOrdersByPO(soldTo, po) {
  const data = await sapGet(
    "SalesOrder?" +
      qs({
        $filter: `SoldToParty eq '${esc(soldTo)}' and PurchaseOrderByCustomer eq '${esc(po)}'`,
        $select: "SalesOrder,SoldToParty,PurchaseOrderByCustomer,CreationDate,TotalNetAmount,TransactionCurrency",
      })
  );
  return data.value;
}

export async function getLastOrder(soldTo) {
  const data = await sapGet(
    "SalesOrder?" +
      qs({
        $filter: `SoldToParty eq '${esc(soldTo)}'`,
        $orderby: "CreationDate desc",
        $top: "1",
        $expand: "_Item($select=SalesOrderItem,Product,SalesOrderItemText,RequestedQuantity,RequestedQuantityUnit)",
      })
  );
  return data.value[0] ?? null;
}

// Zum Erkunden: ein beliebiger Auftrag inkl. Positionen (zeigt echte Feldwerte von XAN100).
export async function sampleOrder() {
  const data = await sapGet("SalesOrder?" + qs({ $top: "1", $expand: "_Item" }));
  return data.value[0] ?? null;
}

export async function createOrder(payload) {
  return sapPost("SalesOrder", payload); // Deep Insert: Kopf + _Item in einem Request
}
