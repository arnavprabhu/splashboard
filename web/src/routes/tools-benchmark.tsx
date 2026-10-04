import { useEffect, useState } from "preact/hooks";
import { api } from "../api/client";
import type {
  BenchmarkPreflight,
  BenchmarkRun,
  BenchmarkRuns,
  BenchmarkStarted,
} from "../api/models";
import {
  Button,
  CodeBlock,
  ConfirmSheet,
  LoadError,
  Section,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import { ToolPage } from "./tools";
type BenchmarkScenario =
  "decode_short" | "cold_prefill" | "cached_ttft" | "concurrency";
const scenarios: BenchmarkScenario[] = [
  "decode_short",
  "cold_prefill",
  "cached_ttft",
  "concurrency",
];
export default function Benchmark() {
  const runs = useApi((s) =>
    api.get<BenchmarkRuns>("/benchmark/runs", undefined, s),
  );
  const preflight = useApi((s) =>
    api.get<BenchmarkPreflight>("/benchmark/preflight", undefined, s),
  );
  const [selected, setSelected] = useState<BenchmarkScenario[]>(scenarios);
  const [samples, setSamples] = useState(3);
  const [details, setDetails] = useState<BenchmarkRun[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState(false);
  const running = runs.data?.runs.find((r) => r.state === "running");
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      void runs.reload();
      void api
        .get<BenchmarkRun>(`/benchmark/runs/${running.id}`)
        .then((r) =>
          setDetails((prev) => [r, ...prev.filter((p) => p.id !== r.id)]),
        )
        .catch(setError);
    }, 1000);
    return () => clearInterval(timer);
  }, [running?.id]);
  async function start() {
    try {
      const result = await api.post<BenchmarkStarted>("/benchmark", {
        scenarios: selected,
        samples,
      });
      setConfirm(false);
      await runs.reload();
      setDetails([
        await api.get<BenchmarkRun>(`/benchmark/runs/${result.run_id}`),
      ]);
    } catch (e) {
      setError(e);
    }
  }
  return (
    <ToolPage tool="benchmark">
      <Section label="Run benchmark">
        <p>
          Each scenario runs one warmup, then measured samples. Benchmarks run
          in the foreground and can take several minutes.
        </p>
        {!!preflight.error && (
          <LoadError
            thing="benchmark preflight"
            error={preflight.error}
            onRetry={preflight.reload}
          />
        )}
        <div class="stack">
          {scenarios.map((s) => (
            <label key={s}>
              <input
                type="checkbox"
                checked={selected.includes(s)}
                disabled={!!running}
                onChange={(e) =>
                  setSelected(
                    e.currentTarget.checked
                      ? [...selected, s]
                      : selected.filter((v) => v !== s),
                  )
                }
              />
              {s.replace(/_/g, " ")}
            </label>
          ))}
          <label>
            Samples
            <input
              type="number"
              min={1}
              max={10}
              value={samples}
              onInput={(e) => setSamples(e.currentTarget.valueAsNumber)}
            />
          </label>
        </div>
        {preflight.data?.warnings.map((w) => (
          <p key={w} role="alert">
            {w}
          </p>
        ))}
        {running ? (
          <Button
            onClick={() =>
              void api
                .post("/benchmark/cancel")
                .then(runs.reload)
                .catch(setError)
            }
          >
            Cancel all benchmark streams
          </Button>
        ) : (
          <Button
            variant="accent"
            disabled={
              !selected.length ||
              !Number.isInteger(samples) ||
              samples < 1 ||
              samples > 10
            }
            onClick={() => void preflight.reload().then(() => setConfirm(true))}
          >
            Start benchmark
          </Button>
        )}
        {!!error && <LoadError thing="benchmark" error={error} />}
      </Section>
      <Section label="Saved runs">
        {!!runs.error && (
          <LoadError
            thing="benchmark runs"
            error={runs.error}
            onRetry={runs.reload}
          />
        )}
        <p>
          Select up to two runs to compare hardware, settings, and measured
          results.
        </p>
        {runs.data?.runs.map((r) => (
          <div class="listrow" key={r.id}>
            <label>
              <input
                type="checkbox"
                checked={details.some((d) => d.id === r.id)}
                onChange={(e) => {
                  if (!e.currentTarget.checked)
                    setDetails(details.filter((d) => d.id !== r.id));
                  else
                    void api
                      .get<BenchmarkRun>(`/benchmark/runs/${r.id}`)
                      .then((run) =>
                        setDetails((prev) => [...prev.slice(-1), run]),
                      )
                      .catch((e) => toastError("Could not open run.", e));
                }}
              />
              {new Date(r.ts).toLocaleString()} · {r.model} · {r.state}
            </label>
          </div>
        ))}
      </Section>
      <Section label="Results">
        <div class="benchmark-compare">
          {details.map((r) => (
            <div key={r.id}>
              <h2 class="heading">{r.model}</h2>
              <p>
                {r.state} · {r.engine_version}
              </p>
              {r.error && <p role="alert">{r.error}</p>}
              <CodeBlock code={JSON.stringify(r.results, null, 2)} />
              <details>
                <summary>Hardware, power & settings</summary>
                <CodeBlock
                  code={JSON.stringify(
                    {
                      hardware: r.hardware,
                      power: r.power,
                      settings: r.settings,
                    },
                    null,
                    2,
                  )}
                />
              </details>
            </div>
          ))}
        </div>
      </Section>
      <ConfirmSheet
        open={confirm}
        title="Start benchmark."
        confirmLabel="Run"
        disabled={!preflight.data?.ready}
        onClose={() => setConfirm(false)}
        onConfirm={start}
      >
        <p>
          The current model must be ready and idle. Cancel stops every benchmark
          stream.
        </p>
        {preflight.data?.warnings.map((w) => (
          <p key={w}>{w}</p>
        ))}
      </ConfirmSheet>
    </ToolPage>
  );
}
