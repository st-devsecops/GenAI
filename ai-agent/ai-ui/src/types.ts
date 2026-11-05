export type Finding = {
  finding?: string;
  title?: string;
  description?: string;
  evidence?: unknown[];
  confidence?: string;
  severity?: string;
};

export type LogRecord = {
  timestamp?: string | null;
  level?: string | null;
  message?: unknown;
  status?: string | number | null;
  environment?: string | null;
  component?: string | null;
  pod_name?: string | null;
  namespace_name?: string | null;
  index?: string | null;
  [key: string]: unknown;
};

export type InvestigationResult = {
  logs?: LogRecord[];
  time_range?: { start_time: string; end_time: string; timezone: string };
  search_mode?: string;
  query_classification?: string;
  query_intents?: string[];
  query_sources?: string[];
  data_coverage?: Record<string, {
    status: string;
    returned_count: number;
    observed_start?: string | null;
    observed_end?: string | null;
    scope: "event_time" | "static_knowledge" | "current_cluster";
    queried_ranges?: { start_time: string; end_time: string; timezone: string }[];
  }>;
  summary?: string;
  findings?: Finding[] | string[];
  root_cause?: string | null;
  confidence?: string;
  recommendations?: string[];
  alternative_causes?: string[];
  evidence_count?: number;
  investigation_errors?: string[];
  visualizations?: MetricVisualization[];
  metric_summary?: MetricSummaryRow[];
  evidence?: unknown[];
  [key: string]: unknown;
};

export type ChatMessage = {
  id: string;
  investigationId: string;
  role: "user" | "assistant";
  query?: string;
  environment?: string;
  result?: InvestigationResult;
  error?: string;
  createdAt: Date;
};


export type MetricPoint = { timestamp: number; value: number };
export type MetricSeries = { name: string; labels?: Record<string, string>; points: MetricPoint[] };
export type MetricVisualization = {
  id: string;
  type: "line";
  title: string;
  metric: string;
  unit?: string;
  start_time?: string;
  end_time?: string;
  summarized?: boolean;
  series: MetricSeries[];
};

export type MetricSummaryValue = {
  latest: number;
  average: number;
  minimum: number;
  peak: number;
  samples: number;
  unit: string;
  sampled: boolean;
};

export type MetricSummaryRow = {
  environment: string;
  namespace: string;
  pod: string;
  cpu: MetricSummaryValue | null;
  memory: MetricSummaryValue | null;
};
