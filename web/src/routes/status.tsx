import { useEffect, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import { CopyButton, KeyValue, Section, SubNav } from "../components";
import { formatBytes, formatCount, formatMs } from "../lib/format";
import { useApi } from "../lib/use-api";
import { engine, settings } from "../store";
import { liveSample, liveSampleAt, useLiveMetrics } from "../store/live";
import { useInstalled } from "./models/hooks";
import { rawStatus } from "./status/api";
import { StatusHeader } from "./status/Header";
import { StateBanners, StartupBand } from "./status/Startup";
import { NumbersBand, type Scope } from "./status/Numbers";
import { ChartsBand } from "./status/Charts";
import { EndpointsBand } from "./status/Endpoints";
import type { WindowMin } from "./status/series";
import { STATUS_TABS } from "./tabs";

export function endpoints(origin: string) {
  return [
    { label: "OpenAI base", url: `${origin}/v1` },
    { label: "Anthropic base", url: origin },
  ];
}
function values(
  data: unknown,
  prefix = "",
): Array<{ key: string; label: string; value: string }> {
  if (!data || typeof data !== "object") return [];
  return Object.entries(data).flatMap(([key, value]) => {
    const name = prefix + key.replace(/_/g, " ");
    if (value !== null && typeof value === "object")
      return values(value, name + " · ");
    return [
      {
        key: name,
        label: name,
        value:
          value == null
            ? "—"
            : typeof value === "number"
              ? key.endsWith("_bytes")
                ? formatBytes(value)
                : key.endsWith("_ms")
                  ? formatMs(value)
                  : formatCount(value)
              : typeof value === "boolean"
                ? value
                  ? "On"
                  : "Off"
                : String(value),
      },
    ];
  });
}
export default function StatusPage() {
  useLiveMetrics();
  const e = engine.value;
  const installed = useInstalled();
  const raw = useApi(rawStatus, [e?.state], !!e?.model);
  const [scope, setScope] = useState<Scope>("session");
  const [windowMin, setWindow] = useState<WindowMin>(5);
  const persistent = Boolean(
    settings.value?.settings?.global?.serve?.persistent_cache,
  );
  useEffect(() => {
    if (!e?.model) return;
    const timer = setInterval(() => void raw.reload(), 5000);
    return () => clearInterval(timer);
  }, [e?.model]);
  return (
    <>
      <SubNav items={STATUS_TABS} label="Status" exact />
      <StatusHeader
        engine={e}
        models={installed.data?.models ?? null}
        modelsFailed={!!installed.error}
        persistentCache={persistent}
      />
      <StateBanners engine={e} persistentCache={persistent} />
      {e?.state.startsWith("starting.") && <StartupBand engine={e} />}
      <NumbersBand
        scope={scope}
        onScope={setScope}
        sample={liveSample.value}
        sampleAt={liveSampleAt.value}
        raw={raw.data}
        stopped={!e?.model}
        frozen={
          !!e &&
          ["recovering", "engine_failed", "stopping", "crashed"].includes(
            e.state,
          )
        }
      />
      <EndpointsBand />
      <ChartsBand
        windowMin={windowMin}
        onWindow={setWindow}
        diskEnabled={!!liveSample.value?.disk?.enabled}
        lanesReported={!!raw.data?.scheduler}
      />
      {(
        [
          "memory_governor",
          "memory_actual",
          "kv",
          "state",
          "disk",
          "cache",
          "scheduler",
          "admission",
          "latency",
        ] as const
      ).map((group) => (
        <Section
          key={group}
          label={
            {
              memory_governor: "Memory budget",
              memory_actual: "Metal memory",
              kv: "KV cache",
              state: "State cache",
              disk: "SSD and persistent cache",
              cache: "Cache efficiency",
              scheduler: "Scheduler",
              admission: "Admission",
              latency: "Latency",
            }[group]
          }
        >
          <KeyValue items={values(raw.data?.[group])} />
          {group === "memory_actual" && (
            <p class="meta">Metal allocations, not process RSS.</p>
          )}
        </Section>
      ))}
      <Section label="Claude Code">
        <div class="cluster">
          <code>splash launch claude</code>
          <CopyButton text="splash launch claude" />
          <Link href="/integrations">All integrations</Link>
        </div>
      </Section>
    </>
  );
}
