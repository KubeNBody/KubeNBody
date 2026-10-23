// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Table2 } from 'lucide-react';
import * as d3 from '@/lib/d3';
import { formatLogTick, formatValue } from '@/lib/chart-format';
import { CHART } from '@/lib/theme/chart';
import { cn } from '@/lib/utils';

export interface LineSeries {
  id: string;
  label: string;
  color: string;
  dash?: string;
  /** Emphasised (thicker), e.g. the reference job */
  primary?: boolean;
  x: number[];
  y: number[];
}

export interface LineChartProps {
  title: string;
  /** One sentence for screen readers: what the chart shows */
  description: string;
  series: LineSeries[];
  xLabel: string;
  yLabel: string;
  /** 'log' drops non-positive values (they have no place on a log axis) */
  yScale?: 'linear' | 'log';
  height?: number;
  className?: string;
}

const MARGIN = { top: 12, right: 16, bottom: 40, left: 64 };

/**
 * Width of the element, following resizes (charts then re-layout instead of
 * scaling). A callback ref: the plot's element unmounts while the table or the
 * empty state shows, and the observer moves to each new element.
 */
function useWidth<T extends HTMLElement>() {
  const observer = useRef<ResizeObserver | null>(null);
  const [width, setWidth] = useState(0);
  const ref = useCallback((el: T | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    observer.current = ro;
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, width] as const;
}

/**
 * The dashboard's line chart (energy, drift, RMSE, momentum): one y-axis,
 * legend above the plot, a crosshair with every series' value at the hovered
 * x, and a table view of each series' first/last/min/max.
 */
export function LineChart({ title, description, series, xLabel, yLabel, yScale = 'linear', height = 280, className }: LineChartProps) {
  const [boxRef, width] = useWidth<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const titleId = useId();

  // Points per series, cleaned for the scale
  const data = useMemo(
    () =>
      series.map((s) => ({
        ...s,
        points: s.x
          .map((x, i) => ({ x, y: s.y[i] }))
          .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y) && (yScale !== 'log' || p.y > 0)),
      })),
    [series, yScale]
  );

  const innerW = Math.max(0, width - MARGIN.left - MARGIN.right);
  const innerH = height - MARGIN.top - MARGIN.bottom;

  const scales = useMemo(() => {
    const xs = data.flatMap((s) => s.points.map((p) => p.x));
    const ys = data.flatMap((s) => s.points.map((p) => p.y));
    if (!xs.length || innerW <= 0) return null;
    // Loops, not Math.min(...ys): spreading a long series overflows the stack
    let yMin = Infinity;
    let yMax = -Infinity;
    for (const y of ys) {
      if (y < yMin) yMin = y;
      if (y > yMax) yMax = y;
    }
    const x = d3.scaleLinear().domain(d3.extent(xs) as [number, number]).range([0, innerW]);
    const y =
      yScale === 'log'
        ? d3.scaleLog().domain([yMin, yMax === yMin ? yMin * 10 : yMax]).range([innerH, 0]).nice()
        : d3.scaleLinear().domain(yMin === yMax ? [yMin - 1, yMax + 1] : [yMin, yMax]).range([innerH, 0]).nice();
    return { x, y };
  }, [data, innerW, innerH, yScale]);

  // Axes and grid (d3 owns only these groups, React draws the lines)
  useEffect(() => {
    if (!svgRef.current || !scales) return;
    const svg = d3.select(svgRef.current);
    const yTicks = yScale === 'log' ? undefined : 5;
    // Log axes: ticks and gridlines at the powers of ten only (every minor tick
    // made a dense grid). A log axis spanning less than a decade keeps the defaults
    const [y0, y1] = scales.y.domain();
    const decades: number[] = [];
    if (yScale === 'log') for (let e = Math.ceil(Math.log10(y0)); e <= Math.floor(Math.log10(y1)); e++) decades.push(10 ** e);
    // At most one labelled decade per ~28 px (16 decades on a phone otherwise overlap)
    const every = Math.max(1, Math.ceil(decades.length / Math.max(3, Math.floor(innerH / 28))));
    const logTicks = decades.length >= 2 ? decades.filter((_, i) => (decades.length - 1 - i) % every === 0) : null;
    const yFormat = (v: d3.NumberValue) => (yScale === 'log' && logTicks ? formatLogTick(Number(v)) : formatValue(Number(v)));
    svg
      .select<SVGGElement>('.x-axis')
      .call(d3.axisBottom(scales.x).ticks(Math.max(2, Math.floor(innerW / 90))).tickFormat((v) => formatValue(Number(v))).tickSizeOuter(0))
      .call((g) => g.selectAll('text').attr('fill', CHART.axisText).attr('font-size', 11))
      .call((g) => g.selectAll('line,path').attr('stroke', CHART.axisLine));
    const yAxis = () => {
      const axis = d3.axisLeft(scales.y as d3.ScaleLinear<number, number>).ticks(yTicks ?? 4);
      return logTicks ? axis.tickValues(logTicks) : axis;
    };
    svg
      .select<SVGGElement>('.y-axis')
      .call(yAxis().tickFormat(yFormat).tickSizeOuter(0))
      .call((g) => g.selectAll('text').attr('fill', CHART.axisText).attr('font-size', 11))
      .call((g) => g.selectAll('line,path').attr('stroke', CHART.axisLine));
    svg
      .select<SVGGElement>('.grid')
      .call(yAxis().tickSize(-innerW).tickFormat(() => ''))
      .call((g) => g.select('.domain').remove())
      .call((g) => g.selectAll('line').attr('stroke', CHART.grid));
  }, [scales, innerW, innerH, yScale]);

  const lines = useMemo(() => {
    if (!scales) return [];
    const gen = d3
      .line<{ x: number; y: number }>()
      .x((p) => scales.x(p.x))
      .y((p) => scales.y(p.y));
    return data.map((s) => ({ ...s, d: gen(s.points) ?? '' }));
  }, [data, scales]);

  // Hover: the nearest sample of each series at the pointer's x
  const hover = useMemo(() => {
    if (hoverX === null || !scales) return null;
    const bisect = d3.bisector((p: { x: number }) => p.x).center;
    const rows = data
      .map((s) => {
        if (!s.points.length) return null;
        const p = s.points[bisect(s.points, hoverX)];
        return { id: s.id, label: s.label, color: s.color, dash: s.dash, x: p.x, y: p.y };
      })
      .filter(Boolean) as { id: string; label: string; color: string; dash?: string; x: number; y: number }[];
    return rows.length ? { x: rows[0].x, rows } : null;
  }, [hoverX, data, scales]);

  const empty = data.every((s) => s.points.length === 0);

  return (
    <figure className={cn('rounded-lg border border-border bg-surface p-4', className)} aria-labelledby={titleId}>
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <figcaption id={titleId} className="text-sm font-medium text-fg">
          {title}
          {yScale === 'log' && <span className="ml-2 text-xs font-normal text-fg-subtle">log scale</span>}
        </figcaption>
        <button
          type="button"
          onClick={() => setShowTable((t) => !t)}
          aria-pressed={showTable}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-fg-muted hover:bg-surface-raised hover:text-fg"
        >
          <Table2 className="size-3.5" aria-hidden /> {showTable ? 'Chart' : 'Table'}
        </button>
      </div>

      {/* Legend (always, for 2+ series): identity is never colour alone */}
      {series.length > 1 && (
        <ul className="mb-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fg-muted">
          {series.map((s) => (
            <li key={s.id} className="flex items-center gap-1.5">
              <svg width="18" height="6" aria-hidden>
                <line x1="0" x2="18" y1="3" y2="3" stroke={s.color} strokeWidth={s.primary ? 3 : 2} strokeDasharray={s.dash} />
              </svg>
              {s.label}
            </li>
          ))}
        </ul>
      )}

      {showTable ? (
        <SeriesTable series={data} />
      ) : empty ? (
        <p className="py-12 text-center text-sm text-fg-muted">No data{yScale === 'log' ? ' above zero to plot on a log scale' : ''}.</p>
      ) : (
        <div ref={boxRef} className="relative">
          <svg ref={svgRef} width={width} height={height} role="img" aria-label={`${title}. ${description}`} className="block overflow-visible">
            <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
              <g className="grid" />
              <g className="x-axis" transform={`translate(0,${innerH})`} />
              <g className="y-axis" />
              <text x={innerW / 2} y={innerH + 34} textAnchor="middle" fill={CHART.axisText} fontSize={11}>
                {xLabel}
              </text>
              <text transform="rotate(-90)" x={-innerH / 2} y={-50} textAnchor="middle" fill={CHART.axisText} fontSize={11}>
                {yLabel}
              </text>
              {lines.map((l) => (
                <path
                  key={l.id}
                  d={l.d}
                  fill="none"
                  stroke={l.color}
                  strokeWidth={l.primary ? 2.5 : 2}
                  strokeDasharray={l.dash}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              ))}
              {hover && scales && (
                <g pointerEvents="none">
                  <line x1={scales.x(hover.x)} x2={scales.x(hover.x)} y1={0} y2={innerH} stroke={CHART.crosshair} />
                  {hover.rows.map((r) => (
                    <circle key={r.id} cx={scales.x(r.x)} cy={scales.y(r.y)} r={4} fill={r.color} stroke={CHART.surface} strokeWidth={2} />
                  ))}
                </g>
              )}
              {/* Hit area for the crosshair */}
              <rect
                width={innerW}
                height={innerH}
                fill="transparent"
                onPointerMove={(e) => scales && setHoverX(scales.x.invert(d3.pointer(e)[0]))}
                onPointerLeave={() => setHoverX(null)}
              />
            </g>
          </svg>
          {hover && scales && (
            <div
              className="pointer-events-none absolute top-2 z-10 min-w-40 rounded-md border border-border-strong bg-surface-raised px-3 py-2 text-xs shadow-lg"
              style={
                scales.x(hover.x) + MARGIN.left > width / 2
                  ? { right: width - (scales.x(hover.x) + MARGIN.left) + 12 }
                  : { left: scales.x(hover.x) + MARGIN.left + 12 }
              }
            >
              <p className="mb-1 text-fg-muted">
                {xLabel}: <span className="tabular-nums text-fg">{formatValue(hover.x, 4)}</span>
              </p>
              {hover.rows.map((r) => (
                <p key={r.id} className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-1.5 text-fg-muted">
                    <svg width="12" height="4" aria-hidden>
                      <line x1="0" x2="12" y1="2" y2="2" stroke={r.color} strokeWidth={2} strokeDasharray={r.dash} />
                    </svg>
                    {r.label}
                  </span>
                  <span className="tabular-nums text-fg">{formatValue(r.y, 4)}</span>
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </figure>
  );
}

function SeriesTable({ series }: { series: { id: string; label: string; points: { x: number; y: number }[] }[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-fg-muted">
            <th className="py-1.5 pr-3 font-medium">Series</th>
            <th className="py-1.5 pr-3 text-right font-medium">Points</th>
            <th className="py-1.5 pr-3 text-right font-medium">First</th>
            <th className="py-1.5 pr-3 text-right font-medium">Last</th>
            <th className="py-1.5 pr-3 text-right font-medium">Min</th>
            <th className="py-1.5 text-right font-medium">Max</th>
          </tr>
        </thead>
        <tbody className="tabular-nums text-fg">
          {series.map((s) => {
            let min = Infinity;
            let max = -Infinity;
            for (const p of s.points) {
              if (p.y < min) min = p.y;
              if (p.y > max) max = p.y;
            }
            return (
              <tr key={s.id} className="border-t border-border">
                <td className="py-1.5 pr-3 text-fg-muted">{s.label}</td>
                <td className="py-1.5 pr-3 text-right">{s.points.length}</td>
                <td className="py-1.5 pr-3 text-right">{formatValue(s.points[0]?.y ?? NaN, 4)}</td>
                <td className="py-1.5 pr-3 text-right">{formatValue(s.points.at(-1)?.y ?? NaN, 4)}</td>
                <td className="py-1.5 pr-3 text-right">{formatValue(min, 4)}</td>
                <td className="py-1.5 text-right">{formatValue(max, 4)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
