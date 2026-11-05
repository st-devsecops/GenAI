"""Deterministic query classification and bounded investigation windows."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def classify_query(query: str) -> str:
    """Classify the primary task with metric intent taking precedence over incidental words."""
    text = query.lower()
    rca = bool(re.search(r"\b(why|root\s+cause|rca|investigate\s+why)\b", text))
    metric = bool(re.search(
        r"\b(cpu|memory|ram|disk|latency|metrics?|promql|utilization|throughput|"
        r"resource usage|request rate|error rate|network traffic|load average)\b", text
    ))
    log = bool(re.search(
        r"\b(log|logs|error|errors|exception|exceptions|timeout|timeouts|"
        r"stacktrace|connection refused|http 5\d\d|status code)\b", text
    ))
    semantic = bool(re.search(
        r"\b(similar|similarity|related incidents?|previous incident|previous rca|"
        r"runbook|sop|known issue|how was .* solved|historical solution)\b", text
    ))
    kubernetes_resource = bool(re.search(
        r"\b(pods?|nodes?|namespaces?|deployments?|replicasets?|statefulsets?|daemonsets?|"
        r"jobs?|cronjobs?|services?|endpoints?|ingresses?|events?|pvc|persistentvolumeclaims?)\b",
        text,
    ))
    kubernetes_action = bool(re.search(r"\b(list|show|get|describe|inspect|check|find|status|logs?)\b", text))
    kubernetes_failure = bool(re.search(
        r"\b(image\s*pull(?:ing)?(?:\s+(?:error|errors|failure|failures|backoff))?|"
        r"imagepullbackoff|errimagepull|crashloopbackoff|crash\s*loop|oomkilled|"
        r"oom\s*killed|unschedulable|evicted\s+pods?|pending\s+pods?)\b",
        text,
    ))
    if kubernetes_failure and not rca:
        return "kubernetes"
    # Explicit RCA and requests requiring both telemetry and log evidence are mixed.
    if rca or (metric and log):
        return "mixed"
    # Metric words win over incidental words such as 'pod', 'matching', or a hostname.
    if metric:
        return "metrics"
    if semantic:
        return "knowledge"
    if log:
        return "logs"
    if kubernetes_resource and kubernetes_action:
        return "kubernetes"
    if re.search(r"\b(explain|procedure|documentation|how do i)\b", text):
        return "knowledge"
    return "mixed"


def route_query(query: str, search_mode: str = "auto") -> dict:
    classification = classify_query(query)
    if search_mode == "auto":
        mode = {"metrics": "metrics", "logs": "keyword", "knowledge": "semantic", "kubernetes": "keyword", "mixed": "hybrid"}[classification]
    else:
        mode = search_mode
        # An explicit UI mode controls retrieval, but telemetry requests still need Prometheus.
        if classification == "metrics" and mode == "auto":
            mode = "metrics"
    intents = ["KUBERNETES_QUERY"] if classification == "kubernetes" else [{"metrics": "METRIC_QUERY", "keyword": "KEYWORD_SEARCH", "semantic": "SEMANTIC_SEARCH", "hybrid": "HYBRID_SEARCH"}[mode]]
    if re.search(r"\b(why|root\s+cause|rca)\b", query, re.I):
        intents.append("RCA_QUERY")
    sources = {
        "metrics": ["prometheus"],
        "keyword": ["elasticsearch"],
        "semantic": ["qdrant"],
        "hybrid": ["elasticsearch", "qdrant", "prometheus"],
    }[mode]
    if classification == "kubernetes":
        sources = ["kubernetes"]
    elif re.search(r"\b(kubernetes|k8s?)\b", query, re.I) or re.search(
        r"\b(image\s*pull(?:ing)?(?:\s+(?:error|errors|failure|failures|backoff))?|imagepullbackoff|errimagepull|crashloopbackoff|crash\s*loop|oomkilled|oom\s*killed|unschedulable|evicted\s+pods?|pending\s+pods?)\b",
        query,
        re.I,
    ) or (
        re.search(r"\b(pods?|nodes?|namespaces?|deployments?|replicasets?|statefulsets?|daemonsets?|jobs?|cronjobs?|services?|endpoints?|ingresses?|events?)\b", query, re.I)
        and re.search(r"\b(list|show|get|describe|inspect|check|find|status|logs?)\b", query, re.I)
        and "metrics" not in query.lower() and "cpu" not in query.lower() and "memory" not in query.lower()
    ) or (
        re.search(r"\b(pods?|nodes?|namespaces?|deployments?|services?|events?)\b", query, re.I)
        and re.search(r"\b(why|root\s+cause|rca|failed|failing|restarted|crash|not\s+ready)\b", query, re.I)
    ):
        sources = [*sources, "kubernetes"]
    return {"query_classification": classification, "search_mode": mode, "query_intents": intents, "query_sources": sources}


def resolve_environment(query: str, selected: str = "all") -> str:
    if selected != "all":
        return selected
    name = r"(?:stg|prod|hub)"
    names = rf"{name}(?:\s*(?:,|/|\band\b|\bor\b|&|\|)\s*{name})*"
    patterns = (
        rf"\b(?:in|across)\s+(?:the\s+)?({names})(?![\w.-])",
        rf"\benv(?:ironment)?s?\s*(?:=|:|is)?\s*({names})(?![\w.-])",
        rf"(?<![\w.-])({names})\s+env(?:ironment)?s?\b",
    )
    environments = {
        environment.lower()
        for pattern in patterns
        for match in re.finditer(pattern, query, re.I)
        for environment in re.findall(r"\b(?:stg|prod|hub)\b", match[1], re.I)
    }
    return next(iter(environments)) if len(environments) == 1 else selected


def requests_recent_logs(query: str) -> bool:
    return bool(re.search(r"\b(?:latest|newest|recent)\b|\blast\s+\d+\s+(?:logs?|events?|records?)\b", query, re.I))


def parse_time_window(
    query: str = "", start_time: str | None = None, end_time: str | None = None,
    time_range: str | None = None, timezone_name: str = "UTC",
    max_hours: float = 24, now: datetime | None = None,
    default_hours: float = 1,
) -> tuple[str, str]:
    # Relative windows are elapsed UTC durations and must not depend on browser timezone.
    relative_text = (time_range or query or "").lower().strip()
    is_relative = bool(re.search(r"(?:\blast\s+|^)(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d|weeks?|w)\b", relative_text))
    is_calendar = bool(re.search(r"\b(today|yesterday)\b", relative_text))
    needs_zone = bool((start_time or end_time) and time_range == "custom") or is_calendar or (start_time or end_time)
    zone = timezone.utc
    if needs_zone:
        aliases = {"Asia/Calcutta": "Asia/Kolkata", "Asia/Bangalore": "Asia/Kolkata", "Etc/Unknown": "UTC", "Unknown": "UTC"}
        timezone_name = aliases.get(timezone_name, timezone_name)
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            if is_relative and not (start_time or end_time):
                zone = timezone.utc
            else:
                raise ValueError("Unknown timezone")
    anchor = (now or datetime.now(timezone.utc)).astimezone(zone)

    def timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamps must include timezone information")
        return parsed.astimezone(timezone.utc)

    if time_range and time_range.lower().strip() == "custom" and not (start_time and end_time):
        raise ValueError("Custom range requires start_time and end_time")
    if start_time or end_time:
        end = timestamp(end_time) if end_time else anchor
        start = timestamp(start_time) if start_time else end - timedelta(hours=1)
    else:
        text = time_range.lower().strip() if time_range else query.lower()
        duration = re.search(r"(?:\blast\s+|^)(\d+(?:\.\d+)?)\s*(minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d|weeks?|w)\b", text)
        midnight = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        if re.search(r"\byesterday\b", text):
            start, end = midnight - timedelta(days=1), midnight
        elif re.search(r"\btoday\b", text):
            start, end = midnight, anchor
        elif duration:
            unit = duration[2].lower()
            multiplier = 1 / 60 if unit in {"m", "min", "mins", "minute", "minutes"} else 24 if unit in {"d", "day", "days"} else 168 if unit in {"w", "week", "weeks"} else 1
            hours = float(duration[1]) * multiplier
            if not 0 < hours <= max_hours:
                raise ValueError(f"Investigation time window must be positive and at most {max_hours} hours")
            end = anchor.astimezone(timezone.utc)
            start = end - timedelta(hours=hours)
        elif time_range:
            raise ValueError("Use today, yesterday, last X hours/days, or custom")
        else:
            end = anchor.astimezone(timezone.utc)
            start = end - timedelta(hours=default_hours)
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if start >= end:
        raise ValueError("start_time must be before end_time")
    if (end - start).total_seconds() > max_hours * 3600:
        raise ValueError(f"Investigation time window exceeds {max_hours} hours")
    return start.isoformat(), end.isoformat()