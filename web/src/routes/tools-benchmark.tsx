import { useEffect, useState } from "preact/hooks";
import { api } from "../api/client";
import type { BenchmarkPreflight, BenchmarkRun, BenchmarkRuns, BenchmarkRunSummary, BenchmarkStarted } from "../api/models";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Checkbox } from "../components/controls";
import { ConfirmSheet } from "../components/ConfirmSheet";
import { copyText } from "../components/CopyButton";
import { NumberInput } from "../components/inputs";
import { NumbersBand, Stat } from "../components/NumbersBand";
import { ProgressBar } from "../components/ProgressBar";
import { Section } from "../components/Section";
import { LoadError } from "../components/States";
import { Table } from "../components/Table";
import { toast, toastError } from "../components/Toast";
import { Toggle } from "../components/Toggle";
import { DASH, formatCount, formatDuration, formatMs, formatPercent, formatTokPerSec, formatTokens } from "../lib/format";
import { useApi } from "../lib/use-api";
import { engine, useEvent } from "../store";
import { t } from "../strings/tools";
import "../styles/pages/tools.css";
import { ToolPage, engineReady } from "./tools";

type Scenario = "decode_short" | "cold_prefill" | "cached_ttft" | "concurrency";
const SCENARIOS: readonly Scenario[] = ["decode_short", "cold_prefill", "cached_ttft", "concurrency"];
const PREFILL = [2048, 8192, 32768];

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const tps = (v: unknown) => (num(v) === null ? DASH : formatTokPerSec(num(v), { unit: false }));
const msOf = (v: unknown) => (num(v) === null ? DASH : formatMs(num(v)));

function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function shortName(id: string): string {
  return id.slice(id.indexOf("/") + 1);
}

function median(values: Array<number | null>): number | null {
  const v = values.filter((x): x is number => x !== null).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m]! : (v[m - 1]! + v[m]!) / 2;
}

/** Context needed per prefill size: the prompt plus generation headroom (D-08-6). */
export function contextGate(size: number): number {
  return size + 1024;
}

interface Progress {
  run_id: string;
  scenario: string;
  step: number;
  total_steps: number;
  state: string;
  message: string | null;
}

export default function Benchmark() {
  const ctx = engine.value?.maximum_context_tokens ?? null;
  const ready = engineReady();
  const runs = useApi((s) => api.get<BenchmarkRuns>("/benchmark/runs", undefined, s));
  const preflight = useApi((s) => api.get<BenchmarkPreflight>("/benchmark/preflight", undefined, s), [engine.value?.model]);
  const [enabled, setEnabled] = useState<Record<Scenario, boolean>>({ decode_short: true, cold_prefill: true, cached_ttft: true, concurrency: true });
  const [samples, setSamples] = useState<number | null>(3);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [started, setStarted] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [comparing, setComparing] = useState<string[] | null>(null);
  const [deleting, setDeleting] = useState<BenchmarkRunSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const running = runs.data?.runs.find((r) => r.state === "running") ?? null;

  useEvent("benchmark.progress", (d) => {
    const p = d as Progress;
    setProgress(p);
    if (["done", "cancelled", "failed"].includes(p.state)) {
      setStarted(null);
      void runs.reload();
    }
  });
  useEffect(() => {
    if (!running) return;
    setStarted((s) => s ?? Date.parse(running.ts));
    const id = setInterval(() => {
      setNow(Date.now());
      void runs.reload();
    }, 2000);
    return () => clearInterval(id);
  }, [running?.id]);

  const sizes = PREFILL.filter((s) => ctx === null || ctx >= contextGate(s));
  const cachedOk = ctx === null || ctx >= contextGate(32768);
  const chosen = SCENARIOS.filter((s) => enabled[s] && (s !== "cached_ttft" || cachedOk) && (s !== "cold_prefill" || sizes.length > 0));
  const n = samples ?? 3;
  const samplesOk = Number.isInteger(n) && n >= 1 && n <= 10;
  const estimate = Math.max(1, Math.round((chosen.length * n * 0.4) + 1));

  async function start() {
    setBusy(true);
    try {
      await api.post<BenchmarkStarted>("/benchmark", { scenarios: chosen, samples: n, prefill_tokens: sizes });
      setStarted(Date.now());
      setProgress(null);
      await runs.reload();
    } catch (err) {
      toastError(t("tools.bm.start_failed"), err);
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    try {
      await api.post("/benchmark/cancel");
      toast(t("tools.bm.cancelled"));
      await runs.reload();
    } catch (err) {
      toastError(t("tools.bm.start_failed"), err);
    }
  }

  const pct = progress && progress.total_steps ? progress.step / progress.total_steps : null;

  return (
    <ToolPage tool="benchmark">
      <Section label={t("tools.bm.scenarios")} meta={t("tools.bm.lead")}>
        <div class="stack">
          <ul class="bm-scenarios">
            {SCENARIOS.map((s, i) => {
              const gated = (s === "cached_ttft" && !cachedOk) || (s === "cold_prefill" && sizes.length === 0);
              const partial = s === "cold_prefill" && sizes.length < PREFILL.length && sizes.length > 0;
              return (
                <li key={s} class="bm-scenario">
                  <Toggle checked={enabled[s] && !gated} disabled={gated || !!running} onChange={(v) => setEnabled({ ...enabled, [s]: v })} label={t(`tools.bm.s.${s}`)} />
                  <div>
                    <span class="label">
                      {String(i + 1).padStart(2, "0")} — {t(`tools.bm.s.${s}`)}
                    </span>
                    <p class="meta">{t(`tools.bm.d.${s}`)}</p>
                    {(gated || partial) && ctx !== null && (
                      <p class="meta">{t("tools.bm.needs_ctx", { need: formatTokens(contextGate(s === "cached_ttft" ? 32768 : (PREFILL.find((p) => !sizes.includes(p)) ?? 32768))), have: formatTokens(ctx) })}</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          <label class="cluster">
            <span class="label">{t("tools.bm.samples")}</span>
            <NumberInput value={samples} onChange={setSamples} min={1} max={10} step={1} class="bm-samples" />
            <span class="meta">{t("tools.bm.samples_help")}</span>
          </label>
        </div>
      </Section>
      <Section label={t("tools.bm.before")}>
        <div class="stack">
          {preflight.error ? (
            <LoadError thing={t("tools.bm.thing_preflight")} error={preflight.error} onRetry={preflight.reload} />
          ) : (
            (preflight.data?.warnings ?? []).map((w) => (
              <Banner key={w} tone="warn">
                {w}
              </Banner>
            ))
          )}
          <p class="meta">{t("tools.bm.native_note")}</p>
          {running ? (
            <div class="stack bm-running" role="status">
              <div class="cluster" style={{ justifyContent: "space-between" }}>
                <span class="label">
                  {t("tools.bm.running")}
                  {progress?.scenario ? ` · ${t(`tools.bm.s.${progress.scenario as Scenario}`)}` : ""}
                  {progress?.message ? ` · ${progress.message}` : ""}
                </span>
                <Button variant="solid" onClick={() => void cancel()}>
                  {t("tools.bm.cancel")}
                </Button>
              </div>
              <ProgressBar live value={pct} label={t("tools.bm.progress")} />
              <span class="meta tnum">
                {pct === null ? "…" : formatPercent(pct, 0)}
                {started ? ` · ${formatDuration(Math.max(0, (now - started) / 1000))}` : ""}
              </span>
            </div>
          ) : (
            <div class="cluster">
              <Button variant={ready && chosen.length && samplesOk ? "accent" : "outline"} disabled={!ready || !chosen.length || !samplesOk || busy} loading={busy} title={ready ? undefined : t("tools.needs_ready")} onClick={() => void start()}>
                {t("tools.bm.run")}
              </Button>
              <span class="meta">{t("tools.bm.estimate", { min: estimate, max: estimate + 2 })}</span>
            </div>
          )}
        </div>
      </Section>
      <Section
        label={t("tools.bm.saved")}
        meta={
          selected.length >= 2 && selected.length <= 4 ? (
            <Button size="s" onClick={() => setComparing([...selected])}>
              {t("tools.bm.compare", { n: selected.length })}
            </Button>
          ) : undefined
        }
      >
        {runs.error ? (
          <LoadError thing={t("tools.bm.thing_runs")} error={runs.error} onRetry={runs.reload} />
        ) : (
          <Table
            caption={t("tools.bm.saved")}
            rows={runs.data?.runs ?? []}
            rowKey={(r) => r.id}
            empty={t("tools.bm.none")}
            columns={[
              {
                key: "sel",
                label: t("tools.bm.col.select"),
                hideLabel: true,
                render: (r) => (
                  <Checkbox
                    checked={selected.includes(r.id)}
                    ariaLabel={t("tools.bm.select", { when: when(r.ts) })}
                    label=""
                    onChange={(v) => setSelected(v ? [...selected, r.id].slice(-4) : selected.filter((x) => x !== r.id))}
                  />
                ),
              },
              { key: "ts", label: t("tools.bm.col.when"), render: (r) => <span class="tnum nowrap">{when(r.ts)}</span> },
              { key: "model", label: t("tools.model"), render: (r) => <span class="mono">{shortName(r.model)}</span> },
              { key: "engine", label: t("tools.bm.col.engine"), render: (r) => (r.engine_version ? `Splash ${r.engine_version}` : DASH) },
              { key: "state", label: t("tools.bm.col.state"), render: (r) => t(`tools.bm.state.${r.state}`) },
              { key: "decode", label: t("tools.bm.h.decode"), align: "right", render: (r) => tps(r.headline?.decode_tps) },
              {
                key: "actions",
                label: t("tools.pg.col.actions"),
                hideLabel: true,
                render: (r) => (
                  <span class="cluster nowrap">
                    <Button size="s" variant="text" onClick={() => setOpenRun(openRun === r.id ? null : r.id)}>
                      {t("tools.bm.open")}
                    </Button>
                    <Button size="s" variant="text" onClick={() => setDeleting(r)}>
                      {t("tools.bm.delete")}
                    </Button>
                  </span>
                ),
              },
            ]}
          />
        )}
      </Section>
      {openRun && <RunView id={openRun} onClose={() => setOpenRun(null)} />}
      {comparing && <CompareView ids={comparing} onClose={() => setComparing(null)} />}
      <ConfirmSheet
        open={!!deleting}
        title={t("tools.bm.delete_title")}
        confirmLabel={t("tools.bm.delete")}
        onClose={() => setDeleting(null)}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await api.del(`/benchmark/runs/${encodeURIComponent(deleting.id)}`);
            setSelected(selected.filter((x) => x !== deleting.id));
            if (openRun === deleting.id) setOpenRun(null);
            setDeleting(null);
            await runs.reload();
          } catch (err) {
            toastError(t("tools.bm.start_failed"), err);
          }
        }}
      >
        <p class="body">{deleting ? t("tools.bm.delete_body", { when: when(deleting.ts), model: deleting.model }) : ""}</p>
      </ConfirmSheet>
    </ToolPage>
  );
}

function results(run: BenchmarkRun, scenario: Scenario) {
  return run.results.filter((r) => r.scenario === scenario);
}

function headlineOf(run: BenchmarkRun) {
  const dec = results(run, "decode_short")[0]?.summary ?? {};
  const pre = results(run, "cold_prefill");
  const big = pre[pre.length - 1];
  const cached = results(run, "cached_ttft")[0]?.summary ?? {};
  const conc = results(run, "concurrency");
  const peak = conc.reduce<number | null>((m, r) => {
    const v = num(r.summary.aggregate_tps);
    return v !== null && (m === null || v > m) ? v : m;
  }, null);
  return {
    decode: num(dec.decode_tps) ?? num(dec.aggregate_tps),
    prefill: num(big?.summary.prefill_tps),
    coldTtft: num(big?.summary.ttft_p50_ms),
    cachedTtft: num(cached.ttft_p50_ms),
    aggregate: peak,
  };
}

function summaryText(run: BenchmarkRun): string {
  const h = headlineOf(run);
  const hw = run.hardware as Record<string, unknown>;
  return [
    `| Splash ${run.engine_version ?? "—"} | ${run.model} | ${String(hw.chip ?? "")} |`,
    "|---|---|---|",
    `| ${t("tools.bm.h.decode")} | ${tps(h.decode)} | |`,
    `| ${t("tools.bm.h.prefill")} | ${tps(h.prefill)} | |`,
    `| ${t("tools.bm.h.cold_ttft")} | ${msOf(h.coldTtft)} | |`,
    `| ${t("tools.bm.h.cached_ttft")} | ${msOf(h.cachedTtft)} | |`,
    `| ${t("tools.bm.h.aggregate")} | ${tps(h.aggregate)} | |`,
  ].join("\n");
}

function RunView({ id, onClose }: { id: string; onClose: () => void }) {
  const run = useApi((s) => api.get<BenchmarkRun>(`/benchmark/runs/${encodeURIComponent(id)}`, undefined, s), [id]);
  if (run.error) return <Section><LoadError thing={t("tools.bm.thing_run")} error={run.error} onRetry={run.reload} /></Section>;
  const r = run.data;
  if (!r) return null;
  const h = headlineOf(r);
  const hw = r.hardware as Record<string, unknown>;
  const decode = results(r, "decode_short")[0];
  return (
    <>
      <NumbersBand
        label={t("tools.bm.run_title", { when: when(r.ts) })}
        actions={
          <>
            <span class="meta mono">{r.model}</span>
            <Button size="s" variant="text" onClick={onClose}>
              {t("tools.bm.close")}
            </Button>
          </>
        }
      >
        <Stat label={t("tools.bm.h.decode")} value={tps(h.decode)} />
        <Stat label={t("tools.bm.h.prefill")} value={tps(h.prefill)} />
        <Stat label={t("tools.bm.h.cold_ttft")} value={msOf(h.coldTtft)} />
        <Stat label={t("tools.bm.h.cached_ttft")} value={msOf(h.cachedTtft)} />
        <Stat label={t("tools.bm.h.aggregate")} value={tps(h.aggregate)} />
      </NumbersBand>
      <Section label={t(`tools.bm.s.decode_short`)} meta={[r.engine_version && `Splash ${r.engine_version}`, hw.chip, r.revision && `rev ${String(r.revision).slice(0, 7)}`].filter(Boolean).join(" · ")}>
        {decode ? (
          <Table
            caption={t("tools.bm.s.decode_short")}
            rowKey={(row) => String(row.i)}
            rows={[
              ...decode.samples.map((s, i) => {
                const req = ((s.requests as Array<Record<string, unknown>>) ?? [])[0] ?? {};
                const tok = num(req.completion_tokens);
                const pms = num(req.predicted_ms);
                return { i: String(i + 1), tps: tok !== null && pms ? (tok * 1000) / pms : null, ttft: num(req.ttft_ms) };
              }),
            ].concat([{ i: t("tools.bm.median"), tps: null, ttft: null }])}
            columns={[
              { key: "i", label: t("tools.bm.sample"), render: (x) => x.i },
              {
                key: "tps",
                label: "tok/s",
                align: "right",
                render: (x) => (x.i === t("tools.bm.median") ? tps(median(decode.samples.map((s) => { const q = ((s.requests as Array<Record<string, unknown>>) ?? [])[0] ?? {}; const a = num(q.completion_tokens); const b = num(q.predicted_ms); return a !== null && b ? (a * 1000) / b : null; }))) : tps(x.tps)),
              },
              { key: "ttft", label: "TTFT", align: "right", render: (x) => (x.i === t("tools.bm.median") ? msOf(num(decode.summary.ttft_p50_ms)) : msOf(x.ttft)) },
              { key: "acc", label: "draft", align: "right", render: (x) => (x.i === t("tools.bm.median") ? (num(decode.summary.draft_acceptance) === null ? DASH : formatPercent(num(decode.summary.draft_acceptance), 0)) : "") },
            ]}
          />
        ) : (
          <p class="meta">{DASH}</p>
        )}
      </Section>
      <Section label={t("tools.bm.s.cold_prefill")}>
        <Table
          caption={t("tools.bm.s.cold_prefill")}
          rows={results(r, "cold_prefill")}
          rowKey={(x) => String(x.params.value)}
          columns={[
            { key: "size", label: "size", render: (x) => formatTokens(num(x.params.value)) },
            { key: "pps", label: "prompt_per_second", align: "right", render: (x) => tps(x.summary.prefill_tps) },
            { key: "ttft", label: "TTFT", align: "right", render: (x) => msOf(x.summary.ttft_p50_ms) },
            { key: "cache", label: "cache_n", align: "right", render: (x) => (num(x.summary.cached_tokens) === null ? DASH : formatCount(Math.round(num(x.summary.cached_tokens)!))) },
          ]}
        />
      </Section>
      <Section label={t("tools.bm.s.cached_ttft")}>
        {results(r, "cached_ttft").map((x) => (
          <p key="c" class="tnum">
            TTFT {msOf(x.summary.ttft_p50_ms)} · cache_n {num(x.summary.cached_tokens) === null ? DASH : formatCount(Math.round(num(x.summary.cached_tokens)!))} of {num(x.summary.prompt_tokens) === null ? DASH : formatCount(Math.round(num(x.summary.prompt_tokens)!))}
          </p>
        ))}
      </Section>
      <Section label={t("tools.bm.s.concurrency")}>
        <Table
          caption={t("tools.bm.s.concurrency")}
          rows={results(r, "concurrency")}
          rowKey={(x) => String(x.params.value)}
          columns={[
            { key: "w", label: "streams", render: (x) => String(x.params.value ?? x.params.width ?? DASH) },
            { key: "agg", label: "aggregate tok/s", align: "right", render: (x) => tps(x.summary.aggregate_tps) },
            { key: "per", label: "per-stream tok/s", align: "right", render: (x) => tps(x.summary.decode_tps) },
            { key: "ttft", label: "TTFT p50", align: "right", render: (x) => msOf(x.summary.ttft_p50_ms) },
          ]}
        />
        <div class="cluster">
          <a class="btn" data-variant="text" data-size="s" download={`splash-benchmark-${r.id}.json`} href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(r, null, 2))}`}>
            {t("tools.bm.export")}
          </a>
          <Button size="s" variant="text" onClick={() => void copyText(summaryText(r)).then((ok) => ok && toast(t("tools.pg.copied", { what: t("tools.bm.copy_summary") })))}>
            {t("tools.bm.copy_summary")}
          </Button>
        </div>
      </Section>
    </>
  );
}

/** Δ vs the first run; lower-is-better metrics invert the sign colouring (D-08-8). */
export function delta(base: number | null, v: number | null, lowerBetter: boolean): { text: string; worse: boolean } | null {
  if (base === null || v === null || base === 0) return null;
  const d = ((v - base) / base) * 100;
  const worse = lowerBetter ? d > 0 : d < 0;
  return { text: `${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(1)} %`, worse };
}

function CompareView({ ids, onClose }: { ids: string[]; onClose: () => void }) {
  const data = useApi((s) => Promise.all(ids.map((id) => api.get<BenchmarkRun>(`/benchmark/runs/${encodeURIComponent(id)}`, undefined, s))), [ids.join(",")]);
  if (data.error) return <Section><LoadError thing={t("tools.bm.thing_runs")} error={data.error} onRetry={data.reload} /></Section>;
  const list = data.data;
  if (!list) return null;
  const heads = list.map(headlineOf);
  const differs = new Set(list.map((r) => `${r.model}|${r.engine_version}`)).size > 1;
  const metrics: Array<{ key: keyof ReturnType<typeof headlineOf>; label: string; lower: boolean; fmt: (v: number | null) => string }> = [
    { key: "decode", label: t("tools.bm.h.decode"), lower: false, fmt: tps },
    { key: "prefill", label: t("tools.bm.h.prefill"), lower: false, fmt: tps },
    { key: "coldTtft", label: t("tools.bm.h.cold_ttft"), lower: true, fmt: msOf },
    { key: "cachedTtft", label: t("tools.bm.h.cached_ttft"), lower: true, fmt: msOf },
    { key: "aggregate", label: t("tools.bm.h.aggregate"), lower: false, fmt: tps },
  ];
  return (
    <Section label={t("tools.bm.compare_title")} meta={<Button size="s" variant="text" onClick={onClose}>{t("tools.bm.close")}</Button>}>
      <div class="stack">
        {differs && <Banner tone="info">{t("tools.bm.compare_differs")}</Banner>}
        {metrics.map((m) => (
          <NumbersBand key={m.key} label={m.label}>
            {list.map((r, i) => {
              const d = i === 0 ? null : delta(heads[0]![m.key], heads[i]![m.key], m.lower);
              return (
                <Stat
                  key={r.id}
                  label={`${when(r.ts)} · ${shortName(r.model)} · ${r.engine_version ?? DASH}`}
                  value={m.fmt(heads[i]![m.key])}
                  sub={d ? <span class={d.worse ? "acc" : undefined}>{d.text}</span> : undefined}
                />
              );
            })}
          </NumbersBand>
        ))}
      </div>
    </Section>
  );
}
