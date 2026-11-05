import { randomUUID } from "node:crypto";
import express from "express";
import path from "node:path";

const REQUEST_LIMIT = "16kb";
const UPSTREAM_TIMEOUT_MS = 120_000;
const READINESS_TIMEOUT_MS = 3_000;
const MAX_RESPONSE_BYTES = 1_000_000;

class ResponseTooLargeError extends Error {}

async function readLimitedBody(response) {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
    await response.body?.cancel();
    throw new ResponseTooLargeError();
  }
  if (!response.body) return "";

  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new ResponseTooLargeError();
    }
    chunks.push(value);
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

function resolveBackendBase(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("BACKEND_BASE_URL must be an http(s) URL without credentials, query, or fragment");
  }
  return url.toString().replace(/\/+$/, "");
}

function sameOriginRequest(req) {
  const origin = req.get("origin");
  if (!origin) return true;
  try {
    const parsed = new URL(origin);
    return ["http:", "https:"].includes(parsed.protocol) && parsed.host === req.get("host");
  } catch {
    return false;
  }
}

function securityHeaders(_req, res, next) {
  res.set({
    "Content-Security-Policy": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; img-src 'self' data:; font-src 'self' https://fonts.gstatic.com data:; style-src 'self' https://fonts.googleapis.com; script-src 'self'; connect-src 'self'; form-action 'self'",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY"
  });
  next();
}

export function createApp({
  backendBase = process.env.BACKEND_BASE_URL || "http://sre-ai-backend.sre-ai.svc.cluster.local:8080",
  apiKey = process.env.SRE_API_KEY,
  dist,
  fetchImpl = globalThis.fetch,
  logger = console
} = {}) {
  const backend = resolveBackendBase(backendBase);
  if (typeof fetchImpl !== "function") throw new Error("Node.js with built-in fetch support is required");

  const app = express();
  app.disable("x-powered-by");
  app.use(securityHeaders);
  app.use((req, res, next) => {
    req.requestId = randomUUID();
    res.setHeader("X-Request-ID", req.requestId);
    if (req.path.startsWith("/api/")) res.setHeader("Cache-Control", "no-store");
    next();
  });
  app.use("/api", express.json({ limit: REQUEST_LIMIT, strict: true, type: "application/json" }));

  app.get("/healthz", (_req, res) => res.json({ status: "ok" }));
  const readiness = async (_req, res) => {
    if (!apiKey) return res.status(503).json({ status: "not_ready" });
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), READINESS_TIMEOUT_MS);
    try {
      const upstream = await fetchImpl(`${backend}/readyz`, { signal: controller.signal, redirect: "manual" });
      if (!upstream.ok) return res.status(503).json({ status: "not_ready" });
      return res.json({ status: "ready" });
    } catch {
      return res.status(503).json({ status: "not_ready" });
    } finally {
      clearTimeout(timeout);
    }
  };
  app.get("/readyz", readiness);
  app.get("/api/readyz", readiness);

  app.post("/api/investigate", async (req, res) => {
    if (!sameOriginRequest(req)) return res.status(403).json({ error: "Cross-origin requests are not allowed." });
    if (!req.is("application/json")) return res.status(415).json({ error: "Content-Type must be application/json." });
    if (!apiKey) return res.status(503).json({ error: "Investigation service is not configured." });

    const { query, environment } = req.body ?? {};
    if (typeof query !== "string" || !query.trim() || query.length > 12_000) {
      return res.status(400).json({ error: "query must contain 1 to 12000 characters." });
    }
    if (typeof environment !== "string" || !environment.trim() || environment.length > 64) {
      return res.status(400).json({ error: "environment must contain 1 to 64 characters." });
    }
    const filters = {};
    for (const field of ["start_time", "end_time", "time_range", "timezone", "search_mode", "service"]) {
      const value = req.body[field];
      if (value === undefined) continue;
      if (typeof value !== "string" || !value.trim() || value.length > (field === "service" ? 128 : 64)) {
        return res.status(400).json({ error: `${field} must be a nonempty bounded string.` });
      }
      filters[field] = value.trim();
    }
    if (filters.search_mode && !["auto", "keyword", "semantic", "hybrid"].includes(filters.search_mode)) {
      return res.status(400).json({ error: "Invalid search_mode." });
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
    const abortWhenDisconnected = () => controller.abort();
    req.once("aborted", abortWhenDisconnected);
    res.once("close", () => {
      if (!res.writableEnded) controller.abort();
    });

    try {
      const upstream = await fetchImpl(`${backend}/api/v1/investigate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": apiKey,
          "X-Request-ID": req.requestId
        },
        body: JSON.stringify({ query: query.trim(), environment: environment.trim(), ...filters }),
        signal: controller.signal,
        redirect: "manual"
      });
      const body = await readLimitedBody(upstream);
      if (upstream.status === 422) {
        let payload;
        try { payload = JSON.parse(body); } catch { payload = null; }
        const rejectedAllEnvironments = environment.trim() === "all" && Array.isArray(payload?.detail)
          && payload.detail.some((item) => item?.type === "literal_error"
            && Array.isArray(item.loc) && item.loc[0] === "body" && item.loc[1] === "environment");
        if (rejectedAllEnvironments) {
          return res.status(422).json({
            ...payload,
            error: "The deployed backend does not support All environments yet. Deploy the updated backend image that accepts environment='all' for stg, prod and hub. Restarting the old pinned image will not update it. Until then, select STG, PROD or HUB explicitly; writing 'in hub' in the question does not change the Environment selection."
          });
        }
        const rejectedFields = Array.isArray(payload?.detail)
          ? [...new Set(payload.detail.filter((item) =>
            item?.type === "extra_forbidden" && Array.isArray(item.loc) && item.loc[0] === "body"
            && ["time_range", "timezone", "search_mode", "start_time", "end_time", "service"].includes(item.loc[1])
          ).map((item) => item.loc[1]))]
          : [];
        if (rejectedFields.length) {
          return res.status(422).json({
            ...payload,
            error: `The deployed backend does not accept these investigation fields: ${rejectedFields.join(", ")}. Deploy the updated backend image with time parsing and automatic query routing, then retry. The UI and backend versions must match.`
          });
        }
      }
      res.status(upstream.status);
      res.setHeader("Content-Type", upstream.headers.get("content-type") || "application/json");
      return res.send(body);
    } catch (error) {
      if (res.destroyed) return;
      if (error instanceof ResponseTooLargeError) {
        return res.status(502).json({ error: "Backend response exceeded the UI proxy limit." });
      }
      const timedOut = error instanceof Error && error.name === "AbortError" && !req.aborted;
      logger.error?.("Investigation proxy request failed", {
        requestId: req.requestId,
        errorType: error instanceof Error ? error.name : "UnknownError"
      });
      return res.status(timedOut ? 504 : 502).json({
        error: timedOut ? "The investigation timed out. Try a narrower query." : "The investigation service is unavailable.",
        request_id: req.requestId
      });
    } finally {
      clearTimeout(timeout);
      req.off("aborted", abortWhenDisconnected);
    }
  });

  app.use("/api", (_req, res) => res.status(404).json({ error: "API route not found." }));

  if (dist) {
    const staticRoot = path.resolve(dist);
    app.use(express.static(staticRoot, {
      index: false,
      fallthrough: true,
      maxAge: "1y",
      immutable: true,
      setHeaders(res, filePath) {
        if (path.basename(filePath) === "index.html") res.setHeader("Cache-Control", "no-cache");
      }
    }));
    app.get("*", (_req, res, next) => {
      res.setHeader("Cache-Control", "no-cache");
      res.sendFile(path.join(staticRoot, "index.html"), (error) => {
        if (error) next(error);
      });
    });
  }

  app.use((error, _req, res, _next) => {
    if (res.headersSent) return;
    if (error?.type === "entity.too.large") return res.status(413).json({ error: "Request body is too large." });
    if (error instanceof SyntaxError && "body" in error) return res.status(400).json({ error: "Request body must be valid JSON." });
    logger.error?.("UI request failed", { errorType: error instanceof Error ? error.name : "UnknownError" });
    return res.status(500).json({ error: "The UI server could not process the request." });
  });

  return app;
}
