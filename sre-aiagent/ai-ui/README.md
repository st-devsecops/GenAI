# SRE AI Agent UI (React + TypeScript)

A local-first React dashboard and chat UI for the existing SRE AI Agent backend.

## Architecture

- React + TypeScript + Vite UI: `http://localhost:5173`
- Small Express proxy: `http://localhost:8787`
- Proxy forwards `POST /api/investigate` to `${BACKEND_BASE_URL}/api/v1/investigate`
- The API key is held by the proxy server and sent as `X-API-Key`; it is not placed in browser code or bundled into Vite.

The request sent to your backend is:

```json
{
  "query": "Show the latest 10 logs from logstash-aitools-*.",
   "environment": "stg",
   "timezone": "Asia/Kolkata",
   "time_range": "yesterday"
}
```

The composer Environment selector defaults to All environments (`stg`, `prod`,
`hub`), with Staging, Production, and Hub available individually. The selected
value is sent separately from the query and applies to both Prometheus metrics
and Elasticsearch logs. Every explicit selection, including All, takes precedence
over environment words in the question. Deploy the updated UI, backend, and
Elasticsearch MCP together to enforce this scope across all log tools.

Metric searches display native line graphs from the backend's `visualizations`
field. CPU rate queries use cores and standard working-set memory uses MiB;
other expressions keep raw query units rather than guessing a conversion.
Graphs preserve environment labels, show UTC timestamps and sample tooltips,
and mark compacted responses as sampled. A per-node table lists every returned
environment/namespace/pod with latest, average, minimum, peak, and sample count
for CPU and memory; missing resources are shown explicitly. It is built from all
returned series, independently of the chart's series limit. They do not create
Grafana dashboards or require a Grafana iframe. Rebuild the active backend and
UI together; responses without chart data continue to show their text findings.

The composer supports time from the question, today/yesterday, preset lookbacks
(15m, 1h, 6h, 12h, 24h), arbitrary last X minutes/hours/days, and custom
start/end times. Search mode can be `Auto`, `Keyword`, `Semantic`, or `Hybrid`;
`Auto` leaves routing to the backend. Calendar presets use the browser's
canonical IANA timezone, with common aliases normalized (for example
`Asia/Calcutta` to `Asia/Kolkata`). Relative lookbacks do not send a timezone,
so a local timezone alias cannot break a `last 5 days` request. Custom local
times are converted to UTC before submission. The proxy preserves these filters and optional service
scope while keeping credentials server-side. The backend and Elasticsearch MCP
allow up to 720 hours (30 days) for logs, hybrid, and metric requests. The “Last
30 days” preset and custom day durations use a fixed rolling 30-day window, not
a calendar month. Keep `MAX_TIME_RANGE_HOURS` aligned on the backend and ES MCP.

## Requirements

- Node.js 20 or newer
- npm
- Your backend reachable from the computer running this UI

## Run locally

1. Extract this project and enter its directory:

   ```bash
   cd sre-ai-agent-ui
   npm install
   ```

2. Create your local environment file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and set the values:

   ```dotenv
   BACKEND_BASE_URL=http://localhost:8080
   SRE_API_KEY=your-real-api-key
   PROXY_PORT=8787
   ```

   `BACKEND_BASE_URL` is the backend root only. Do not append `/api/v1/investigate`.
   If your backend runs in Kubernetes, `localhost:8080` works only if you have port-forwarded it (see below).

4. Start the proxy and UI:

   ```bash
   npm run dev
   ```

5. Open <http://localhost:5173>.

The UI sends requests to the local Vite proxy path `/api/investigate`. The Express proxy adds the secret header and forwards the request to the backend.

## If the backend is running in Kubernetes

From a terminal where the correct cluster context is selected, port-forward the backend service or deployment. Example (replace the name/port with your actual backend resource):

```bash
kubectl get svc -n sre-ai
kubectl get deploy -n sre-ai
kubectl port-forward -n sre-ai svc/<BACKEND_SERVICE_NAME> 8080:8080
```

If your backend listens on a different port, adjust the right-hand port and `BACKEND_BASE_URL`. Keep the port-forward running while using the UI.

If the backend is not exposed as a Kubernetes Service, port-forward the actual deployment:

```bash
kubectl port-forward -n sre-ai deployment/<BACKEND_DEPLOYMENT_NAME> 8080:8080
```

Confirm the backend path and port are correct. You can test the API directly with curl (avoid putting real API keys in shell history when possible):

```bash
curl -i -X POST http://localhost:8080/api/v1/investigate \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_API_KEY' \
  -d '{"query":"Show the latest 10 logs from logstash-aitools-*.","environment":"stg"}'
```

## Response support

The UI displays these backend fields when present:

- `summary`
- `findings`
- `root_cause`
- `confidence`
- `recommendations`
- `alternative_causes`
- `evidence_count`
- `investigation_errors`
- `time_range`
- `data_coverage` per source, including returned counts and observed timestamps

Coverage belongs to the saved response, not the currently selected filters.
Static Qdrant knowledge is labeled separately from time-bounded metrics/logs.

It also offers a raw JSON view, useful if the backend returns additional fields. Non-2xx responses and proxy/network errors are displayed in the chat.

## Production/security notes

- Do not put `X-API-Key` in React code or `VITE_*` variables. Anything exposed to the browser can be inspected.
- The Express server is the backend-for-frontend proxy in both local and production builds. It keeps the backend API key server-side, limits request bodies, checks browser origins, applies security headers, and does not expose upstream network errors.
- The proxy uses one shared backend API key. **External access must be behind your organization's authenticated ingress/SSO and TLS**; never expose the ClusterIP through an unauthenticated public route. Apply user authorization and rate limits at that ingress.
- Kubernetes readiness checks the backend `/readyz` endpoint. Liveness only checks the UI process.
- The proxy uses a 120-second upstream timeout. Kubernetes termination grace allows an in-flight investigation to finish during a rollout.
- The app currently stores conversation history only in browser memory; refreshing the page clears it.
- The Google Fonts import is optional. Remove the `@import` at the top of `src/styles.css` if the UI must be fully offline.

## Build check

```bash
npm ci
npm test
npm run build
```

## Troubleshooting

### Input should be 'stg', 'prod' or 'hub'

If the UI sends `environment: "all"` and receives this HTTP 422 error, it is
calling an older backend. The current backend accepts `all`, `stg`, `prod`,
and `hub`, with `all` as the default. Changing the UI alone does not update
the backend's request schema. Selecting HUB explicitly is a temporary workaround
for a hub-only request. Explicitly selecting All keeps all three environments,
even when the question mentions one environment. Staging, Production, and Hub
selections likewise remain authoritative. API callers that omit `environment`
can still infer a scope from an explicit phrase such as "in hub env";
environment-like tokens inside workload names or hostnames do not narrow it.
Do not silently replace `all` with one environment or drop the environment filter.

Rebuild from the active `ai-backend` directory, not a backup directory. From
the repository root, build and publish a new image and inspect its pushed digest:

```bash
BACKEND_IMAGE="xxxx/tfs-docker/sre-ai-agent:all-env-$(date -u +%Y%m%d%H%M%S)"
docker build -t "$BACKEND_IMAGE" ./ai-backend
docker push "$BACKEND_IMAGE"
docker image inspect "$BACKEND_IMAGE" --format '{{index .RepoDigests 0}}'
```

Set the backend container's `image` in `ai-backend/k8s/deployment.yaml` to
the returned `repository@sha256:...` reference. Then deploy and wait for every
backend replica to be replaced:

```bash
kubectl -n sre-ai apply -f ai-backend/k8s/deployment.yaml
kubectl -n sre-ai rollout status deployment/sre-ai-agent
kubectl -n sre-ai get deployment sre-ai-agent \
   -o 'custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image'
```

Ensure the UI's `BACKEND_BASE_URL` targets this backend's service. Retrying
after a completed rollout should accept All environments. Restarting pods with
the old pinned digest still runs the old validation schema. Rebuild and deploy
the UI with a new image version as well to receive the improved error message.

### Extra inputs are not permitted

If an investigation returns HTTP 422 with `extra_forbidden` for `timezone`,
`time_range`, or `search_mode`, the UI is likely calling an older backend schema.
Inspect the validation response's `detail[].loc` for the rejected field. The
current backend accepts these fields and supports five-day metric-only requests;
do not remove filters or disable strict validation to hide a version mismatch.
The proxy preserves the original 422 details and adds a deployment hint.

Rebuild and publish the updated backend and UI using new immutable image
versions. Update the backend image digest in `ai-backend/k8s/deployment.yaml`
and the UI image version in `ai-ui/k8s/deployment.yaml`, then roll out both
deployments. Restarting a deployment without changing its pinned digest runs
the same old backend. Do not reuse the UI tag with `imagePullPolicy: IfNotPresent`.
Confirm `BACKEND_BASE_URL` points to the service for the updated backend.

Inspect deployed image references without printing credentials:

```bash
kubectl -n sre-ai get deployment sre-ai-agent sre-ai-ui \
   -o 'custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image'
kubectl -n sre-ai rollout status deployment/sre-ai-agent
kubectl -n sre-ai rollout status deployment/sre-ai-ui
```

- **Proxy is not ready**: confirm `.env` exists in the project root, the backend `/readyz` responds successfully, and restart `npm run dev`.
- **Could not reach backend**: check `BACKEND_BASE_URL`, `kubectl port-forward`, service port, backend logs, and network access.
- **401/403 from backend**: check the API key and backend auth configuration.
- **404**: confirm the backend route is exactly `/api/v1/investigate`.
- **Request times out**: the proxy timeout is 120 seconds; update `UPSTREAM_TIMEOUT_MS` and the Kubernetes termination grace together if investigations normally take longer.
- **Investigation errors inside a 200 response**: these are backend tool errors, shown in the results panel; check backend logs and MCP connectivity.

## Updated query builder
The time selector includes common 3-, 5-, and 7-day ranges in addition to minutes/hours, today, yesterday, custom ranges, and an Auto/Keyword/Semantic/Hybrid search selector. Install dependencies from the lockfile with `npm ci` before building.
