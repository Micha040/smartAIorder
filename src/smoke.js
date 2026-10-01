// Verbindungstest ohne MCP/LLM:  npm run smoke
import { sampleOrder } from "./sap.js";
const o = await sampleOrder();
console.log(JSON.stringify(o, null, 2));
