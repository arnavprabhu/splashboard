import { useMemo, useRef, useState } from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import { api } from "../api/client";
import type { UsageFacets, UsageRow, UsageRows, UsageSummary, UsageTimeseries } from "../api/models";
import {
  Button,
  Chart,
  Empty,
  LoadError,
  Loading,
  NumbersBand,
  PageHeader,
  Section,
  Select,
  Stat,
  SubNav,
  Table,
  Tag,
  type Column,
} from "../components";
import { useApi } from "../lib/use-api";
import { DASH, formatCompact, formatCount, formatMs, formatRelativeTime } from "../lib/format";
import { engine } from "../store";
import { t } from "../strings/usage";
import { useTitle } from "../lib/title";
import { STATUS_TABS } from "./tabs";
import {
  FILTER_KEYS,
  HISTORY_PAGE_SIZE,
  USAGE_ENDPOINTS,
  USAGE_STATUS,
  apiFilters,
  clientRows,
  defaultRange,
  exportHref,
  heatMax,
  heatOpacity,
  pageRange,
  injectedCount,
  isFiltered,
  readFilters,
  requestsOverTime,
  stackTokensPerDay,
  type ClientRow,
  type HistoryFilters,
} from "./status/history";

const isEmptyRange = (s: UsageSummary | null) => !!s && s.requests === 0;

export default function History() {
  useTitle(t("usage.history.page_title"));
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const key = FILTER_KEYS.map((k) => filters[k]).join("|");
  const query = apiFilters(filters);

  const setFilter = (name: keyof HistoryFilters, value: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: true },
    );
  const reset = () =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const k of FILTER_KEYS) next.delete(k);
        return next;
      },
      { replace: true },
    );

  // Totals honour every filter; the option lists only the date range.
  const summary = useApi(
    (s) => api.get<UsageSummary>("/usage/summary", { scope: "all", ...query }, s),
    [key],
  );
  const facets = useApi(
    (s) => api.get<UsageFacets>("/usage/facets", { start: query.start, end: query.end }, s),
    [filters.from, filters.to],
  );
  const series = useApi(
    (s) =>
      api.get<UsageTimeseries>(
        "/usage/timeseries",
        { ...query, bucket: "day", group_by: "model", view: "heatmap" },
        s,
      ),
    [key],
  );

  // 0-based page in state, reset whenever a filter changes; the manager's `page` is 1-based.
  const [paging, setPaging] = useState<{ key: string; page: number }>({ key, page: 0 });
  const page = paging.key === key ? paging.page : 0;
  const setPage = (next: number) => setPaging({ key, page: next });
  const rows = useApi(
    (s) =>
      api.get<UsageRows>(
        "/usage/requests",
        { ...query, limit: HISTORY_PAGE_SIZE, page: page + 1 },
        s,
      ),
    [key, page],
  );

  const filtered = isFiltered(filters);
  const nothingAtAll = !filtered && isEmptyRange(summary.data) && (rows.data?.rows.length ?? 0) === 0;

  return (
    <>
      <SubNav items={STATUS_TABS} label="Status" exact />
      <PageHeader title={t("usage.history.title")} />
      <Filters
        filters={filters}
        models={facets.data?.models ?? []}
        endpoints={facets.data?.endpoints ?? []}
        clients={facets.data?.clients ?? []}
        statuses={facets.data?.statuses ?? []}
        error={facets.error}
        onRetry={facets.reload}
        filtered={filtered}
        onChange={setFilter}
        onReset={reset}
      />
      {nothingAtAll ? (
        <Section>
          <Empty
            title={t("usage.history.empty")}
            action={
              <Link href="/chat" class="btn" data-variant="outline">
                {t("usage.history.open_chat")}
              </Link>
            }
          >
            {t("usage.history.empty_body")}
          </Empty>
        </Section>
      ) : (
        <>
          <Totals summary={summary.data} error={summary.error} onRetry={summary.reload} />
          <TokensPerDay data={series.data} error={series.error} onRetry={series.reload} />
          <Heatmap data={series.data} error={series.error} onRetry={series.reload} />
          <RequestsOverTime data={series.data} error={series.error} onRetry={series.reload} />
          <TopClients summary={summary.data} error={summary.error} onRetry={summary.reload} />
          <RequestLog
            rows={rows.data?.rows ?? null}
            loading={rows.loading}
            error={rows.error}
            onRetry={rows.reload}
            page={page}
            total={rows.data?.total ?? null}
            filtered={filtered}
            onOlder={() => setPage(page + 1)}
            onNewer={() => setPage(Math.max(0, page - 1))}
            onReset={reset}
          />
        </>
      )}
    </>
  );
}

// ---------- Filters ----------

function Filters({
  filters,
  models,
  endpoints,
  clients,
  statuses,
  error,
  onRetry,
  filtered,
  onChange,
  onReset,
}: {
  filters: HistoryFilters;
  models: string[];
  endpoints: string[];
  clients: string[];
  statuses: string[];
  error: unknown;
  onRetry: () => void;
  filtered: boolean;
  onChange: (name: keyof HistoryFilters, value: string) => void;
  onReset: () => void;
}) {
  const all = { value: "", label: t("usage.history.all") };
  const withCurrent = (list: string[], current: string) =>
    current && !list.includes(current) ? [current, ...list] : list;
  const range = defaultRange();
  return (
    <section class="band history-filters" aria-label={t("usage.history.filters")}>
      <div class="cluster history-filter-row">
        <label class="history-filter">
          <span class="label">{t("usage.history.from")}</span>
          <input
            type="date"
            class="input"
            value={filters.from}
            max={filters.to}
            onChange={(e) => onChange("from", e.currentTarget.value === range.from ? "" : e.currentTarget.value)}
          />
        </label>
        <label class="history-filter">
          <span class="label">{t("usage.history.to")}</span>
          <input
            type="date"
            class="input"
            value={filters.to}
            min={filters.from}
            onChange={(e) => onChange("to", e.currentTarget.value === range.to ? "" : e.currentTarget.value)}
          />
        </label>
        <label class="history-filter">
          <span class="label">{t("usage.history.model")}</span>
          <Select
            value={filters.model}
            options={[all, ...withCurrent(models, filters.model).map((m) => ({ value: m, label: m }))]}
            onChange={(v) => onChange("model", v)}
          />
        </label>
        <label class="history-filter">
          <span class="label">{t("usage.history.endpoint")}</span>
          <Select
            value={filters.endpoint}
            options={[
              all,
              ...withCurrent(endpoints.length ? endpoints : [...USAGE_ENDPOINTS], filters.endpoint).map((e) => ({ value: e, label: e })),
            ]}
            onChange={(v) => onChange("endpoint", v)}
          />
        </label>
        <label class="history-filter">
          <span class="label">{t("usage.history.status")}</span>
          <Select
            value={filters.status}
            options={[
              all,
              ...withCurrent(statuses.length ? statuses : [...USAGE_STATUS], filters.status).map((s) => ({
                value: s,
                label: (USAGE_STATUS as readonly string[]).includes(s)
                  ? t(`usage.history.st.${s as (typeof USAGE_STATUS)[number]}`)
                  : s,
              })),
            ]}
            onChange={(v) => onChange("status", v)}
          />
        </label>
        <label class="history-filter">
          <span class="label">{t("usage.history.client")}</span>
          <Select
            value={filters.client}
            options={[all, ...withCurrent(clients, filters.client).map((c) => ({ value: c, label: c }))]}
            onChange={(v) => onChange("client", v)}
          />
        </label>
      </div>
      {!!error && <LoadError thing={t("usage.history.thing.facets")} error={error} onRetry={onRetry} />}
      <div class="cluster history-filter-actions">
        <Button variant="text" size="s" disabled={!filtered} onClick={onReset}>
          {t("usage.history.reset")}
        </Button>
        <a class="btn" data-variant="outline" data-size="s" href={exportHref(filters)} download>
          {t("usage.history.export")}
        </a>
      </div>
    </section>
  );
}

// ---------- Totals ----------

function Totals({ summary, error, onRetry }: { summary: UsageSummary | null; error: unknown; onRetry: () => void }) {
  if (error)
    return (
      <Section label={t("usage.history.totals")}>
        <LoadError thing={t("usage.history.thing.summary")} error={error} onRetry={onRetry} />
      </Section>
    );
  const s = summary;
  const failed = s?.failed ?? null;
  return (
    <NumbersBand label={t("usage.history.totals")}>
      <Stat label={t("usage.history.t.requests")} value={s ? formatCount(s.requests) : DASH} />
      <Stat
        label={t("usage.history.t.in")}
        value={s ? formatCompact(s.prompt_tokens) : DASH}
        title={s ? formatCount(s.prompt_tokens) : undefined}
      />
      <Stat
        label={t("usage.history.t.out")}
        value={s ? formatCompact(s.completion_tokens) : DASH}
        title={s ? formatCount(s.completion_tokens) : undefined}
      />
      <Stat
        label={t("usage.history.t.cached")}
        value={s ? formatCompact(s.cached_tokens) : DASH}
        title={s ? formatCount(s.cached_tokens) : undefined}
      />
      <Stat label={t("usage.history.t.failed")} value={failed === null ? DASH : formatCount(failed)} accent={!!failed} />
      <Stat label={t("usage.history.t.ttft")} value={formatMs(s?.ttft_p50_ms)} />
    </NumbersBand>
  );
}

// ---------- Charts ----------

interface SeriesProps {
  data: UsageTimeseries | null;
  error: unknown;
  onRetry: () => void;
}

function TokensPerDay({ data, error, onRetry }: SeriesProps) {
  const active = engine.value?.model ?? null;
  const stacked = useMemo(
    () => stackTokensPerDay(data?.points ?? [], active, t("usage.history.other")),
    [data, active],
  );
  return (
    <Section label={t("usage.history.tokens_day")}>
      {error ? (
        <LoadError thing={t("usage.history.thing.timeseries")} error={error} onRetry={onRetry} />
      ) : (
        <>
          <Chart
            ariaLabel={t("usage.history.tokens_day_aria")}
            data={stacked.data as never}
            series={stacked.series.map((s) => ({ label: s.label, tone: s.tone }))}
            yFormat={formatCompact}
            empty={t("usage.history.heatmap_none")}
            bars
          />
          {stacked.series.length > 0 && (
            <p class="meta history-legend">
              {[...stacked.series].reverse().map((s) => (
                <span key={s.label} class="history-legend-item">
                  <span class={`history-swatch`} data-tone={s.tone} aria-hidden="true">
                    {s.glyph}
                  </span>{" "}
                  {s.label}
                </span>
              ))}
            </p>
          )}
        </>
      )}
    </Section>
  );
}

function RequestsOverTime({ data, error, onRetry }: SeriesProps) {
  const cols = useMemo(() => requestsOverTime(data?.points ?? []), [data]);
  return (
    <Section label={t("usage.history.requests_time")}>
      {error ? (
        <LoadError thing={t("usage.history.thing.timeseries")} error={error} onRetry={onRetry} />
      ) : (
        <Chart
          ariaLabel={t("usage.history.requests_time")}
          data={cols as never}
          series={[
            { label: t("usage.history.s.requests"), primary: true },
            { label: t("usage.history.s.errors"), dashed: true },
          ]}
          yFormat={formatCompact}
          empty={t("usage.history.heatmap_none")}
        />
      )}
    </Section>
  );
}

const HOURS = Array.from({ length: 24 }, (_, h) => h);
const DAYS = [0, 1, 2, 3, 4, 5, 6] as const;
const dayName = (d: number) => t(`usage.history.wd.${d as 0}`);

function cellLabel(grid: number[][], tokens: number[][] | null, d: number, h: number): string {
  const n = grid[d]?.[h] ?? 0;
  const hour = String(h).padStart(2, "0");
  const tk = tokens?.[d]?.[h];
  return typeof tk === "number"
    ? t("usage.history.heatmap_tokens", { day: dayName(d), hour, n, tokens: formatCompact(tk) })
    : t("usage.history.heatmap_cell", { day: dayName(d), hour, n });
}

function Heatmap({ data, error, onRetry }: SeriesProps) {
  const grid = data?.heatmap ?? null;
  const tokens = data?.heatmap_tokens ?? null;
  const [focus, setFocus] = useState<[number, number]>([0, 0]);
  const [readout, setReadout] = useState<string | null>(null);
  const host = useRef<HTMLDivElement>(null);
  if (error)
    return (
      <Section label={t("usage.history.heatmap")}>
        <LoadError thing={t("usage.history.thing.heatmap")} error={error} onRetry={onRetry} />
      </Section>
    );
  if (!grid)
    return (
      <Section label={t("usage.history.heatmap")}>
        <Loading label={t("usage.history.loading")} />
      </Section>
    );
  const max = heatMax(grid);
  const move = (dd: number, dh: number) => {
    const d = Math.min(6, Math.max(0, focus[0] + dd));
    const h = Math.min(23, Math.max(0, focus[1] + dh));
    setFocus([d, h]);
    setReadout(cellLabel(grid, tokens, d, h));
    requestAnimationFrame(() =>
      host.current?.querySelector<HTMLElement>(`[data-cell="${d}-${h}"]`)?.focus(),
    );
  };
  return (
    <Section label={t("usage.history.heatmap")} meta={t("usage.history.heatmap_hint")}>
      {max === 0 ? (
        <p class="meta">{t("usage.history.heatmap_none")}</p>
      ) : (
        <>
          <div
            ref={host}
            class="heatmap"
            role="grid"
            aria-label={t("usage.history.heatmap_aria")}
            onKeyDown={(e) => {
              const k = e.key;
              const delta: Record<string, [number, number]> = {
                ArrowLeft: [0, -1],
                ArrowRight: [0, 1],
                ArrowUp: [-1, 0],
                ArrowDown: [1, 0],
                Home: [0, -24],
                End: [0, 24],
              };
              const dlt = delta[k];
              if (dlt) {
                e.preventDefault();
                move(dlt[0], dlt[1]);
              }
            }}
          >
            <div class="heat-row heat-hours" role="row" aria-hidden="true">
              <span class="heat-day" />
              {HOURS.map((h) => (
                <span key={h} class="heat-hour meta">
                  {h % 6 === 0 ? String(h).padStart(2, "0") : ""}
                </span>
              ))}
            </div>
            {DAYS.map((d) => (
              <div class="heat-row" role="row" key={d}>
                <span class="heat-day meta" role="rowheader">
                  {dayName(d)}
                </span>
                {HOURS.map((h) => {
                  const n = grid[d]?.[h] ?? 0;
                  const label = cellLabel(grid, tokens, d, h);
                  const isFocus = focus[0] === d && focus[1] === h;
                  return (
                    <span
                      key={h}
                      role="gridcell"
                      class="heat-cell"
                      data-cell={`${d}-${h}`}
                      data-n={n}
                      tabIndex={isFocus ? 0 : -1}
                      aria-label={label}
                      title={label}
                      style={{ "--heat": String(heatOpacity(n, max)) }}
                      onMouseEnter={() => setReadout(label)}
                      onFocus={() => {
                        setFocus([d, h]);
                        setReadout(label);
                      }}
                    />
                  );
                })}
              </div>
            ))}
          </div>
          <p class="meta tnum heat-readout" aria-live="polite">
            {readout ?? " "}
          </p>
        </>
      )}
    </Section>
  );
}

// ---------- Top clients ----------

function TopClients({ summary, error, onRetry }: { summary: UsageSummary | null; error: unknown; onRetry: () => void }) {
  const rows = clientRows(summary);
  const columns: Column<ClientRow>[] = [
    { key: "client", label: t("usage.history.c.client"), render: (r) => <span class="mono">{r.client || t("usage.history.c.unknown")}</span> },
    { key: "requests", label: t("usage.history.c.requests"), align: "right", render: (r) => formatCount(r.requests) },
    { key: "tokens", label: t("usage.history.c.tokens"), align: "right", render: (r) => (r.tokens === null ? DASH : formatCompact(r.tokens)) },
    { key: "last", label: t("usage.history.c.last"), render: (r) => (r.last_seen ? formatRelativeTime(r.last_seen) : DASH) },
  ];
  return (
    <Section label={t("usage.history.top_clients")}>
      {error ? (
        <LoadError thing={t("usage.history.thing.clients")} error={error} onRetry={onRetry} />
      ) : !summary ? (
        <Loading label={t("usage.history.loading")} />
      ) : (
        <Table
          columns={columns}
          rows={rows}
          rowKey={(r) => r.client}
          caption={t("usage.history.top_clients")}
          empty={t("usage.history.heatmap_none")}
        />
      )}
    </Section>
  );
}

// ---------- Request log ----------

function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function RequestLog({
  rows,
  loading,
  error,
  onRetry,
  page,
  total,
  filtered,
  onOlder,
  onNewer,
  onReset,
}: {
  rows: UsageRow[] | null;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  page: number;
  total: number | null;
  filtered: boolean;
  onOlder: () => void;
  onNewer: () => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (id: number) => {
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };
  const num = (v: number | null | undefined) => (v === null || v === undefined ? DASH : formatCount(v));
  const columns: Column<UsageRow>[] = [
    { key: "time", label: t("usage.history.l.time"), render: (r) => <span class="tnum nowrap">{formatTime(r.ts)}</span> },
    {
      key: "model",
      label: t("usage.history.l.model"),
      render: (r) => (
        <span class="mono history-model">
          {r.model ?? DASH}
          {r.profile ? `:${r.profile}` : ""}
        </span>
      ),
    },
    { key: "endpoint", label: t("usage.history.l.endpoint"), render: (r) => <span class="mono">{r.endpoint}</span> },
    { key: "client", label: t("usage.history.l.client"), render: (r) => r.client ?? t("usage.history.c.unknown") },
    {
      key: "status",
      label: t("usage.history.l.status"),
      render: (r) => (
        <span class="tnum" title={r.error_code ?? undefined}>
          {r.error_code === "cancelled" ? t("usage.history.st.cancelled") : r.status}
        </span>
      ),
    },
    { key: "in", label: t("usage.history.l.in"), align: "right", render: (r) => num(r.prompt_tokens) },
    { key: "cached", label: t("usage.history.l.cached"), align: "right", render: (r) => num(r.cached_tokens) },
    { key: "out", label: t("usage.history.l.out"), align: "right", render: (r) => num(r.completion_tokens) },
    { key: "ttft", label: t("usage.history.l.ttft"), align: "right", render: (r) => formatMs(r.ttft_ms) },
    { key: "dur", label: t("usage.history.l.dur"), align: "right", render: (r) => formatMs(r.duration_ms) },
    {
      key: "injected",
      label: t("usage.history.l.injected"),
      render: (r) => {
        const n = injectedCount(r.injected);
        if (n === 0) return <span class="mute">{DASH}</span>;
        return (
          <button
            type="button"
            class="tag-button"
            aria-expanded={open.has(r.id)}
            aria-controls={open.has(r.id) ? `injected-${r.id}` : undefined}
            aria-label={t("usage.history.injected_show", { id: r.id })}
            onClick={() => toggle(r.id)}
          >
            <Tag>{t("usage.history.injected", { n })}</Tag>
          </button>
        );
      },
    },
  ];
  const { a, b } = pageRange(page, rows?.length ?? 0);
  const hasOlder = total !== null ? b < total : (rows?.length ?? 0) === HISTORY_PAGE_SIZE;
  return (
    <Section label={t("usage.history.log")}>
      {error ? (
        <LoadError thing={t("usage.history.thing.requests")} error={error} onRetry={onRetry} />
      ) : rows === null ? (
        <Loading label={t("usage.history.loading")} />
      ) : rows.length === 0 && page === 0 ? (
        <Empty
          title={filtered ? t("usage.history.heatmap_none") : t("usage.history.empty")}
          action={
            filtered ? (
              <Button size="s" onClick={onReset}>
                {t("usage.history.reset")}
              </Button>
            ) : undefined
          }
        >
          {filtered ? undefined : t("usage.history.empty_body")}
        </Empty>
      ) : (
        <>
          <Table
            class="table-scroll history-log"
            columns={columns}
            rows={rows}
            rowKey={(r) => String(r.id)}
            caption={t("usage.history.log")}
            detail={(r) =>
              open.has(r.id) && r.injected ? (
                <div class="history-injected" id={`injected-${r.id}`}>
                  <span class="label">{t("usage.history.l.injected")}</span>
                  <pre class="mono">{JSON.stringify(r.injected, null, 2)}</pre>
                </div>
              ) : null
            }
          />
          <div class="cluster history-pager">
            <span class="meta tnum" aria-live="polite">
              {rows.length === 0
                ? DASH
                : total !== null
                  ? t("usage.history.page_range_total", { a, b, total })
                  : t("usage.history.page_range", { a, b })}
            </span>
            <Button size="s" disabled={page === 0 || loading} onClick={onNewer}>
              ‹ {t("usage.history.prev")}
            </Button>
            <Button size="s" disabled={!hasOlder || loading} onClick={onOlder}>
              {t("usage.history.next")} ›
            </Button>
          </div>
        </>
      )}
    </Section>
  );
}
