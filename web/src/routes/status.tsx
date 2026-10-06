import { useEffect, useState } from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import { Empty } from "../components/States";
import { Section } from "../components/Section";
import { SubNav } from "../components/SubNav";
import { useApi } from "../lib/use-api";
import { useTitle } from "../lib/title";
import { stateDisplay } from "../lib/engine-state";
import { engine, settings } from "../store";
import { liveSample, liveSampleAt, useLiveMetrics } from "../store/live";
import { t } from "../strings/status";
import { useInstalled } from "./models/hooks";
import { rawStatus } from "./status/api";
import { StatusHeader } from "./status/Header";
import { StateBanners, StartupBand } from "./status/Startup";
import { NumbersBand, type Scope } from "./status/Numbers";
import { ChartsBand } from "./status/Charts";
import { EndpointsBand } from "./status/Endpoints";
import { stateGroup, viewOf } from "./status/logic";
import { nextWindow, readWindow, type WindowMin } from "./status/series";
import { STATUS_TABS } from "./tabs";
import { StatusFailureBand as FailureBand, StatusFitSheet as FitSheet, StatusLowerBands as LowerBands } from "../routes";

// LowerBands and FitSheet are below the fold and load as their own chunks (see ../routes).



const SCOPE_KEY = "splash-gui-status-scope";

function readScope(param: string | null): Scope {
  if (param === "all" || param === "session") return param;
  try {
    return localStorage.getItem(SCOPE_KEY) === "all" ? "all" : "session";
  } catch {
    return "session";
  }
}

export default function StatusPage() {
  useLiveMetrics();
  const e = engine.value;
  const installed = useInstalled();
  const [params, setParams] = useSearchParams();
  const scope = readScope(params.get("scope"));
  const windowMin = readWindow(params.get("window") ?? "15");
  const group = stateGroup(e?.state ?? null);
  const running = !!e?.model && group !== "stopped";
  const raw = useApi(rawStatus, [e?.state], running);
  const persistent = Boolean(
    (settings.value?.settings?.global?.serve as { persistent_cache?: boolean } | undefined)?.persistent_cache,
  );
  const port = (settings.value?.settings?.global?.server as { port?: number } | undefined)?.port ?? 8000;
  const models = installed.data?.models ?? null;
  const failed = e?.state === "failed";
  const budget = failed && e?.error?.kind === "budget_refusal";
  const [fitOpen, setFitOpen] = useState(false);
  useTitle(e?.model ? t("status.doc_title", { chip: stateDisplay(e.state).label, name: e.model.slice(e.model.indexOf("/") + 1) }) : t("status.page_title"));
  const [fitShownFor, setFitShownFor] = useState<string | null>(null);

  const setQuery = (key: string, value: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(key, value);
        return next;
      },
      { replace: true },
    );
  const setScope = (s: Scope) => {
    try {
      localStorage.setItem(SCOPE_KEY, s);
    } catch {
      /* per-browser convenience only */
    }
    setQuery("scope", s);
  };
  const setWindow = (w: WindowMin) => setQuery("window", String(w));

  // Refresh the raw document every 5 s while the engine runs (Memory/Cache/Scheduler/Latency).
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void raw.reload(), 5000);
    return () => clearInterval(timer);
  }, [running]);

  // `.` cycles the chart window (docs/ui/02 §7).
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "." || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      const el = ev.target as HTMLElement | null;
      if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return;
      setWindow(nextWindow(windowMin));
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [windowMin]);

  // "Model does not fit." opens by itself the first time the failure is entered (docs/ui/02 §11.1).
  const failKey = budget ? `${e?.model}|${e?.error?.message}` : null;
  useEffect(() => {
    if (failKey && failKey !== fitShownFor) {
      setFitShownFor(failKey);
      setFitOpen(true);
    }
  }, [failKey]);

  const family = models?.find((m) => m.id === e?.model)?.family ?? null;
  const fullError = viewOf(e?.view).error ?? null;
  const showCharts = running || group === "frozen";

  return (
    <>
      <SubNav items={STATUS_TABS} label={t("status.tabs")} exact />
      <StatusHeader engine={e} models={models} modelsFailed={!!installed.error} persistentCache={persistent} />
      <StateBanners engine={e} persistentCache={persistent} />
      {e && group === "starting" && <StartupBand engine={e} />}
      {e && failed ? (
        <FailureBand engine={e} error={fullError} family={family} onDetails={() => setFitOpen(true)} />
      ) : group === "stopped" && models !== null && models.length === 0 ? (
        <Section label={t("status.numbers.label")}>
          <Empty
            title={t("status.charts.empty_none")}
            action={
              <Link href="/models/downloader" class="btn">
                {t("status.actions.open_downloader")}
              </Link>
            }
          >
            {t("status.charts.empty_none_body")}
          </Empty>
        </Section>
      ) : (
        group !== "starting" && (
          <NumbersBand
            scope={scope}
            onScope={setScope}
            sample={liveSample.value}
            sampleAt={liveSampleAt.value}
            raw={raw.data}
            stopped={!running}
            frozen={group === "frozen"}
          />
        )
      )}
      <EndpointsBand />
      {showCharts ? (
        <ChartsBand
          windowMin={windowMin}
          onWindow={setWindow}
          diskEnabled={!!liveSample.value?.disk?.enabled}
          lanesReported={raw.data ? !!raw.data.scheduler : true}
        />
      ) : (
        group !== "starting" &&
        !(models !== null && models.length === 0) && (
          <Section label={t("status.charts.label")}>
            <Empty title={t("status.charts.empty_stopped")}>{t("status.charts.empty_stopped_body", { port: String(port) })}</Empty>
          </Section>
        )
      )}
      <LowerBands raw={raw.data} stopped={!running} models={(models ?? []).map((m) => m.id)} />
      {budget && fullError ? (
        <FitSheet
          open={fitOpen}
          onClose={() => setFitOpen(false)}
          error={fullError}
          model={e?.model ?? null}
          family={family}
        />
      ) : null}
    </>
  );
}
