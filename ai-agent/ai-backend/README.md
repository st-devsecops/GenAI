# SRE AI Agent MCP integration

The FastAPI backend connects to Prometheus MCP and the custom Elasticsearch MCP
at startup. The investigation graph collects metrics and logs through their
read-only tools and sends evidence to the remote LLM analyst.

## API-only image

The default image contains FastAPI, the RCA agent, MCP and LLM clients,
Pydantic, and security/logging dependencies. It contains no model weights,
CUDA packages, databases, RAG documents, training datasets, Jupyter, or build
toolchain. Qdrant, Elasticsearch, Grafana and the LLM remain external services.

`RAG_ENABLED=false` is the default. Investigations still use live MCP evidence,
but do not retrieve runbooks or previous RCA documents. Qdrant credentials are
not required in this mode. When enabled, RAG uses a remote embedding API and
Qdrant REST through httpx; no local model runtime or Qdrant SDK is needed.

## Production safeguards

The backend now includes fail-closed API-key authentication, per-client rate
limits, request-body and investigation-concurrency limits, protected Prometheus
metrics, and content-free request audit logs. All authorized keys grant the same
configured read-only operator scope; this is not per-service or tenant RBAC.

An MCP supervisor owns transport sessions, cleans up partial startup, pings both
servers, and reconnects with capped exponential backoff. Failed calls are not
automatically replayed. Readiness performs live MCP pings, while liveness remains
independent of upstream outages. RAG retrieval uses asynchronous HTTP with a
request deadline and propagates cancellation.

MCP results, planner context, aggregate evidence and LLM output have configurable
size budgets. Known configured credentials and bearer-token strings are redacted
before model context is built. This is not comprehensive PII/DLP filtering.
The analyst returns a low-confidence, undetermined RCA when live evidence is
missing, structured analysis fails, or findings cite missing/unknown references.
Valid references improve traceability but do not prove an LLM's causal claims.

Production approval still requires these environment-specific gates:

- Inject real credentials through a secret manager, replace image/LLM placeholders,
  configure the datasource and allowed hosts, and approve the LLM egress rules.
- Terminate TLS at the trusted gateway or service mesh. Add organization-specific
  identity, authorization and global rate limiting there. In-process limits are
  per pod/process and reset on restart; two replicas double aggregate capacity.
- Restrict or authenticate the custom ES MCP and use least-privilege database and
  Grafana credentials. Enforce upstream Prometheus query cost and lookback limits.
- Approve which logs/runbooks may reach the LLM and configure any required DLP.
- Run staging smoke, concurrent-load and outage/recovery tests against the actual
  MCPs, Qdrant and model, and evaluate RCA accuracy and prompt-injection resistance.
- Build, scan and sign the full runtime image, review dependency advisories, and
  configure alerts for error rates, latency, saturation and unavailable MCPs.

The repository CI runs Python 3.12 unit/integration tests, compilation and Docker
build checks. These checks do not certify the live deployment or image security.

## API access

Set `API_AUTH_KEYS` in the `sre-ai-agent-secrets` Secret as a JSON object mapping
client names to unique random keys of at least 32 characters. Store actual values
only in your secret manager, not this repository. Clients send `X-API-Key`.
Rotate keys by updating the Secret and rolling the deployment. API startup rejects
missing/short keys, an invalid Prometheus MCP endpoint (or an unset datasource for
the legacy Grafana provider), and the default LLM placeholders.

`/api/v1/investigate`, `/mcp/tools` and `/metrics` require authentication.
`/healthz` and `/readyz` remain unauthenticated for Kubernetes probes and return
no credentials. Interactive API documentation is disabled. All responses include
a generated `X-Request-ID`; audit logs record only route, status, ID and duration.

The ConfigMap exposes `API_REQUESTS_PER_MINUTE` (30),
`MAX_CONCURRENT_INVESTIGATIONS` (2), `MAX_REQUEST_BYTES` (16384),
`MAX_CONTEXT_CHARS` (32000), `MAX_TOOL_RESULT_CHARS` (12000),
`LLM_MAX_OUTPUT_TOKENS` (2000), RAG timeouts and MCP heartbeat/reconnect settings.
Use a separate monitoring key for `/metrics`, since scrapes share per-key limits.

## Adaptive investigation

### Time-aware retrieval

`POST /api/v1/investigate` accepts these optional filters:

```json
{
  "query": "Investigate checkout errors yesterday",
  "environment": "prod",
  "service": "checkout",
  "search_mode": "hybrid",
  "timezone": "Asia/Kolkata",
  "time_range": "yesterday"
}
```

- `time_range`: `today`, `yesterday`, `last 6 hours`, `last 2 days`, or
  `custom` with timezone-aware ISO `start_time` and `end_time`. Explicit
  timestamps take precedence. Custom requires both endpoints. Without an
  explicit filter, the parser extracts time from the question, falling back
  to the previous hour. Calendar days use the IANA `timezone` (default `UTC`);
  rolling hours/days are elapsed durations, including across DST changes.
- `search_mode`: `keyword` queries Elasticsearch, `semantic` queries Qdrant,
  and `hybrid` combines their rankings using reciprocal-rank fusion. Keyword
  terms are quoted as literals; the optional service is an additional required
  term. This is keyword matching, not per-service authorization. Qdrant still
  requires `RAG_ENABLED=true` and a populated collection with matching embeddings.
- `auto` (default) classifies the question as `logs`, `metrics`, `knowledge`,
  or `mixed`. Metrics select Grafana only (`METRIC_QUERY`, resolved search mode
  `metrics`); logs select ES (`KEYWORD_SEARCH`); similarity/knowledge selects
  Qdrant (`SEMANTIC_SEARCH`); mixed queries select all three (`HYBRID_SEARCH`).
  Causal questions add `RCA_QUERY`. Explicit similarity intent takes precedence
  over resource words such as memory; causal intent takes precedence over both.
  Classification is deterministic and heuristic. An explicit search mode overrides
  the source route. Discovery, validation, and per-request health pings respect
  selected MCP sources; pure semantic requests do not ping MCP servers. Global
  startup/readiness still supervises both MCP connections. Knowledge answers cite
  static guidance, never a verified incident cause.
- Prometheus-only requests use `MAX_METRIC_TIME_RANGE_HOURS` (**720 by default**).
  Log and hybrid requests use `MAX_TIME_RANGE_HOURS` (**720 by default**), matching
  the UI's maximum 30-day range. Configure the same approved value (up to 720)
  on **both the backend and Elasticsearch MCP**. Redeploy
  ES MCP for relevance-ranked keyword search; older tool schemas fall back to
  their existing ordering. Recent-first ordering remains the ES tool default.

| Question | Intents | Sources |
| --- | --- | --- |
| CPU of host X for last 5 days | `METRIC_QUERY` | Grafana, five-day range |
| Show me the cpu and memory of elastic search pod | `METRIC_QUERY` | Grafana, discover Elasticsearch pod/namespace labels and query both resources |
| find errors containing connection refused yesterday | `KEYWORD_SEARCH` | ES, exact phrase `connection refused`, yesterday's calendar window |
| find incidents similar to this memory problem | `SEMANTIC_SEARCH` | Qdrant |
| why did this pod fail yesterday? | `HYBRID_SEARCH` + `RCA_QUERY` | ES + Qdrant + Grafana |

Compact filter durations such as `time_range: "5d"` and `"6h"` are also accepted.
CPU is reported as rate in cores and memory as working-set bytes from discovered
metrics. Target labels must be verified, not inferred from an environment name.
Without a time expression/filter, the previous-hour default still applies.

Every investigation response includes `time_range`, `search_mode`,
`query_classification`, `query_intents`, `query_sources`, and `data_coverage`. The resolved window is expressed
as UTC ISO timestamps; its `timezone` records the calendar interpretation.
Grafana queries use range mode, bounded step sizes, and inward-rounded timestamps.
Each source's coverage reports status, returned sample/document count, actual
queried ranges, and first/last observed timestamps when parseable. Counts are
returned records across calls, not unique records or total matching documents.
Empty, failed, truncated/unparseable, disabled, and unqueried sources are
distinguished. `available` does **not** prove full interval coverage or absence
of gaps. Qdrant runbooks are explicitly `static_knowledge`: they have no event-time
filter or fabricated time coverage. Hybrid retrieval survives either source failing.

Investigation HTTP errors retain `detail` and include metadata too. If the window
could not be resolved, `time_range` is null. A deadline after graph execution
started reports unknown coverage, not a claim that no data was queried.

The graph runs `RAG -> Planner -> Tool -> Planner ... -> Evidence -> Analyst`.
The LLM selects one approved tool and its arguments based on the question,
service, environment, and previous results. It can investigate a follow-up or
finish early when further queries would not help. The old fixed Grafana and ES
nodes are retained for compatibility but are no longer part of the graph.

Tool schemas are discovered once per investigation and filtered through the
read-only allowlist. Before executing, the backend validates the input schema,
restricts the ES index and Grafana datasource to configured values, constrains
explicit query timestamps to the investigation window, and limits result-count
arguments to 100. Missing index, datasource and time arguments are filled from
trusted configuration and request state, not chosen freely by the LLM.

Grafana timestamps, including discovery `startRfc3339`/`endRfc3339`, are sent as
UTC whole-second RFC3339 values ending in `Z`. Starts round up and ends round
down so the query never expands the investigation window. A planner-supplied
start truncated to the request's starting second is moved inside that window.
`now` and `now-<duration>` (a single duration in ms/s/m/h/d/w, including decimals)
resolve against the investigation end rather than the wall clock. Larger window
violations, reversed ranges, and ranges with no usable whole-second timestamps
are still rejected. Elasticsearch timestamp handling is unchanged.

`MAX_TOOL_CALLS` is the tool-attempt budget: rejected, failed, and duplicate
attempts count too. Identical normalized calls are not executed again. The API
also enforces `MAX_INVESTIGATION_SECONDS` and sizes the graph recursion limit
for the configured budget. Failed calls and stopping reasons are supplied to
the final analyst; evidence contains unique references and query arguments.
If the API deadline expires, it returns HTTP 504 rather than a partial RCA.

The planner requires an OpenAI-compatible LLM endpoint with function-calling
support. The existing final analyst also requires structured-output support.
Local tests mock these responses; verify support against your actual model.
No infrastructure changes or remediation actions are enabled.
Prometheus query workload and lookback limits should also be enforced server-side;
timestamp validation alone does not constrain every expression's data access.

## Connection settings

Configure the following environment variables, or update `k8s/configmap.yaml`:

| Setting | Value |
| --- | --- |
| `METRICS_PROVIDER` | `prometheus` in the supplied Kubernetes configuration; `grafana` retains legacy deployments |
| `PROMETHEUS_MCP_URL` | `http://prometheus-mcp-server.prometheus-mcp.svc:8080/mcp` |
| `PROMETHEUS_MCP_TRANSPORT` | `streamable_http` (default) |
| `PROMETHEUS_QUERY_TOOL` | `range_query`, matching prometheus/prometheus-mcp v0.18.0 |
| `PROMETHEUS_MCP_TOKEN` | Optional bearer token for the Prometheus MCP endpoint; empty unless required, never reused from Grafana |
| `GRAFANA_MCP_URL` | Grafana MCP SSE endpoint (`/sse`) or Streamable HTTP endpoint (`/mcp`) |
| `GRAFANA_MCP_TRANSPORT` | `sse` (default) or `streamable_http`; must match the running server |
| `GRAFANA_MCP_TOKEN` | Optional MCP server bearer token; store it in a Secret |
| `GRAFANA_DATASOURCE_UID` | UID of the Grafana Prometheus datasource |
| `ELASTICSEARCH_MCP_URL` | Custom ES MCP endpoint, normally `http://elasticsearch-mcp.es-mcp.svc.cluster.local:8000/mcp` |
| `ELASTICSEARCH_MCP_TRANSPORT` | `streamable_http` (default) or `sse` |
| `ELASTICSEARCH_MCP_API_KEY` | Optional MCP gateway API key, sent as `Authorization: ApiKey ...` |
| `ES_INDEX` | `logstash-aitools-*` (default) or `tfs-speech-prod`, as allowed by the custom server |

The custom ES MCP in `elk-mcp/es-mcp/app/server.py` does not validate inbound
MCP authentication. Leave `ELASTICSEARCH_MCP_API_KEY` empty unless an
authentication gateway is configured. Its `ELASTICSEARCH_API_KEY` is a separate
database credential that belongs only on the ES MCP server, not the agent.
Keep this unauthenticated MCP endpoint restricted to trusted callers.

The supplied deployment replaces Grafana MCP with Prometheus MCP, backed by the
Thanos URL configured in `../prometheus-mcp/values.yaml`. No Grafana datasource UID
or Grafana token is needed. The backend uses `/mcp`; the separate
`http://prometheus-mcp-server.prometheus-mcp.svc:8080/metrics` endpoint exposes
MCP-server telemetry and is not a workload-metric query endpoint.

For compatibility with the existing UI, API source identifiers and state fields
such as `grafana` and `grafana_results` still identify the metrics slot. With
`METRICS_PROVIDER=prometheus`, that slot connects only to Prometheus MCP. Existing
UI labels may therefore still say Grafana. `GRAFANA_*` settings apply only when
explicitly selecting the legacy Grafana provider.

For local testing:

```bash
kubectl port-forward -n prometheus-mcp svc/prometheus-mcp-server 8080:8080
```

Set `METRICS_PROVIDER=prometheus` and
`PROMETHEUS_MCP_URL=http://localhost:8080/mcp` on the local backend. Run the backend
on another port, for example `uvicorn app.main:app --port 8081`, since the forward
occupies 8080. MCP-server telemetry is at `http://localhost:8080/metrics`.

After rebuilding the backend image and updating its deployment image digest,
apply the ConfigMap and roll out the backend; environment changes require a pod
restart. Verify the discovered tools through the authenticated `/mcp/tools` API.

The planner selects PromQL and log queries dynamically. Queries must still match
the metrics, labels and log fields available in your environment; failed queries
are returned to the planner for a corrected or alternative follow-up.

### Kubernetes CPU and memory queries

The deployed Prometheus `range_query` schema uses `query`, `start_time`,
`end_time`, and optional string `step`. The backend normalizes PromQL aliases to the discovered expression field and
rejects missing or blank expressions, even when the server schema marks them
optional. No fixed HTTP-error query is used by the investigation graph.

The planner can use read-only `label_names`, `label_values`, `series`, and
`metric_metadata` before querying. Metric names are discovered using `label_values`
with `label=__name__`. Time-aware discovery stays within the request window;
discovery metadata is not treated as measured utilization. The instant `query`
tool can inspect labels at the investigation end; it does not replace the required
range queries. Destructive tools, including `reload`, `quit`, `delete_series`, and
`snapshot`, remain blocked even if the server advertises them.
For current Kubernetes usage, the planner is instructed to collect both CPU
rate and memory working set and verify labels before filtering by environment.
Actual metrics depend on what your Prometheus-compatible datasource scrapes.

MCP failures include a bounded, credential-redacted server message in
`investigation_errors`, so datasource, authorization and PromQL errors can be
distinguished. Tool discovery succeeding does not prove metric queries succeed.
After rebuilding and rolling out the backend, repeat the investigation and check
these details. A datasource UID is only required by the legacy Grafana provider.
`RAG retrieval failed (HTTPStatusError)` is a separate embedding/Qdrant failure;
disable `RAG_ENABLED` when only live Grafana evidence is needed, or fix that
retrieval service independently.

The backend requires the existing LLM configuration. Qdrant configuration is
only needed for a separately provisioned RAG runtime. Before
deploying, replace the LLM and image placeholders, provide credentials via
Secrets, and add the approved LLM destination to the NetworkPolicy.

## Verify the deployed connection

### RAG HTTP failures

The RAG node now distinguishes `Embedding API HTTP <status>` from
`Qdrant query HTTP <status>` in `investigation_errors`. It does not expose
upstream response bodies, URLs, request text, or credentials. Live Grafana and
Elasticsearch evidence collection continues if retrieval fails.

- Embedding 401/403: check `EMBEDDING_API_KEY` (or its `LLM_API_KEY` fallback)
  and permission to use the embedding model, not the Grafana MCP token.
- Embedding 400/404: verify that `EMBEDDING_BASE_URL` exposes the OpenAI-compatible
  `/embeddings` endpoint and that `EMBEDDING_MODEL` is enabled under its exact
  gateway model/provider name. A working chat endpoint does not establish this.
- Qdrant 401/403: check `QDRANT_API_KEY` and access to the configured collection.
- Qdrant 404: check `QDRANT_COLLECTION`, the native REST endpoint, and support
  for `/collections/<collection>/points/query` in the deployed Qdrant version.
- Qdrant 400/422: verify vector dimensions, `QDRANT_VECTOR_NAME`, and the
  embedding model against the model used to populate the collection.
- Either service 429/5xx: check quotas, rate limits, and upstream availability.

Rebuild and deploy the backend to obtain the service-specific diagnostics before
changing models, recreating a collection, or rotating unrelated credentials.
Do not disable RAG just to suppress its error if historical retrieval is needed.

### Ingest the 768-dimensional knowledge collection

A green collection with `points_count: 0` is healthy but contains no runbooks.
The current `sre_knowledge` collection uses unnamed vectors of size 768. Set
`EMBEDDING_DIMENSIONS=768` for both ingestion and retrieval, keep
`QDRANT_VECTOR_NAME` empty, and use the same `EMBEDDING_MODEL` for both.
The retriever sends the dimensions parameter to the embedding gateway and
rejects a response of the wrong size before querying Qdrant. If the gateway
ignores that parameter, enable dimension support or use a supported model;
never slice/pad vectors or mix embedding models in a populated collection.

Build and push the updated backend image before running this sequence. From
the repository root, set `NEW_BACKEND_IMAGE` to that image's immutable reference
and `RUNBOOK_FILE` to an actual UTF-8 runbook on your workstation:

```sh
kubectl -n sre-ai patch configmap sre-ai-agent-config --type merge \
  -p '{"data":{"EMBEDDING_DIMENSIONS":"768"}}'
kubectl -n sre-ai set image deployment/sre-ai-agent \
  sre-ai-agent="$NEW_BACKEND_IMAGE"
kubectl -n sre-ai rollout status deployment/sre-ai-agent
kubectl -n sre-ai exec -i deployment/sre-ai-agent -c sre-ai-agent -- \
  python -m app.rag.ingest --source runbooks/tfs-aiva-stg.md < "$RUNBOOK_FILE"
```

These commands patch only the RAG dimension setting and roll out the backend;
they do not change Grafana MCP, its credentials, or its working datasource.
The ingester checks collection dimensions and vector names before embedding,
creates overlapping text chunks, and upserts UUID-identified points with
`wait=true`. It never creates, deletes, or recreates a collection. Repeating the
same source/content is idempotent; shortening a previously ingested document can
leave old trailing chunks, so manage removed chunks separately. Do not run the
API-key generation section of the Qdrant setup steps on an existing installation.

Verify retrieval from inside the backend pod, using its existing credentials:

```sh
kubectl -n sre-ai exec deployment/sre-ai-agent -c sre-ai-agent -- \
  python -c 'import asyncio; from app.rag.retriever import retrieve_knowledge; hits = asyncio.run(retrieve_knowledge("tfs-aiva-stg CPU and memory runbook")); print("RAG hits:", len(hits)); raise SystemExit(0 if hits else 1)'
```

Expect at least one hit after ingesting a relevant runbook. Repeat the API
investigation to verify RAG and live Grafana evidence together. If ingestion
returns Qdrant 403, use a write-capable ingestion credential; investigation-only
credentials may intentionally be read-only. Existing nonempty collections made
with a different model require a planned re-embedding migration, not just a
dimension setting change.

### Grafana datasource lookup returns 401

An MCP error such as `get datasource by uid thanos ... [401] Unauthorized`
identifies the MCP-to-Grafana authentication hop, not the API key used by the
investigation client. Successful `/mcp/tools` discovery does not test this hop.

The Grafana MCP chart consumes `envValueFrom`, not `extraEnv`. The values in
`../grafana-mcp/values.yaml` inject two independent credentials from the existing
`grafana-mcp-secret` Secret in the `grafana-mcp` namespace:

- `GRAFANA_SERVICE_ACCOUNT_TOKEN`: outbound Grafana authentication, using a
  service-account token issued by the Grafana instance configured in `grafana.url`.
- `MCP_GRAFANA_SERVER_TOKEN`: inbound MCP caller authentication; the backend's
  `GRAFANA_MCP_TOKEN` must match this token, not the Grafana service-account token.

Upgrade the existing MCP Helm release with the corrected values and the chart
version already deployed; do not change the backend token to fix this 401:

```sh
helm upgrade "$GRAFANA_MCP_RELEASE" grafana-mcp \
  --repo https://grafana.github.io/helm-charts \
  --version "$GRAFANA_MCP_CHART_VERSION" \
  --namespace grafana-mcp -f grafana-mcp/values.yaml
kubectl -n grafana-mcp rollout status deployment \
  -l "app.kubernetes.io/instance=$GRAFANA_MCP_RELEASE"
```

Run these commands from the repository root, setting the release name and chart
version to the existing installation. The changed environment specification
triggers a pod rollout. For later Secret-only updates, restart the deployment
because environment variables do not refresh in running pods.

Repeat the investigation after rollout. If the datasource lookup still returns
401, replace the expired, revoked, or incorrect Grafana service-account token
through your secret manager. Grant only the required read/query permissions
(`datasources:read` and `datasources:query` for datasource UID `thanos`, where
fine-grained RBAC is available). A 403 indicates a permission check; a 404 calls
for checking the datasource UID and organization after authentication succeeds.
Never log tokens or use the MCP caller token as Grafana's bearer credential.

### Repeated MCP connection-cycle failures

HTTP 200/202 transport responses do not prove the MCP initialization handshake
completed. Startup is transactional: a failure in any required server closes
the other sessions and retries the entire cycle. The warning includes the
exception-group traceback and the failing server/transport; configured credentials
are redacted before logging.

When `RAG_ENABLED=true`, Qdrant is queried directly over its native REST API
(no MCP). Set `QDRANT_URL` to an absolute URL such as
`http://qdrant.qdrant-vector.svc.cluster.local:6333`, `QDRANT_API_KEY` to the
Qdrant service API key, and `EMBEDDING_MODEL` to the model used to ingest the
collection (called via `EMBEDDING_BASE_URL`/`embeddings`, defaulting to the LLM
endpoint). Qdrant is not part of MCP startup or readiness; RAG failures are
reported as investigation errors and do not block live MCP evidence.
After updating configuration, roll out the backend again and check `/readyz`.

Forward the backend service from your cluster:

```sh
kubectl -n sre-ai port-forward service/sre-ai-agent 8080:8080
```

In another terminal:

```sh
curl --fail http://localhost:8080/readyz
curl --fail http://localhost:8080/mcp/tools -H "X-API-Key: $SRE_AGENT_API_KEY"
curl --fail http://localhost:8080/api/v1/investigate \
  -H "X-API-Key: $SRE_AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"Investigate HTTP 5xx errors in the last hour"}'
```

Set `SRE_AGENT_API_KEY` locally from your secret manager without sharing it in chat.
`/readyz` pings both initialized sessions. `/mcp/tools` performs live
discovery from both servers and returns their input schemas. Inspect the
investigation response for tool errors and evidence to confirm queries succeed;
initialization alone does not prove tool calls work.

## Focused tests

For tests only, install the core dependencies without the embedding model stack.
Run from `ai-backend` using Python 3.12:

```sh
python -m pip install -r requirements-core.txt
python -m unittest discover -s tests/unit -t . -v
```

Unit coverage lives in `tests/unit/`, including API guards, MCP lifecycle,
adaptive planning, evidence validation and RAG cancellation. Integration tests
live in `tests/integration/`: one runs the real graph with remote services mocked;
another exercises the actual MCP SDK over an in-process Streamable HTTP server.
Run integration independently, or discover both suites:

```sh
python -m unittest discover -s tests/integration -t . -v
python -m unittest discover -s tests -t . -v
```

These tests mock remote MCP sessions and the LLM. They check transport selection,
optional authentication, schema and access guards, adaptive follow-up, duplicate
rejection, failure feedback, cancellation, and real LangGraph execution up to
the maximum attempt budget. Additional unit cases cover malformed planner
decisions, MCP protocol errors, and result serialization. There are currently
unit and local protocol tests, not deployed-service end-to-end tests.
They do not require access to Grafana, Elasticsearch, Qdrant, or an LLM.
The Docker runtime targets Python 3.12;
MCP 1.30 has stricter Pydantic and Starlette requirements on Python 3.14.
## Query routing and time-range hardening (updated)
- Auto routing prioritizes metric requests so incidental terms such as `matching` do not route CPU/memory questions to Elasticsearch.
- Metric requests use Grafana; keyword/log requests use Elasticsearch; semantic requests use Qdrant; RCA/mixed requests use the relevant sources.
- Relative windows support minutes, hours, days, and weeks and do not depend on browser timezone. Calendar windows (`today`/`yesterday`) use a validated IANA timezone.
- Planner instructions require actual Prometheus queries for metrics; discovery-only responses are not sufficient.
- The bundled `k8s/secret.yaml` is a template. Replace all `REPLACE_WITH_SECRET` and `REPLACE_WITH_GENERATED_KEY` values before applying it. Do not commit production credentials.
