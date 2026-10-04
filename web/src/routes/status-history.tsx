import { useState } from "preact/hooks";
import { api } from "../api/client";
import type { UsageRows, UsageSummary, UsageTimeseries } from "../api/models";
import {
  Button,
  Chart,
  LoadError,
  PageHeader,
  Section,
  SubNav,
} from "../components";
import { useApi } from "../lib/use-api";
import { formatCount, formatMs } from "../lib/format";
import { STATUS_TABS } from "./tabs";
export default function History() {
  const [model, setModel] = useState("");
  const [status, setStatus] = useState("");
  const [client, setClient] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const filters = {
    model: model || undefined,
    status: status || undefined,
    client: client || undefined,
  };
  const summary = useApi((s) => api.get<UsageSummary>("/usage/summary", {}, s));
  const series = useApi(
    (s) =>
      api.get<UsageTimeseries>(
        "/usage/timeseries",
        { view: "heatmap", group_by: "none", model: model || undefined },
        s,
      ),
    [model],
  );
  const rows = useApi(
    (s) => api.get<UsageRows>("/usage/requests", { ...filters, cursor }, s),
    [model, status, client, cursor],
  );
  const points = series.data?.points ?? [];
  return (
    <>
      <SubNav items={STATUS_TABS} label="Status" exact />
      <PageHeader title="Usage history." />
      <Section label="Tokens per day">
        <Chart
          title="Tokens"
          data={[
            points.map((p) => Date.parse(p.t) / 1000),
            points.map((p) => p.prompt_tokens),
            points.map((p) => p.completion_tokens),
          ]}
          series={[{ label: "Prompt", primary: true }, { label: "Completion" }]}
          bars
        />
        <p class="meta">
          {formatCount(summary.data?.requests)} requests ·{" "}
          {formatCount(summary.data?.total_tokens)} tokens
        </p>
      </Section>
      <Section label="Activity by hour">
        <div
          class="heatmap"
          role="img"
          aria-label="Requests by weekday and hour"
        >
          {series.data?.heatmap?.map((day, d) => (
            <div class="cluster" key={d}>
              <span>
                {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]}
              </span>
              {day.map((n, h) => (
                <span
                  class="heat-cell"
                  data-active={n > 0}
                  title={`${h}:00 · ${n} requests`}
                  key={h}
                >
                  {n || "·"}
                </span>
              ))}
            </div>
          ))}
        </div>
      </Section>
      <Section label="Top clients">
        {summary.data?.top_clients?.map((c) => (
          <p key={c.client}>
            {c.client} · {c.requests}
          </p>
        ))}
      </Section>
      <Section label="Request log">
        <div class="cluster">
          <label>
            Model{" "}
            <input
              value={model}
              onInput={(e) => {
                setModel(e.currentTarget.value);
                setCursor(null);
              }}
            />
          </label>
          <label>
            Status{" "}
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.currentTarget.value);
                setCursor(null);
              }}
            >
              <option value="">All</option>
              <option>2xx</option>
              <option>4xx</option>
              <option>5xx</option>
            </select>
          </label>
          <label>
            Client{" "}
            <input
              value={client}
              onInput={(e) => {
                setClient(e.currentTarget.value);
                setCursor(null);
              }}
            />
          </label>
          <a
            href={
              "/api/admin/usage/export.csv?" +
              new URLSearchParams(
                Object.entries(filters)
                  .filter(([, v]) => v)
                  .map(([k, v]) => [k, v!]),
              )
            }
          >
            Export CSV
          </a>
        </div>
        {!!rows.error && (
          <LoadError
            error={rows.error}
            thing="request history"
            onRetry={rows.reload}
          />
        )}
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                {[
                  "Time",
                  "Model",
                  "Endpoint",
                  "Client",
                  "Status",
                  "In / out",
                  "TTFT",
                  "Injected",
                ].map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.data?.rows.map((r) => (
                <tr key={r.id}>
                  <td>{new Date(r.ts).toLocaleString()}</td>
                  <td>{r.model}</td>
                  <td>{r.endpoint}</td>
                  <td>{r.client}</td>
                  <td>{r.status}</td>
                  <td>
                    {r.prompt_tokens} / {r.completion_tokens}
                  </td>
                  <td>{formatMs(r.ttft_ms)}</td>
                  <td>
                    <code>{JSON.stringify(r.injected)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.data?.next_cursor && (
          <Button onClick={() => setCursor(rows.data!.next_cursor!)}>
            Older requests
          </Button>
        )}
      </Section>
    </>
  );
}
