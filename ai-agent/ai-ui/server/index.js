import { createApp } from "./app.js";
const port = Number(process.env.PROXY_PORT || 8787);
createApp({ backendBase: process.env.BACKEND_BASE_URL || "http://localhost:8080" })
  .listen(port, "0.0.0.0", () => console.log(`SRE AI Agent UI dev proxy listening on port ${port}`));
