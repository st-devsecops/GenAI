"""
Evidence collection node.

Normalizes results from Qdrant, Prometheus MCP and Elasticsearch MCP
into a bounded evidence set for the RCA model.
"""

import json
import math
import re
from datetime import datetime, timezone

from app.config import settings
from app.mcp.registry import PROMETHEUS_DISCOVERY_TOOLS
from app.state import InvestigationState


# Prevent huge MCP responses from consuming the entire LLM context.
MAX_TEXT_PER_SOURCE = 12000


def scope_metric_result(result: dict, environment: str | None) -> dict:
    if not environment or result.get("is_error"):
        return result
    allowed_environments = {"stg", "prod", "hub"} if environment == "all" else {environment}
    payloads = [result["structured"]] if result.get("structured") is not None else []
    if not payloads:
        for item in result.get("content", []):
            try:
                payloads.append(json.loads(item.get("text", "")))
            except (ValueError, TypeError, AttributeError):
                pass
    excluded = set()
    accepted = 0

    def scoped(value):
        nonlocal accepted
        if isinstance(value, dict):
            if isinstance(value.get("metric"), dict) and ("values" in value or "value" in value):
                labels = value["metric"]
                environments = [labels[key] for key in ("environment", "env") if key in labels]
                if not environments or any(label not in allowed_environments for label in environments) or len(set(environments)) != 1:
                    excluded.update(str(label) for label in environments or ["unlabelled"])
                    return None
                accepted += 1
                return value
            return {key: scoped(item) for key, item in value.items()}
        if isinstance(value, list):
            return [filtered for item in value if (filtered := scoped(item)) is not None]
        return value

    filtered = [scoped(payload) for payload in payloads]
    if not excluded:
        return result
    warning = (
        f"Excluded metric series outside verified environment={environment}: "
        f"{', '.join(sorted(excluded))}."
    )
    if not accepted:
        warning += f" No verified {environment}-labeled measurements were returned."
    return {
        **result,
        "structured": filtered[0] if len(filtered) == 1 else filtered,
        "content": [{"type": "text", "text": warning if not accepted else json.dumps(filtered)}],
        "is_error": not accepted,
        "scope_warning": warning,
    }


def metric_summary_table(state: InvestigationState) -> list[dict]:
    rows = {}
    allowed_environments = {"stg", "prod", "hub"}

    def series_in(payload):
        if isinstance(payload, dict):
            if isinstance(payload.get("metric"), dict) and ("values" in payload or "value" in payload):
                yield payload
            else:
                for value in payload.values():
                    yield from series_in(value)
        elif isinstance(payload, list):
            for value in payload:
                yield from series_in(value)

    for record in state.get("prometheus_results", []):
        if record.get("tool") not in {settings.metrics_query_tool, "query", "query_prometheus"}:
            continue
        result = scope_metric_result(record.get("result") or {}, state.get("environment", "all"))
        if result.get("is_error"):
            continue
        arguments = record.get("arguments") or {}
        expression = next((arguments[key] for key in ("query", "expr", "promql", "prometheus_query") if key in arguments), "")
        if not isinstance(expression, str) or not expression:
            continue
        calculation = re.sub(r'"(?:\\.|[^"\\])*"', '""', expression)
        if "container_cpu_usage_seconds_total" in calculation and "rate(" in calculation and not any(operator in calculation for operator in ("/", "*")):
            resource, divisor, unit = "cpu", 1, "cores"
        elif "container_memory_working_set_bytes" in calculation and not any(operator in calculation for operator in ("/", "*")):
            resource, divisor, unit = "memory", 1024 ** 3, "GiB"
        else:
            continue
        payloads = [result["structured"]] if result.get("structured") is not None else []
        if not payloads:
            for content in result.get("content", []):
                try:
                    payloads.append(json.loads(content.get("text", "")))
                except (ValueError, TypeError, AttributeError):
                    continue
        for payload in payloads:
            for series in series_in(payload):
                labels = series["metric"]
                environment = labels.get("environment", labels.get("env"))
                if environment not in allowed_environments:
                    continue
                pod = labels.get("pod") or labels.get("instance") or labels.get("node") or labels.get("container")
                if not pod:
                    continue
                namespace = labels.get("namespace", "")
                key = (environment, namespace, str(pod))
                row = rows.setdefault(key, {
                    "environment": environment, "namespace": namespace, "pod": str(pod),
                    "cpu": None, "memory": None,
                })
                values = series.get("values", [series.get("value")])
                samples = []
                for sample in values:
                    try:
                        timestamp, raw_value = sample
                        timestamp, value = float(timestamp), float(raw_value) / divisor
                        datetime.fromtimestamp(timestamp, timezone.utc)
                        if math.isfinite(timestamp) and math.isfinite(value):
                            samples.append((timestamp, value))
                    except (ValueError, TypeError, OverflowError, OSError):
                        continue
                if not samples:
                    continue
                samples.sort()
                summary = series.get("sample_summary")
                if isinstance(summary, dict) and summary.get("count", 0) > 0:
                    average = float(summary["average"]) / divisor
                    peak = float(summary["peak"]) / divisor
                    minimum = float(summary.get("minimum", min(value for _, value in samples) * divisor)) / divisor
                    count = int(summary["count"])
                else:
                    sample_values = [value for _, value in samples]
                    average = sum(sample_values) / len(sample_values)
                    minimum, peak, count = min(sample_values), max(sample_values), len(sample_values)
                metric_value = {
                    "latest": samples[-1][1], "average": average, "minimum": minimum,
                    "peak": peak, "samples": count, "unit": unit,
                    "sampled": bool(result.get("summarized") or result.get("truncated") or series.get("sample_summary")),
                }
                previous = row[resource]
                if previous is None or samples[-1][0] >= previous["_timestamp"]:
                    metric_value["_timestamp"] = samples[-1][0]
                    row[resource] = metric_value

    ordered = []
    for row in sorted(rows.values(), key=lambda item: (item["environment"], item["namespace"], item["pod"])):
        for resource in ("cpu", "memory"):
            if row[resource] is not None:
                row[resource].pop("_timestamp", None)
        ordered.append(row)
    return json.loads(redact_text(json.dumps(ordered)))


def metric_visualizations(state: InvestigationState) -> list[dict]:
    def series_in(payload):
        if isinstance(payload, dict):
            if isinstance(payload.get("metric"), dict) and ("values" in payload or "value" in payload):
                yield payload
            else:
                for value in payload.values():
                    yield from series_in(value)
        elif isinstance(payload, list):
            for value in payload:
                yield from series_in(value)

    charts = {}
    for record in state.get("prometheus_results", []):
        if record.get("tool") not in {settings.metrics_query_tool, "query", "query_prometheus"}:
            continue
        result = scope_metric_result(record.get("result") or {}, state.get("environment"))
        if result.get("is_error"):
            continue
        arguments = record.get("arguments") or {}
        expression = next((arguments[key] for key in ("query", "expr", "promql", "prometheus_query") if key in arguments), "")
        if not isinstance(expression, str) or not expression:
            continue
        calculation = re.sub(r'"(?:\\.|[^"\\])*"', '""', expression)
        if "container_cpu_usage_seconds_total" in calculation and "rate(" in calculation and not any(operator in calculation for operator in ("/", "*")):
            title, unit, divisor = "CPU", "cores", 1
        elif "container_memory_working_set_bytes" in calculation and not any(operator in calculation for operator in ("/", "*")):
            title, unit, divisor = "Memory", "MiB", 1024 ** 2
        else:
            title, unit, divisor = "Metric", "raw query units", 1
        payloads = [result["structured"]] if result.get("structured") is not None else []
        if not payloads:
            for content in result.get("content", []):
                try:
                    payloads.append(json.loads(content.get("text", "")))
                except (ValueError, TypeError, AttributeError):
                    continue
        chart_series = []
        for payload in payloads:
            for series in series_in(payload):
                points = {}
                for sample in series.get("chart_values", series.get("values", [series.get("value")])):
                    try:
                        timestamp, value = sample
                        timestamp, value = float(timestamp), float(value) / divisor
                        datetime.fromtimestamp(timestamp, timezone.utc)
                        if math.isfinite(timestamp) and math.isfinite(value):
                            points[timestamp] = value
                    except (ValueError, TypeError, OverflowError, OSError):
                        continue
                ordered = sorted(points.items())
                if not ordered:
                    continue
                if len(ordered) > 120:
                    ordered = [ordered[round(index * (len(ordered) - 1) / 119)] for index in range(120)]
                labels = {str(key): str(value) for key, value in series["metric"].items()}
                name = ", ".join(f"{key}={value}" for key, value in sorted(labels.items()) if key != "__name__") or "Unlabelled series"
                chart_series.append({"name": name, "labels": labels, "points": [
                    {"timestamp": timestamp, "value": value} for timestamp, value in ordered
                ]})
                if len(chart_series) >= 12:
                    break
            if len(chart_series) >= 12:
                break
        if chart_series and (expression in charts or len(charts) < 6):
            charts[expression] = {
                "id": f"metric-{list(charts).index(expression) if expression in charts else len(charts)}",
                "type": "line", "title": title, "metric": expression, "unit": unit,
                "start_time": state.get("start_time"), "end_time": state.get("end_time"),
                "summarized": bool(result.get("summarized") or result.get("truncated")),
                "series": chart_series,
            }
    return json.loads(redact_text(json.dumps(list(charts.values()))))


def data_coverage(state: InvestigationState) -> dict:
    coverage = {}
    statuses = state.get("retrieval_status", {})
    for source in ("prometheus", "elasticsearch", "kubernetes", "qdrant"):
        if source == "qdrant":
            count = len(state.get("rag_results", []))
            coverage[source] = {
                "status": statuses.get(source, "available" if count else "not_queried"),
                "returned_count": count, "scope": "static_knowledge",
            }
            continue
        if source == "kubernetes":
            records = state.get("kubernetes_results", [])
            kubernetes_statuses = ["failed" if (record.get("result") or {}).get("is_error") else "available" for record in records]
            status = "partial" if "available" in kubernetes_statuses and "failed" in kubernetes_statuses else kubernetes_statuses[-1] if kubernetes_statuses else "not_queried"
            coverage[source] = {"status": status, "returned_count": sum(value == "available" for value in kubernetes_statuses), "scope": "current_cluster"}
            continue
        records = state.get(f"{source}_results", [])
        ranges, timestamps = [], []
        count, successes, failures, unknown = 0, 0, 0, False

        def observe(value):
            try:
                parsed = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    timestamps.append(parsed.astimezone(timezone.utc))
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                pass

        def measurements(value):
            nonlocal count
            recognized = False
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"logs", "errors"} and isinstance(item, list):
                        count += len(item)
                        for log in item:
                            if isinstance(log, dict):
                                observe(log.get("timestamp") or log.get("@timestamp"))
                        recognized = True
                    elif key in {"values", "value"} and isinstance(item, list):
                        samples = item if key == "values" else [item]
                        for sample in samples:
                            if isinstance(sample, list) and len(sample) == 2 and isinstance(sample[0], (int, float)):
                                count += 1
                                observe(sample[0])
                                recognized = True
                    elif key == "result" and item == []:
                        recognized = True
                    else:
                        recognized = measurements(item) or recognized
            elif isinstance(value, list):
                for item in value:
                    recognized = measurements(item) or recognized
            return recognized

        for record in records:
            if source == "prometheus" and record.get("tool") in PROMETHEUS_DISCOVERY_TOOLS:
                continue
            arguments = record.get("arguments", {})
            starts = [value for key, value in arguments.items() if key.lower().replace("_", "") in {"starttime", "start", "from", "startrfc3339"}]
            ends = [value for key, value in arguments.items() if key.lower().replace("_", "") in {"endtime", "end", "to", "endrfc3339"}]
            if starts and ends:
                ranges.append({"start_time": str(starts[0]), "end_time": str(ends[0]), "timezone": "UTC"})
            result = record.get("result", {})
            if source == "prometheus":
                result = scope_metric_result(result, state.get("environment"))
            if result.get("is_error"):
                failures += 1
                continue
            successes += 1
            payloads = [result["structured"]] if result.get("structured") is not None else []
            if not payloads:
                for item in result.get("content", []):
                    try:
                        payloads.append(json.loads(item.get("text", "")))
                    except (ValueError, TypeError):
                        pass
            recognized = False
            for payload in payloads:
                recognized = measurements(payload) or recognized
            unknown |= not recognized or bool(result.get("truncated"))
        retrieval_status = statuses.get(source)
        if retrieval_status == "failed":
            failures += 1

        # A successful query with returned data is not automatically "partial".
        # Mark coverage partial only when observed timestamps prove that the live
        # data covers less than the requested investigation window. Unknown payload
        # shapes should be reported as unknown, not as a false partial result.
        status = "not_queried"
        if failures and not count:
            status = "failed"
        elif successes and count:
            if timestamps:
                try:
                    requested_start = datetime.fromisoformat(state["start_time"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    requested_end = datetime.fromisoformat(state["end_time"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    observed_start = min(timestamps)
                    observed_end = max(timestamps)
                    status = "available" if observed_start <= requested_start and observed_end >= requested_end else "partial"
                except (KeyError, ValueError, TypeError):
                    status = "unknown"
            else:
                status = "partial" if unknown and bool(result.get("truncated")) else "available"
        elif successes:
            status = "unknown" if unknown else "empty"
        elif failures:
            status = "failed"
        coverage[source] = {
            "status": status, "returned_count": count, "queried_ranges": ranges,
            "observed_start": min(timestamps).isoformat() if timestamps else None,
            "observed_end": max(timestamps).isoformat() if timestamps else None,
        }
    return coverage


def redact_text(text: str) -> str:
    values = [
        settings.qdrant_api_key, settings.embedding_api_key, settings.llm_api_key,
        settings.prometheus_mcp_token, settings.prometheus_mcp_token, settings.elasticsearch_mcp_api_key,
    ]
    values.extend(key.get_secret_value() for key in settings.api_auth_keys.values())
    for value in values:
        if value and len(value) >= 8:
            text = text.replace(json.dumps(value)[1:-1], "[REDACTED]")
    text = re.sub(r"(?i)(\bapi[_-]?key=)[^&\s\"\\#]+", r"\1[REDACTED]", text)
    return re.sub(r"(?i)(Bearer\s+)[^\s\"\\]+", r"\1[REDACTED]", text)


def bounded_context(context: dict, limit: int) -> str:
    payload = json.loads(redact_text(json.dumps(context, default=str)))
    while True:
        encoded = json.dumps(payload, default=str)
        if len(encoded) <= limit:
            return encoded
        payload["context_truncated"] = True
        if payload.get("knowledge"):
            payload["knowledge"] = ""
        elif payload.get("previous_calls"):
            payload["previous_calls"].pop(0)
        elif payload.get("previous_decisions"):
            payload["previous_decisions"].pop(0)
        elif payload.get("errors"):
            payload["errors"].pop(0)
        else:
            raise ValueError("Approved tool catalog exceeds the model context budget")


def _clean(value) -> str:
    """Converts tool output into bounded JSON text."""

    try:

        text = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
        )

    except Exception:

        text = str(value)

    return redact_text(text)[:MAX_TEXT_PER_SOURCE]


async def evidence_node(
    state: InvestigationState,
):
    """
    Combines all data sources into one normalized evidence list.

    The LLM receives this evidence instead of direct unrestricted
    access to the MCP servers.
    """

    evidence = []

    # --------------------------------------------------
    # Historical/static knowledge
    # --------------------------------------------------

    for item in state.get(
        "rag_results",
        [],
    ):

        evidence.append(
            {
                "source": "qdrant",
                "type": "knowledge",
                "reference": item.get(
                    "source",
                    "unknown",
                ),
                "content": _clean(item),
            }
        )

    # --------------------------------------------------
    # Live Prometheus evidence
    # --------------------------------------------------

    for position, item in enumerate(state.get("prometheus_results", []), start=1):

        evidence.append(
            {
                "source": "prometheus",
                "type": (
                    "tool_error" if item.get("result", {}).get("is_error")
                    else "metric_metadata" if item.get("tool") in PROMETHEUS_DISCOVERY_TOOLS
                    else "live_metric"
                ),
                "reference": f"prometheus/{item.get('tool', 'unknown')}/{position}",
                "content": _clean(item),
            }
        )

    # --------------------------------------------------
    # Live Elasticsearch evidence
    # --------------------------------------------------

    for position, item in enumerate(state.get("elasticsearch_results", []), start=1):

        evidence.append(
            {
                "source": "elasticsearch",
                "type": "tool_error" if item.get("result", {}).get("is_error") else "live_log",
                "reference": f"elasticsearch/{item.get('tool', 'unknown')}/{position}",
                "content": _clean(item),
            }
        )

    for position, item in enumerate(state.get("kubernetes_results", []), start=1):
        evidence.append({
            "source": "kubernetes",
            "type": "tool_error" if item.get("result", {}).get("is_error") else "live_kubernetes",
            "reference": f"kubernetes/{item.get('tool', 'unknown')}/{position}",
            "content": _clean(item),
        })

    remaining = settings.max_context_chars - 8000
    bounded = []
    for item in reversed(evidence):
        overhead = len(item["reference"]) + 200
        allowed = min(len(item["content"]), remaining - overhead)
        if allowed <= 0:
            break
        bounded.append({**item, "content": item["content"][:allowed]})
        remaining -= allowed + overhead

    return {
        **state,
        "evidence": list(reversed(bounded)),
    }