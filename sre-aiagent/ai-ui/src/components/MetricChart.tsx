import { useEffect, useMemo, useRef, useState } from "react";
import type { MetricVisualization, MetricSeries } from "../types";

type Props = { visualization: MetricVisualization };

const HEIGHT = 250;
const PAD = { left: 52, right: 18, top: 18, bottom: 32 };
const COLORS = ["#78a9ff", "#68d3ad", "#e4ae6b", "#c69cff", "#ef8ea8", "#6bd0df"];

function formatValue(value: number, unit: string) {
  if (!Number.isFinite(value)) return "-";
  const suffix = unit ? ` ${unit}` : "";
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}k${suffix}`;
  if (Math.abs(value) >= 100) return `${value.toFixed(0)}${suffix}`;
  if (Math.abs(value) >= 10) return `${value.toFixed(1)}${suffix}`;
  return `${value.toFixed(2)}${suffix}`;
}

function formatTime(timestamp: number) {
  const date = new Date(timestamp * 1000);
  return Number.isFinite(date.getTime())
    ? date.toISOString().slice(5, 16).replace("T", " ")
    : "-";
}

export default function MetricChart({ visualization }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(760);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, entry.contentRect.width)));
    observer.observe(container);
    return () => observer.disconnect();
  }, [visualization]);
  const prepared = useMemo(() => {
    const series = (visualization.series ?? []).map((item) => ({
      ...item, points: item.points.filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value))
        .sort((first, second) => first.timestamp - second.timestamp),
    })).filter((item) => item.points.length > 0);
    const timestamps = series.flatMap((item) => item.points.map((point) => point.timestamp));
    const values = series.flatMap((item) => item.points.map((point) => point.value));
    if (!timestamps.length || !values.length) return null;
    const minX = Math.min(...timestamps);
    const maxX = Math.max(...timestamps);
    let minY = Math.min(...values);
    let maxY = Math.max(...values);
    if (minY === maxY) {
      const pad = Math.max(Math.abs(minY) * 0.1, 1);
      minY -= pad;
      maxY += pad;
    } else {
      const pad = (maxY - minY) * 0.08;
      minY -= pad;
      maxY += pad;
    }
    return { series, minX, maxX: maxX === minX ? minX + 1 : maxX, minY, maxY };
  }, [visualization]);

  if (!prepared) return null;
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (value: number) => PAD.left + ((value - prepared.minX) / (prepared.maxX - prepared.minX)) * plotW;
  const y = (value: number) => PAD.top + (1 - (value - prepared.minY) / (prepared.maxY - prepared.minY)) * plotH;

  const yTicks = Array.from({ length: 5 }, (_, index) => {
    const value = prepared.maxY - ((prepared.maxY - prepared.minY) * index) / 4;
    return { value, y: y(value) };
  });

  return (
    <div className="metric-chart" ref={containerRef}>
      <div className="metric-chart-header">
        <div>
          <h4>{visualization.title}</h4>
          <span>{visualization.metric}</span>
          <span>{visualization.unit || "Raw query units"} / UTC{visualization.summarized ? " / Sampled" : ""}</span>
        </div>
        <span className="metric-chart-badge">Prometheus</span>
      </div>
      <svg className="metric-chart-svg" viewBox={`0 0 ${width} ${HEIGHT}`} role="img" aria-label={`${visualization.title} metric chart`}>
        <desc>{prepared.series.length} observed series. Times are UTC. Values are {visualization.unit || "raw query units"}.</desc>
        {yTicks.map((tick) => (
          <g key={tick.value}>
            <line x1={PAD.left} x2={width - PAD.right} y1={tick.y} y2={tick.y} className="metric-grid" />
            <text x={PAD.left - 8} y={tick.y + 3} textAnchor="end" className="metric-axis">{formatValue(tick.value, "")}</text>
          </g>
        ))}
        <line x1={PAD.left} x2={width - PAD.right} y1={HEIGHT - PAD.bottom} y2={HEIGHT - PAD.bottom} className="metric-axis-line" />
        {prepared.series.map((item: MetricSeries, seriesIndex) => {
          const points = item.points.map((point) => `${x(point.timestamp)},${y(point.value)}`).join(" ");
          return <g key={item.name + seriesIndex}><polyline points={points} fill="none" className={`metric-line metric-line-${seriesIndex % 6}`} />{item.points.map((point, index) => <circle key={index} cx={x(point.timestamp)} cy={y(point.value)} r={3} fill={COLORS[seriesIndex % COLORS.length]} className="metric-point"><title>{item.name}: {formatValue(point.value, visualization.unit ?? "")} at {new Date(point.timestamp * 1000).toISOString()}</title></circle>)}</g>;
        })}
        <text x={PAD.left} y={HEIGHT - 10} className="metric-axis">{formatTime(prepared.minX)}</text>
        <text x={width - PAD.right} y={HEIGHT - 10} textAnchor="end" className="metric-axis">{formatTime(prepared.maxX)}</text>
      </svg>
      <div className="metric-chart-legend">
        {prepared.series.map((item: MetricSeries, index) => {
          const latest = item.points[item.points.length - 1]?.value;
          return <span key={item.name + index}><i className={`metric-dot metric-dot-${index % 6}`} />{item.name}<strong>{latest == null ? "-" : formatValue(latest, visualization.unit ?? "")}</strong></span>;
        })}
      </div>
    </div>
  );
}
