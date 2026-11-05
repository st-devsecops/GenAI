import { createApp } from "./app.js";
import { fileURLToPath } from "node:url";
import path from "node:path";

const port = Number(process.env.PORT || 8787);
const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(here, "../dist");
const server = createApp({ dist }).listen(port, "0.0.0.0", () => console.log(`SRE AI UI listening on port ${port}`));
server.requestTimeout = 30_000;
server.headersTimeout = 15_000;
server.keepAliveTimeout = 5_000;

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    server.close((error) => {
      if (error) {
        console.error("UI server shutdown failed", { errorType: error.name });
        process.exitCode = 1;
      }
    });
  });
}
