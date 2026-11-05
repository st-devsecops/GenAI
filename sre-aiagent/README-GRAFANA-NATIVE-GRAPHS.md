# Prometheus MCP + SRE AI native graphs

This bundle adds native metric graphs to the existing SRE AI Agent.

## Architecture

React UI -> FastAPI/LangGraph backend -> Prometheus MCP -> Thanos/Prometheus

The backend converts the Prometheus MCP range-query response into a small, stable
`visualizations` payload. The React UI renders the payload as native SVG line charts.
No Grafana iframe or Grafana API is required for the AI response charts.

## Files changed

### AI backend
- `app/visualization.py` — converts Prometheus range results into chart JSON.
- `app/models.py` — adds `visualizations` to `RCAResponse`.
- `app/main.py` — returns visualizations from Prometheus results.
- `app/mcp/client.py` — keeps up to 120 evenly distributed samples per Prometheus
  range series instead of reducing a series to only first/last samples. This is
  required so the UI has enough points to draw a useful graph.

### AI UI
- `src/components/MetricChart.tsx` — native SVG line chart.
- `src/types.ts` — visualization types and response field.
- `src/App.tsx` — renders metric charts in the existing Investigation Results panel.
- `src/styles.css` — chart styling.

### Prometheus MCP
The supplied `prometheus-mcp.tar.gz` contains only the Helm values file, not the
MCP server source. No MCP server-code modification was possible or necessary from
that archive. Its current values point to:

- image: `xxxx/tfs-docker/prometheus-mcp-server:0.18.0`
- digest: `sha256:649949f954c0bac9397e77fb17f280e0cac2fb481b1248fa536d630b9fc542b5`
- backend: `thanos`
- URL: `http://thanos-query-frontend.prom-monitoring.svc.cluster.local:9090`
- service: ClusterIP port `8080`

The backend expects the configured Prometheus MCP range-query tool to be `range_query`
(the current backend default).

## Response example

`POST /api/v1/investigate` now can return:

```json
{
  "visualizations": [
    {
      "id": "container-memory-working-set-bytes-1",
      "type": "line",
      "title": "Memory",
      "metric": "container_memory_working_set_bytes",
      "unit": "MiB",
      "series": [
        {
          "name": "pod-1",
          "labels": {"pod": "pod-1", "namespace": "tfs-aiva-stg"},
          "points": [
            {"timestamp": 1790000000, "value": 808.2},
            {"timestamp": 1790000060, "value": 810.4}
          ]
        }
      ]
    }
  ]
}
```

## Validation performed

- Backend Python compile check: passed.
- Visualization parser smoke test for CPU + memory: passed.
- React TypeScript `tsc -b`: passed.

A live MCP/Thanos query was not executed because the supplied files do not include
cluster credentials/network access.

## Deployment

Build/deploy the backend and UI images from the included source trees. Keep the
existing Prometheus MCP deployment and its internal ClusterIP endpoint. After
backend deployment, verify `/mcp/tools` contains `range_query` and submit a metric
question such as:

`Show me CPU and memory for the Elasticsearch pod over the last 1 hour.`

The response should contain a **Metrics** section with native line graphs.
