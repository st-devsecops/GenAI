# SRE AI Agent — production candidate

This package is a bounded, read-only SRE investigation service. The supplied deployment talks to Prometheus, Elasticsearch, and Kubernetes MCP servers, and (optionally) queries Qdrant directly over its REST API for RAG. Kubernetes access is through the read-only MCP service; the backend does not use a Kubernetes API credential. Legacy Grafana MCP remains optional through `METRICS_PROVIDER=grafana`.

## Production prerequisites

1. Replace all `REPLACE_*` configuration values.
2. Configure External Secrets Operator and your Vault `ClusterSecretStore` to create `sre-ai-agent-secrets`.
3. Set `EMBEDDING_MODEL` to the model used to ingest the Qdrant collection, and `QDRANT_VECTOR_NAME` if the collection uses named vectors.
4. Set the real immutable Harbor image digest in `k8s/deployment.yaml`.
5. Configure NetworkPolicy egress for the actual MCP and LLM endpoints. The supplied policy only covers the example in-cluster MCP namespaces; external LLM egress must be explicitly added for your network/CNI.
6. Run dependency installation, unit/integration tests, image build, Trivy scan and image signing in CI before promotion.
7. Rotate any credentials that existed in the original archive; they were removed from this package.

## Architecture

User/UI -> FastAPI -> LangGraph -> read-only MCP tools (Prometheus / Elasticsearch / Kubernetes) + direct Qdrant vector search -> evidence correlation -> metric answer or LLM RCA.

Set `KUBERNETES_MCP_URL` to the in-cluster `/mcp` endpoint. The backend allowlists only the discovered read tools for pods, pod logs/top, nodes, namespaces, events and generic resource get/list. The Kubernetes MCP service account must retain its read-only RBAC; if the cluster enforces egress NetworkPolicies outside this package, allow backend pods to reach namespace `k8-mcp` on TCP 8080.

Kubernetes-specific states such as `ImagePullBackOff`, `ErrImagePull`,
`CrashLoopBackOff`, and `OOMKilled` route to the Kubernetes MCP rather than
Elasticsearch text search. This endpoint inspects one configured current
cluster. Selecting All environments does not fan out Kubernetes queries to
separate STG/PROD/HUB clusters; deploy/federate additional MCP endpoints for
multi-cluster inspection.

No model weights, `.venv`, Qdrant SDK, or static credentials are included in the runtime image.

## Investigation environment scope

The UI sends `environment` as `all`, `stg`, `prod`, or `hub`. An explicitly
supplied value is authoritative for both metrics and logs, including `all`,
which checks all three supported environments. Query-based inference is only
used when an API caller omits the field.

Prometheus evidence must carry a matching `env` or `environment` label. All-mode
results retain environment labels and report missing resources per environment.
Elasticsearch search, errors, 5xx, timeout, and error-aggregation tools apply an
exact environment filter; planned calls cannot override the selected scope.
Component log searches return per-environment counts and bounded samples, not
complete exports. Rebuild and deploy the UI, backend, and ES MCP together.

## Elasticsearch index scope

The backend default and supplied ConfigMap use:

```text
ES_INDEX=apisix-*,logstash-*,speech-*,target-*,tfs-*
```

Unscoped log searches query all five patterns. Planned tool calls can select
individual patterns or comma-separated subsets such as `speech-*,tfs-*`.
Both backend validation and Elasticsearch MCP reject other patterns, empty
selectors, exclusions, remote-cluster selectors, and unrestricted `*`/`_all`.
Each returned log retains its originating index.

Deploy the updated Elasticsearch MCP from `elk-mcp/es-mcp`, rebuild and deploy
the backend, and apply the updated ConfigMap. Restart backend pods after a
ConfigMap change because `envFrom` values are loaded only at process startup.
Updating only `ES_INDEX` is insufficient: an older MCP image rejects the
combined selector. Ensure the Elasticsearch API key has read access to these
five index families; application allowlists do not grant Elasticsearch permissions.
