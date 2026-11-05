import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, Bot, CheckCircle2,
  ChevronDown, CircleHelp, Clock3, Database, FileSearch, Gauge, History,
  Layers3, LoaderCircle, MessageSquare, Plus, Search, Send, ShieldCheck, Sparkles,
  TerminalSquare, Workflow, XCircle
} from "lucide-react";
import type { ChatMessage, Finding, InvestigationResult, LogRecord, MetricSummaryRow, MetricSummaryValue } from "./types";
import MetricChart from "./components/MetricChart";

const ENVIRONMENTS = ["all", "stg", "prod", "hub"];
const SEARCH_MODES = ["auto", "keyword", "semantic", "hybrid"] as const;
const TIMEZONE_ALIASES: Record<string, string> = {
  "Asia/Calcutta": "Asia/Kolkata",
  "US/Eastern": "America/New_York",
  "US/Central": "America/Chicago",
  "US/Mountain": "America/Denver",
  "US/Pacific": "America/Los_Angeles"
};
const SUGGESTIONS = [
  "Show the latest 10 logs from logstash-aitools-*.",
  "Show me the CPU and memory of the Elasticsearch pod.",
  "Investigate recurring errors in tfs-aiva-stg over the last hour."
];

function id() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function normalizeFindings(value: InvestigationResult["findings"]): Finding[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return { finding: item };
    if (typeof item === "object" && item !== null && !Array.isArray(item)) return item as Finding;
    return { finding: asText(item) || "Unstructured finding" };
  });
}

function canonicalTimezone(value: string | undefined): string {
  if (!value || value === "Unknown" || value === "Etc/Unknown") return "UTC";
  return TIMEZONE_ALIASES[value] ?? value;
}

function confidenceLabel(value?: string) {
  if (!value) return "Not specified";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function responseError(payload: unknown, status: number) {
  if (typeof payload !== "object" || payload === null) return `Backend returned HTTP ${status}`;
  const body = payload as { error?: unknown; detail?: unknown };
  if (typeof body.error === "string") return body.error;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    const messages = body.detail.map((item) =>
      typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : asText(item)
    );
    if (messages.length) return messages.join("; ");
  }
  return `Backend returned HTTP ${status}`;
}

function App() {
  const [environment, setEnvironment] = useState("all");
  const [query, setQuery] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activeInvestigationId, setActiveInvestigationId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [apiStatus, setApiStatus] = useState<"idle" | "connected" | "error">("idle");
  const [showRaw, setShowRaw] = useState(false);
  const [timeRange, setTimeRange] = useState("query");
  const [duration, setDuration] = useState("1");
  const [durationUnit, setDurationUnit] = useState("hours");
  const [searchMode, setSearchMode] = useState<(typeof SEARCH_MODES)[number]>("auto");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [filterError, setFilterError] = useState("");
  const queryInputRef = useRef<HTMLTextAreaElement>(null);
  const localTimezone = canonicalTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone);

  const activeMessages = useMemo(
    () => activeInvestigationId ? messages.filter((message) => message.investigationId === activeInvestigationId) : [],
    [messages, activeInvestigationId]
  );
  const activeMessage = [...activeMessages].reverse().find((message) => message.role === "assistant");
  const result = activeMessage?.result;
  const findings = normalizeFindings(result?.findings);
  const logs = Array.isArray(result?.logs) ? result.logs.filter((log) => log !== null && typeof log === "object" && !Array.isArray(log)) : [];
  const logOnly = logs.length > 0 && (result?.search_mode === "keyword" || result?.query_classification === "logs");
  const logReferences = [...new Set(findings.flatMap((finding) => Array.isArray(finding.evidence) ? finding.evidence.map(asText) : []))];
  const recommendations = Array.isArray(result?.recommendations)
    ? result.recommendations.filter((item): item is string => typeof item === "string") : [];
  const errors = Array.isArray(result?.investigation_errors)
    ? result.investigation_errors.filter((item): item is string => typeof item === "string") : [];
  const alternativeCauses = Array.isArray(result?.alternative_causes)
    ? result.alternative_causes.filter((item): item is string => typeof item === "string") : [];
  const summary = typeof result?.summary === "string" ? result.summary : "No summary was returned.";
  const rootCause = typeof result?.root_cause === "string" ? result.root_cause : "Not determined from available evidence.";
  const evidenceCount = typeof result?.evidence_count === "number"
    ? result.evidence_count
    : Array.isArray(result?.evidence) ? result.evidence.length : 0;

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/readyz", { signal: controller.signal })
      .then((response) => setApiStatus(response.ok ? "connected" : "error"))
      .catch(() => setApiStatus("error"));
    return () => controller.abort();
  }, []);

  async function investigate(text: string) {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setFilterError("");
    const filters: Record<string, string> = {};
    if (searchMode !== "auto") filters.search_mode = searchMode;
    if (timeRange === "today" || timeRange === "yesterday" || timeRange === "custom") {
      filters.timezone = localTimezone;
    }
    if (timeRange === "custom") {
      if (!customStart || !customEnd || new Date(customStart) >= new Date(customEnd)) {
        setFilterError("Choose a custom start before the end.");
        return;
      }
      filters.time_range = "custom";
      filters.start_time = new Date(customStart).toISOString();
      filters.end_time = new Date(customEnd).toISOString();
    } else if (timeRange === "last") {
      if (!Number.isFinite(Number(duration)) || Number(duration) <= 0) {
        setFilterError("Duration must be greater than zero.");
        return;
      }
      const numericDuration = Number(duration);
      const unit = numericDuration === 1 ? durationUnit.replace(/s$/, "") : durationUnit;
      filters.time_range = `last ${duration} ${unit}`;
    } else if (timeRange.startsWith("last_")) {
      const presetRanges: Record<string, string> = {
        last_15m: "last 15 minutes",
        last_1h: "last 1 hour",
        last_6h: "last 6 hours",
        last_12h: "last 12 hours",
        last_24h: "last 24 hours",
        last_3d: "last 3 days",
        last_5d: "last 5 days",
        last_7d: "last 7 days",
        last_30d: "last 30 days"
      };
      filters.time_range = presetRanges[timeRange] ?? "last 1 hour";
    } else if (timeRange !== "query") {
      filters.time_range = timeRange;
    }

    const investigationId = id();
    const userMessage: ChatMessage = {
      id: id(), investigationId, role: "user", query: trimmed, environment, createdAt: new Date()
    };
    setMessages((current) => [...current, userMessage]);
    setActiveInvestigationId(investigationId);
    setQuery("");
    setLoading(true);
    setApiStatus("idle");
    setShowRaw(false);

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 125_000);
    try {
      const response = await fetch("/api/investigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: trimmed, environment, ...filters }),
        signal: controller.signal
      });
      const raw = await response.text();
      let payload: unknown;
      try { payload = raw ? JSON.parse(raw) : {}; } catch { payload = { raw_response: raw }; }

      if (!response.ok) throw new Error(responseError(payload, response.status));

      const investigation = payload as InvestigationResult;
      const assistantMessage: ChatMessage = {
        id: id(), investigationId, role: "assistant", query: trimmed, environment, result: investigation, createdAt: new Date()
      };
      setMessages((current) => [...current, assistantMessage]);
      setApiStatus("connected");
    } catch (error) {
      const assistantMessage: ChatMessage = {
        id: id(), investigationId, role: "assistant", query: trimmed, environment,
        error: error instanceof Error && error.name === "AbortError"
          ? "The request timed out. Try narrowing the query or timeframe."
          : error instanceof Error ? error.message : "The investigation request failed.",
        createdAt: new Date()
      };
      setMessages((current) => [...current, assistantMessage]);
      setApiStatus("error");
    } finally {
      clearTimeout(timeout);
      setLoading(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void investigate(query);
  }

  function newInvestigation() {
    if (loading) return;
    setQuery("");
    setActiveInvestigationId(null);
    setShowRaw(false);
    setTimeRange("query");
    setDuration("1");
    setDurationUnit("hours");
    setSearchMode("auto");
    setCustomStart("");
    setCustomEnd("");
    setFilterError("");
    requestAnimationFrame(() => {
      queryInputRef.current?.focus({ preventScroll: true });
      queryInputRef.current?.scrollIntoView({ block: "center" });
    });
  }

  function selectHistory(message: ChatMessage) {
    setActiveInvestigationId(message.investigationId);
    setShowRaw(false);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Activity size={21} strokeWidth={2.5} /></div>
          <div><div className="brand-name">SRE<span>·</span>AI</div><div className="brand-subtitle">INCIDENT INTELLIGENCE</div></div>
        </div>
        <button className="new-investigation" onClick={newInvestigation} disabled={loading}><Plus size={17} /> New investigation</button>
        <div className="side-section-label"><History size={14} /> RECENT INVESTIGATIONS</div>
        <div className="history-list">
          {messages.filter((m) => m.role === "user").slice().reverse().map((message) => (
            <button className={`history-item ${activeInvestigationId === message.investigationId ? "selected" : ""}`} key={message.investigationId} onClick={() => selectHistory(message)}>
              <MessageSquare size={15} />
              <span>{message.query}</span>
            </button>
          ))}
          {messages.filter((m) => m.role === "user").length === 0 && (
            <p className="empty-history">Your investigations will appear here.</p>
          )}
        </div>
        <div className="sidebar-bottom">
          <div className="connection-card">
            <div className="connection-top"><span className={`status-dot ${apiStatus}`} /> <span>Backend API</span></div>
            <p>{apiStatus === "connected" ? "Backend readiness confirmed" : apiStatus === "error" ? "Backend unavailable or not configured" : "Checking backend readiness…"}</p>
            <div className="connection-foot"><ShieldCheck size={13} /> API key kept server-side</div>
          </div>
          <div className="sidebar-footer"><span>LOCAL DEVELOPMENT</span><span className="version">v1.0.0</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb"><span>Workspace</span><span className="crumb-sep">/</span><strong>Investigation</strong></div>
          <div className="topbar-right">
            <div className="env-picker"><span className="env-label">ENVIRONMENT</span><div className="select-wrap"><select aria-label="Environment" value={environment} onChange={(e) => setEnvironment(e.target.value)}>{ENVIRONMENTS.map((env) => <option key={env} value={env}>{env === "all" ? "All environments" : env.toUpperCase()}</option>)}</select><ChevronDown size={14} /></div></div>
            <div className="avatar">S</div>
          </div>
        </header>

        <div className="page-content">
          <section className="welcome-row">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" /> AI-POWERED OPERATIONS</div>
              <h1>Investigate with <span>clarity.</span></h1>
              <p className="welcome-copy">Correlate metrics, logs, and operational knowledge in one place.</p>
            </div>
            <div className="live-pill"><span className={`live-dot ${apiStatus}`} /> INVESTIGATION ENGINE <span className="live-divider" /> {apiStatus === "connected" ? "READY" : apiStatus === "error" ? "UNAVAILABLE" : "CHECKING"}</div>
          </section>

          <section className="stats-grid" aria-label="Investigation status">
            <StatCard icon={<Workflow size={17} />} label="Data sources" value="4" caption="Prometheus · Elasticsearch · K8s MCP · Qdrant" accent="blue" />
            <StatCard icon={<FileSearch size={17} />} label="Evidence collected" value={result ? String(evidenceCount) : "—"} caption={result ? "From latest investigation" : "Run an investigation to begin"} accent="purple" />
            <StatCard icon={<AlertTriangle size={17} />} label="Investigation errors" value={result ? String(errors.length) : "—"} caption={result ? (errors.length ? "Requires attention" : "No tool errors reported") : "Waiting for first run"} accent={errors.length ? "orange" : "green"} />
            <StatCard icon={<Gauge size={17} />} label="Environment" value={environment === "all" ? "ALL" : environment.toUpperCase()} caption={environment === "all" ? "STG · PROD · HUB" : "Scope for the next query"} accent="cyan" />
          </section>

          <section className="workspace-grid">
            <div className="panel chat-panel">
              <div className="panel-header">
                <div className="panel-heading-icon"><Bot size={18} /></div>
                <div><h2>Investigation copilot</h2><p>Ask about incidents, logs, or service health</p></div>
                <span className="beta-tag"><Sparkles size={12} /> AI COPILOT</span>
              </div>
              <div className="chat-body">
                {activeMessages.length === 0 && !loading ? (
                  <div className="chat-empty">
                    <div className="bot-orbit"><div className="orbit-ring" /><div className="bot-core"><Bot size={29} /></div><span className="orbit-spark spark-one" /><span className="orbit-spark spark-two" /></div>
                    <h3>What are we investigating?</h3>
                    <p>Ask a question in plain language. The agent will gather evidence from connected operational data sources.</p>
                    <div className="suggestion-list">{SUGGESTIONS.map((suggestion, index) => <button key={suggestion} onClick={() => void investigate(suggestion)}><span className="suggestion-icon">{index === 0 ? <Database size={15} /> : index === 1 ? <Activity size={15} /> : <AlertTriangle size={15} />}</span><span>{suggestion}</span><ArrowUpRight size={14} className="suggestion-arrow" /></button>)}</div>
                  </div>
                ) : (
                  <div className="message-stream">
                    {activeMessages.map((message) => (
                      <div key={message.id} className={`message ${message.role}`}>
                        {message.role === "user" ? <div className="message-avatar user-avatar">S</div> : <div className="message-avatar ai-avatar"><Bot size={16} /></div>}
                        <div className="message-content">
                          <div className="message-meta"><strong>{message.role === "user" ? "You" : "SRE AI Agent"}</strong><span>{message.createdAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>{message.environment && <span className="mini-env">{message.environment}</span>}</div>
                          {message.role === "user" ? <p className="user-query">{message.query}</p> : message.error ? <div className="inline-error"><XCircle size={16} /><div><strong>Investigation request failed</strong><p>{message.error}</p><small>Check the proxy configuration and backend logs.</small></div></div> : <div className="assistant-summary">{typeof message.result?.summary === "string" ? message.result.summary : "Investigation completed. Review the findings in the dashboard."}<button className="view-result" onClick={() => selectHistory(message)}>View result <ArrowDownRight size={14} /></button></div>}
                        </div>
                      </div>
                    ))}
                    {loading && <div className="message assistant"><div className="message-avatar ai-avatar"><Bot size={16} /></div><div className="message-content"><div className="message-meta"><strong>SRE AI Agent</strong><span>Working</span></div><div className="thinking"><LoaderCircle size={16} className="spin" /><span>Gathering evidence from your connected sources…</span><span className="thinking-dots">•••</span></div></div></div>}
                    <div className="stream-end" />
                  </div>
                )}
              </div>
              <form className="composer" onSubmit={submit}>
                <div className="composer-top"><span><span className="composer-dot" /> QUERY BUILDER</span><span>Enter to send · Shift + Enter for newline</span></div>
                <fieldset className="query-filters" disabled={loading}>
                  <legend className="sr-only">Investigation filters</legend>
                  <label>Environment<select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option value="all">All environments</option><option value="stg">Staging</option><option value="prod">Production</option><option value="hub">Hub</option></select></label>
                  <label className="time-range-filter">Time range<select value={timeRange} onChange={(event) => setTimeRange(event.target.value)}><option value="query">From query (agent detects)</option><option value="last_15m">Last 15 minutes</option><option value="last_1h">Last 1 hour</option><option value="last_6h">Last 6 hours</option><option value="last_12h">Last 12 hours</option><option value="last_24h">Last 24 hours</option><option value="last_3d">Last 3 days</option><option value="last_5d">Last 5 days</option><option value="last_7d">Last 7 days</option><option value="last_30d">Last 30 days</option><option value="today">Today</option><option value="yesterday">Yesterday</option><option value="last">Last...</option><option value="custom">Custom</option></select></label>
                  {timeRange === "last" && <div className="duration-filter"><label>Duration<input aria-label="Duration" type="number" min="1" max={durationUnit === "minutes" ? "43200" : durationUnit === "hours" ? "720" : "30"} step="1" value={duration} onChange={(event) => setDuration(event.target.value)} /></label><label>Unit<select value={durationUnit} onChange={(event) => setDurationUnit(event.target.value)}><option value="minutes">Minutes</option><option value="hours">Hours</option><option value="days">Days</option></select></label></div>}
                  {timeRange.startsWith("last_") && <span className="filter-hint">{timeRange === "last_15m" ? "15 minutes" : timeRange === "last_1h" ? "1 hour" : timeRange === "last_6h" ? "6 hours" : timeRange === "last_12h" ? "12 hours" : timeRange === "last_24h" ? "24 hours" : timeRange === "last_3d" ? "3 days" : timeRange === "last_5d" ? "5 days" : timeRange === "last_7d" ? "7 days" : "30 days"} of data will be requested.</span>}
                  <label className="search-mode-filter"><span><Search size={12} /> Search mode</span><select value={searchMode} onChange={(event) => setSearchMode(event.target.value as (typeof SEARCH_MODES)[number])}><option value="auto">Auto</option><option value="keyword">Keyword</option><option value="semantic">Semantic</option><option value="hybrid">Hybrid</option></select></label>
                  {timeRange === "custom" && <><label>Start ({localTimezone})<input type="datetime-local" required value={customStart} onChange={(event) => setCustomStart(event.target.value)} /></label><label>End ({localTimezone})<input type="datetime-local" required value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label></>}
                </fieldset>
                {filterError && <p className="filter-error" role="alert">{filterError}</p>}
                <textarea ref={queryInputRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void investigate(query); } }} placeholder="Ask about logs, metrics, incidents, or service health…" rows={2} disabled={loading} />
                <div className="composer-bottom"><span className="composer-hint"><CircleHelp size={13} /> Be specific about service, timeframe, or index</span><button className="send-button" type="submit" aria-label={loading ? "Investigating" : "Investigate"} disabled={!query.trim() || loading}>{loading ? <LoaderCircle size={16} className="spin" /> : <Send size={15} />} <span>{loading ? "Investigating" : "Investigate"}</span></button></div>
              </form>
            </div>

            <div className="panel results-panel">
              <div className="panel-header results-header">
                <div className="panel-heading-icon result-icon"><Layers3 size={18} /></div>
                <div><h2>{logOnly ? "Log results" : "Investigation results"}</h2><p>{logOnly ? "Elasticsearch" : "Structured findings and evidence"}</p></div>
                {result && <span className={`result-status ${errors.length ? "warning" : "success"}`}>{errors.length ? "PARTIAL" : "COMPLETE"}</span>}
              </div>
              {!activeMessage || activeMessage.role === "user" ? (
                <div className="results-empty"><div className="empty-illustration"><TerminalSquare size={27} /><span><Activity size={15} /></span></div><h3>Results will appear here</h3><p>Submit an investigation to see the summary, root cause, findings, and recommended next steps.</p><div className="empty-checks"><div><CheckCircle2 size={14} /> Metrics and logs</div><div><CheckCircle2 size={14} /> Evidence-backed findings</div><div><CheckCircle2 size={14} /> Actionable recommendations</div></div></div>
              ) : activeMessage.error ? (
                <div className="results-error"><AlertTriangle size={21} /><h3>Could not complete request</h3><p>{activeMessage.error}</p></div>
              ) : result ? (
                <div className="results-scroll">
                  {result.time_range && (
                    <div className="result-section coverage-section">
                      <div className="section-title"><span className="section-icon blue"><Clock3 size={14} /></span><h3>Time range & coverage</h3></div>
                      <p className="coverage-window"><time dateTime={result.time_range.start_time}>{new Date(result.time_range.start_time).toISOString()}</time><span>to</span><time dateTime={result.time_range.end_time}>{new Date(result.time_range.end_time).toISOString()}</time></p>
                      <p className="muted-copy">{result.time_range.timezone}</p>
                      <dl className="coverage-list">
                        {Object.entries(result.data_coverage ?? {}).map(([source, coverage]) => (
                          <div key={source}>
                            <dt>{source}</dt>
                            <dd>
                              <strong>{coverage.status.replace(/_/g, " ")}</strong>
                              <span>{coverage.returned_count} returned{coverage.scope === "static_knowledge" ? " · static knowledge" : coverage.scope === "current_cluster" ? " · current cluster only" : ""}</span>
                              {coverage.observed_start && coverage.observed_end && <span>{coverage.observed_start} to {coverage.observed_end}</span>}
                              {coverage.queried_ranges?.map((range, index) => <span key={index}>Queried: {range.start_time} to {range.end_time}</span>)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    </div>
                  )}
                  <div className="result-section">
                    <div className="section-title"><span className="section-icon purple"><Sparkles size={14} /></span><h3>Executive summary</h3></div>
                    <p className="summary-text">{summary}</p>
                  </div>
                  {!logOnly && <><div className="mini-metrics">
                    <div><span className="mini-metric-label">EVIDENCE</span><strong>{evidenceCount}</strong><small>items collected</small></div>
                    <div><span className="mini-metric-label">CONFIDENCE</span><strong className="confidence-value">{confidenceLabel(result.confidence)}</strong><small>agent assessment</small></div>
                  </div>
                  <div className="result-section">
                    <div className="section-title"><span className="section-icon orange"><AlertTriangle size={14} /></span><h3>Root cause</h3></div>
                    <div className={`root-cause ${rootCause.toLowerCase() !== "not determined" ? "has-cause" : ""}`}>{rootCause}</div>
                  </div></>}
                  {logs.length > 0 && <div className="result-section log-section">
                    <div className="section-title"><span className="section-icon blue"><TerminalSquare size={14} /></span><h3>Logs <span className="count-badge">{logs.length}</span></h3></div>
                    <LogList logs={logs} />
                    {logReferences.length > 0 && <div className="log-sources"><span>Sources</span>{logReferences.map((reference) => <code key={reference}>{reference}</code>)}</div>}
                  </div>}
                  {result.visualizations && result.visualizations.length > 0 && (
                    <div className="result-section visualization-section">
                      <div className="section-title"><span className="section-icon blue"><Activity size={14} /></span><h3>Metrics <span className="count-badge">{result.visualizations.length}</span></h3></div>
                      <div className="metric-chart-list">
                        {result.visualizations.map((visualization) => <MetricChart key={visualization.id} visualization={visualization} />)}
                      </div>
                    </div>
                  )}
                  {result.metric_summary && result.metric_summary.length > 0 && (
                    <div className="result-section metric-summary-section">
                      <div className="section-title"><span className="section-icon green"><Activity size={14} /></span><h3>Node summary <span className="count-badge">{result.metric_summary.length}</span></h3></div>
                      <p className="muted-copy metric-summary-caption">Latest observed values with averages, peaks, and sample counts for every returned node.</p>
                      <MetricSummaryTable rows={result.metric_summary} />
                    </div>
                  )}
                  {!logOnly && <div className="result-section">
                    <div className="section-title"><span className="section-icon blue"><FileSearch size={14} /></span><h3>Findings <span className="count-badge">{findings.length}</span></h3></div>
                    {findings.length ? <div className="finding-list">{findings.map((finding, index) => <div className="finding-item" key={index}><span className="finding-number">{String(index + 1).padStart(2, "0")}</span><div><p>{finding.finding || finding.title || finding.description || asText(finding)}</p>{finding.confidence && <span className="finding-confidence">Confidence: {finding.confidence}</span>}{Array.isArray(finding.evidence) && finding.evidence.length > 0 && <div className="evidence-tags">{finding.evidence.slice(0, 3).map((item, i) => <span key={i}>{asText(item)}</span>)}</div>}</div></div>)}</div> : <p className="muted-copy">No structured findings were returned.</p>}
                  </div>}
                  {(!logOnly || recommendations.length > 0) && <div className="result-section">
                    <div className="section-title"><span className="section-icon green"><CheckCircle2 size={14} /></span><h3>Recommendations <span className="count-badge">{recommendations.length}</span></h3></div>
                    {recommendations.length ? <ul className="recommendation-list">{recommendations.map((item, index) => <li key={index}><span className="recommendation-check"><CheckCircle2 size={14} /></span><span>{item}</span></li>)}</ul> : <p className="muted-copy">No recommendations returned.</p>}
                  </div>}
                  {alternativeCauses.length > 0 && <div className="result-section"><div className="section-title"><span className="section-icon orange"><CircleHelp size={14} /></span><h3>Alternative causes</h3></div><ul className="recommendation-list">{alternativeCauses.map((item, i) => <li key={i}><span className="recommendation-check"><ArrowUpRight size={14} /></span><span>{item}</span></li>)}</ul></div>}
                  {errors.length > 0 && <div className="error-list-box"><div className="section-title"><span className="section-icon orange"><AlertTriangle size={14} /></span><h3>Investigation errors <span className="count-badge error-count">{errors.length}</span></h3></div>{errors.map((error, i) => <p key={i}>{error}</p>)}</div>}
                  <button className="raw-toggle" onClick={() => setShowRaw((value) => !value)}><TerminalSquare size={14} /> {showRaw ? "Hide raw response" : "Inspect raw response"} <ChevronDown size={14} className={showRaw ? "rotate" : ""} /></button>
                  {showRaw && <pre className="raw-json">{JSON.stringify(result, null, 2)}</pre>}
                  <div className="result-footer"><ShieldCheck size={13} /> Generated from the evidence returned by the backend. Validate before taking production action.</div>
                </div>
              ) : null}
            </div>
          </section>

          <footer className="page-footer"><div><span className="footer-pulse" /> {apiStatus === "connected" ? "SYSTEM READY" : apiStatus === "error" ? "SYSTEM DEGRADED" : "CHECKING SYSTEM"} <span className="footer-separator">·</span> {apiStatus === "connected" ? "BACKEND CONNECTED" : "BACKEND STATUS"}</div><div><Clock3 size={13} /> {new Date().toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" })}<span className="footer-separator">·</span><span>Evidence-driven operations</span></div></footer>
        </div>
      </main>
    </div>
  );
}

function LogList({ logs }: { logs: LogRecord[] }) {
  return <ol className="log-list">{logs.map((log, index) => {
    const timestamp = log.timestamp ? new Date(log.timestamp) : null;
    const timeLabel = timestamp && Number.isFinite(timestamp.getTime()) ? timestamp.toISOString().replace("T", " ").replace("Z", " UTC") : log.timestamp || "Timestamp unavailable";
    const level = log.level?.toUpperCase();
    let message = asText(log.message);
    if (typeof log.message === "string") {
      try {
        const parsed: unknown = JSON.parse(log.message);
        if (typeof parsed === "object" && parsed !== null) message = JSON.stringify(parsed, null, 2);
      } catch { message = log.message; }
    }
    return <li className="log-entry" key={`${log.timestamp ?? "log"}-${index}`}>
      <div className="log-entry-header"><time dateTime={log.timestamp || undefined}>{timeLabel}</time>{level && <span className="log-level" data-level={level}>{level}</span>}{log.status != null && log.status !== "" && <span className="log-status">Status {log.status}</span>}</div>
      <pre className="log-message">{message || "Message unavailable"}</pre>
      {(log.environment || log.component) && <div className="log-identity">{log.environment && <span>{log.environment.toUpperCase()}</span>}{log.component && <span>{log.component}</span>}</div>}
      <details className="log-details"><summary>Details</summary><pre>{JSON.stringify(log, null, 2)}</pre></details>
    </li>;
  })}</ol>;
}

  function MetricSummaryTable({ rows }: { rows: MetricSummaryRow[] }) {
    const format = (value: number | undefined, unit: string) => value == null || !Number.isFinite(value)
      ? "-"
      : `${new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 }).format(value)} ${unit}`;
    const renderMetric = (metric: MetricSummaryValue | null) => metric ? <>
      <span className="metric-cell-latest">{format(metric.latest, metric.unit)}</span>
      <span>Avg {format(metric.average, metric.unit)}</span>
      <span>Min {format(metric.minimum, metric.unit)}</span>
      <span>Peak {format(metric.peak, metric.unit)}</span>
      <span>{metric.samples} samples{metric.sampled ? " · sampled" : ""}</span>
    </> : <span className="metric-cell-missing">No data</span>;
    return <div className="metric-summary-scroll" role="region" aria-label="Per-node CPU and memory summary" tabIndex={0}>
      <table className="metric-summary-table">
        <thead><tr><th scope="col">Environment</th><th scope="col">Namespace</th><th scope="col">Node / pod</th><th scope="col">CPU</th><th scope="col">Memory</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={`${row.environment}/${row.namespace}/${row.pod}`}>
          <td><span className={`metric-env-label metric-env-${row.environment}`}>{row.environment.toUpperCase()}</span></td>
          <td className="metric-node-label">{row.namespace || "-"}</td>
          <td className="metric-node-label">{row.pod}</td>
          <td className="metric-value-cell">{renderMetric(row.cpu)}</td>
          <td className="metric-value-cell">{renderMetric(row.memory)}</td>
        </tr>)}</tbody>
      </table>
    </div>;
  }

function StatCard({ icon, label, value, caption, accent }: { icon: React.ReactNode; label: string; value: string; caption: string; accent: string }) {
  return <div className="stat-card"><div className={`stat-icon ${accent}`}>{icon}</div><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className="stat-caption">{caption}</div></div>;
}

export default App;
