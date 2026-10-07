import { useEffect, useRef, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import { api } from "../api/client";
import { postStream } from "../api/stream";
import type { DiagnosticsBundle, DoctorReport, StorageInfo, TraceItem, TraceList } from "../api/models";
import {
  Banner,
  Button,
  ConfirmSheet,
  CopyButton,
  Disclosure,
  Empty,
  ExternalLink,
  KeyValue,
  LoadError,
  Loading,
  LogPane,
  PageHeader,
  Sheet,
  Section,
  SubNav,
  Table,
  Tag,
  Toggle,
  toast,
  toastError,
  type LogPaneLine,
} from "../components";
import { copyText } from "../components/CopyButton";
import { DASH, formatBytes } from "../lib/format";
import { useApi } from "../lib/use-api";
import { engine, settings } from "../store";
import { t } from "../strings/logs";
import { useTitle } from "../lib/title";
import { LOGS_TABS } from "./tabs";
import {
  DOCTOR_GLYPH,
  STATUS_OPEN_KEYS,
  STATUS_REFRESH_MS,
  STATUS_SCHEMA,
  annotateStatus,
  bundleFilename,
  bundleText,
  doctorText,
  textBytes,
  tracesTotal,
} from "./logs/diagnostics";

const ISSUE_URL = "https://github.com/incoai/splash/issues/new";

const pad = (n: number) => String(n).padStart(2, "0");
/** Local `YYYY-MM-DD HH:MM` for a trace's modification time. */
export function traceTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function clockTime(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export default function Diagnostics() {
  useTitle(t("logs.diag.page_title"));
  const bundle = useApi((s) => api.read<DiagnosticsBundle>("/diagnostics", undefined, s));
  const traces = useApi((s) => api.get<TraceList>("/traces", undefined, s));
  const v = bundle.data?.versions;
  const sys = bundle.data?.system;
  const meta = [
    v?.engine?.version && t("logs.diag.meta_engine", { version: v.engine.version }),
    v?.status_schema_version != null && t("logs.diag.meta_schema", { schema: v.status_schema_version }),
    v?.gui && t("logs.diag.meta_gui", { version: v.gui }),
    sys?.chip,
    sys && formatBytes(sys.memory_bytes),
    sys && `macOS ${sys.macos_version}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      <SubNav items={LOGS_TABS} label={t("logs.subnav")} exact />
      <PageHeader title={t("logs.diag.title")} meta={meta || undefined} />
      <BundleBand bundle={bundle.data} error={bundle.error} onRetry={bundle.reload} />
      <RawStatus />
      <TracesBand traces={traces.data} error={traces.error} onRetry={traces.reload} />
      <DoctorBand />
      <PathsBand />
    </>
  );
}

// ---------- §5.1 bundle ----------

function BundleBand({ bundle, error, onRetry }: { bundle: DiagnosticsBundle | null; error: unknown; onRetry: () => void }) {
  const text = bundle ? bundleText(bundle) : "";
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = bundleFilename();
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <Section label={t("logs.diag.bundle")}>
      <div class="stack">
        <p class="body">{t("logs.diag.bundle_body")}</p>
        {error ? (
          <LoadError thing={t("logs.diag.bundle_thing")} error={error} onRetry={onRetry} />
        ) : (
          <div class="cluster">
            <Button
              variant="solid"
              disabled={!bundle}
              onClick={() =>
                void copyText(text).then((ok) => ok && toast(t("logs.diag.bundle_copied", { size: formatBytes(textBytes(text)) })))
              }
            >
              {t("logs.diag.bundle_copy")}
            </Button>
            <Button disabled={!bundle} onClick={download}>
              {t("logs.diag.bundle_download")}
            </Button>
          </div>
        )}
        <Disclosure summary={t("logs.diag.bundle_included")}>
          <p class="body">{t("logs.diag.bundle_redaction")}</p>
        </Disclosure>
        <p class="meta">
          <ExternalLink href={ISSUE_URL}>{t("logs.diag.bundle_issue")}</ExternalLink> · {t("logs.diag.bundle_issue_note")}
        </p>
      </div>
    </Section>
  );
}

// ---------- §5.2 raw /status ----------

function JsonNode({ name, value, open }: { name: string; value: unknown; open: boolean }) {
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value);
    return (
      <details class="json-node" open={open}>
        <summary class="mono">
          <span class="disclosure-glyph" aria-hidden="true">
            ▸
          </span>
          {name} <span class="meta">{Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`}</span>
        </summary>
        <div class="json-children">
          {entries.map(([k, v]) => (
            <JsonNode key={k} name={k} value={v} open={open} />
          ))}
        </div>
      </details>
    );
  }
  const note = annotateStatus(name, value);
  return (
    <div class="json-leaf mono">
      <span>{name}</span>: <span class="tnum">{JSON.stringify(value)}</span>
      {note && <span class="meta"> · {note}</span>}
    </div>
  );
}

function RawStatus() {
  const running = !!engine.value?.model;
  const [auto, setAuto] = useState(false);
  const [doc, setDoc] = useState<Record<string, unknown> | null>(null);
  const [fetched, setFetched] = useState<Date | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [expand, setExpand] = useState<boolean | null>(null);
  const [gen, setGen] = useState(0);
  const load = async () => {
    try {
      const d = await api.get<Record<string, unknown>>("/engine/status");
      setDoc(d);
      setFetched(new Date());
      setError(null);
    } catch (e) {
      setError(e);
    }
  };
  useEffect(() => {
    if (running) void load();
  }, [running]);
  useEffect(() => {
    if (!auto || !running) return;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, STATUS_REFRESH_MS);
    return () => clearInterval(id);
  }, [auto, running]);
  const json = doc ? JSON.stringify(doc, null, 2) : "";
  const schemaVersion = typeof doc?.schema_version === "number" ? doc.schema_version : null;
  const body = doc && (
    <div class="json-view" key={gen}>
      {Object.entries(doc).map(([k, val]) => (
        <JsonNode
          key={k}
          name={k}
          value={val}
          open={expand ?? (STATUS_OPEN_KEYS as readonly string[]).includes(k)}
        />
      ))}
    </div>
  );
  return (
    <Section label={t("logs.diag.raw")}>
      <div class="stack">
        <div class="cluster">
          <Toggle checked={auto} onChange={setAuto} label={t("logs.diag.raw_refresh")} disabled={!running} />
          <span class="meta">{t("logs.diag.raw_refresh")}</span>
          {fetched && (
            <span class="meta tnum">
              {t("logs.diag.raw_fetched", { time: clockTime(fetched), size: formatBytes(textBytes(json)) })}
            </span>
          )}
          {doc && <CopyButton text={json} what="/status" />}
          <Button size="s" variant="text" disabled={!doc} onClick={() => (setExpand(true), setGen(gen + 1))}>
            {t("logs.diag.raw_expand")}
          </Button>
          <Button size="s" variant="text" disabled={!doc} onClick={() => (setExpand(false), setGen(gen + 1))}>
            {t("logs.diag.raw_collapse")}
          </Button>
        </div>
        {schemaVersion !== null && schemaVersion !== STATUS_SCHEMA && (
          <Banner tone="info">{t("logs.diag.raw_schema", { schema: schemaVersion })}</Banner>
        )}
        {!running ? (
          <>
            <Empty title={t("logs.diag.raw_stopped")} />
            {doc && fetched && <Disclosure summary={t("logs.diag.raw_last", { time: clockTime(fetched) })}>{body}</Disclosure>}
          </>
        ) : error ? (
          <LoadError thing={t("logs.diag.raw_thing")} error={error} onRetry={() => void load()} />
        ) : !doc ? (
          <Loading />
        ) : (
          body
        )}
      </div>
    </Section>
  );
}

// ---------- §5.3 crash traces ----------

function TracesBand({ traces, error, onRetry }: { traces: TraceList | null; error: unknown; onRetry: () => void }) {
  const list = traces?.traces ?? [];
  const [deleting, setDeleting] = useState<TraceItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<LogPaneLine[] | null>(null);
  const [exit, setExit] = useState<number | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replayName, setReplayName] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);

  async function replay(name: string) {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLines([]);
    setExit(null);
    setReplaying(true);
    setReplayName(name);
    let n = 0;
    try {
      for await (const msg of postStream<{ text?: string; level?: string; code?: number }>(
        `/api/admin/traces/${encodeURIComponent(name)}/replay`,
        {},
        { signal: c.signal },
      )) {
        if (msg.event === "exit") setExit(typeof msg.data.code === "number" ? msg.data.code : null);
        else {
          const text = msg.data.text ?? String(msg.data);
          const level = msg.data.level as LogPaneLine["level"];
          setLines((prev) => [...(prev ?? []), { key: n++, text, level }]);
        }
      }
    } catch (e) {
      if (!c.signal.aborted) toastError(t("logs.diag.replay_failed"), e);
    } finally {
      if (ctrl.current === c) {
        ctrl.current = null;
        setReplaying(false);
      }
    }
  }
  /** Stop: the manager kills the replay and the stream ends with `exit {code: -15}` (api.md §12.2). */
  async function stopReplay() {
    if (!replayName) return;
    try {
      await api.post(`/traces/${encodeURIComponent(replayName)}/replay/cancel`);
    } catch {
      ctrl.current?.abort();
    }
  }
  function askReplay(name: string) {
    if (engine.value?.model && engine.value.state !== "stopped") setConfirm(name);
    else void replay(name);
  }
  async function reveal(name: string) {
    try {
      await api.post("/system/reveal", { target: "trace", id: name });
    } catch (e) {
      toastError(t("logs.diag.reveal_failed"), e);
    }
  }
  const show = !!traces && (traces.enabled || list.length > 0);
  return (
    <Section label={t("logs.diag.traces")}>
      <div class="stack">
        {error ? (
          <LoadError thing={t("logs.diag.traces_thing")} error={error} onRetry={onRetry} />
        ) : !traces ? (
          <Loading />
        ) : !show ? (
          <p class="body">
            {t("logs.diag.traces_off")} <Link href="/settings/advanced#advanced.crash_trace">{t("logs.diag.traces_settings")} ↗</Link>
          </p>
        ) : (
          <>
            <p class="meta">
              {traces.enabled
                ? t("logs.diag.traces_on", { dir: traces.directory, n: list.length, size: formatBytes(tracesTotal(list)) })
                : t("logs.diag.traces_off")}
            </p>
            <Banner tone="info">{t("logs.diag.replay_note")}</Banner>
            <Table
              columns={[
                { key: "name", label: t("logs.diag.traces_file"), render: (r) => <span class="mono trace-name">{r.name}</span> },
                { key: "time", label: t("logs.diag.traces_time"), render: (r) => <span class="tnum nowrap">{traceTime(r.modified_at)}</span> },
                { key: "size", label: t("logs.diag.traces_size"), align: "right", render: (r) => <span class="tnum nowrap">{formatBytes(r.size_bytes)}</span> },
                {
                  key: "actions",
                  label: t("logs.diag.traces_actions"),
                  render: (r) => (
                    <span class="cluster nowrap">
                      <Button size="s" disabled={replaying} onClick={() => askReplay(r.name)}>
                        {t("logs.diag.replay")}
                      </Button>
                      <Button size="s" variant="text" onClick={() => void reveal(r.name)}>
                        {t("logs.diag.reveal")}
                      </Button>
                      <Button size="s" variant="text" onClick={() => setDeleting(r)}>
                        {t("logs.diag.delete")}
                      </Button>
                    </span>
                  ),
                },
              ]}
              rows={list}
              rowKey={(r) => r.name}
              caption={t("logs.diag.traces")}
              empty={t("logs.diag.traces_none")}
            />
            {lines && (
              <Disclosure summary={t("logs.diag.replay_output")} open>
                <LogPane lines={lines} label={t("logs.diag.replay_output")} height={240} />
                <div class="cluster">
                  {exit !== null && <span class="meta">{t("logs.diag.replay_exit", { code: exit })}</span>}
                  {replaying && (
                    <Button size="s" onClick={() => void stopReplay()}>
                      {t("logs.diag.replay_stop")}
                    </Button>
                  )}
                </div>
              </Disclosure>
            )}
          </>
        )}
        <p class="meta">{t("logs.diag.traces_footer")}</p>
      </div>
      <Sheet
        open={!!confirm}
        title={t("logs.diag.replay_confirm_title")}
        role="alertdialog"
        onClose={() => setConfirm(null)}
        footer={
          <>
            <Button variant="text" onClick={() => setConfirm(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="solid"
              onClick={() => {
                const name = confirm!;
                setConfirm(null);
                void api
                  .post("/engine/stop")
                  .then(() => replay(name))
                  .catch((e) => toastError(t("logs.diag.replay_failed"), e));
              }}
            >
              {t("logs.diag.replay_stop_engine")}
            </Button>
            <Button
              onClick={() => {
                const name = confirm!;
                setConfirm(null);
                void replay(name);
              }}
            >
              {t("logs.diag.replay_anyway")}
            </Button>
          </>
        }
      >
        <p class="body">{t("logs.diag.replay_confirm_body")}</p>
      </Sheet>
      <ConfirmSheet
        open={!!deleting}
        title={t("logs.diag.delete_title")}
        confirmLabel={`${t("logs.diag.delete")} · ${deleting ? formatBytes(deleting.size_bytes) : ""}`}
        busy={busy}
        onClose={() => setDeleting(null)}
        onConfirm={async () => {
          if (!deleting) return;
          setBusy(true);
          try {
            await api.del(`/traces/${encodeURIComponent(deleting.name)}`);
            setDeleting(null);
            onRetry();
          } catch (e) {
            toastError(t("logs.diag.delete_failed"), e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <p class="body mono">
          {t("logs.diag.delete_body", { name: deleting?.name ?? "", size: deleting ? formatBytes(deleting.size_bytes) : "" })}
        </p>
        {deleting && <Tag tone="mute">{traceTime(deleting.modified_at)}</Tag>}
      </ConfirmSheet>
    </Section>
  );
}

// ---------- §5.4 doctor and paths ----------

function DoctorBand() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setReport(await api.read<DoctorReport>("/doctor"));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section label={t("logs.diag.doctor")}>
      <div class="stack">
        <div class="cluster">
          <Button loading={busy} onClick={() => void run()}>
            {t("logs.diag.doctor_run")}
          </Button>
          {report && <CopyButton text={doctorText(report)} what={t("logs.diag.doctor")} />}
        </div>
        {!!error && <LoadError thing={t("logs.diag.doctor_thing")} error={error} onRetry={() => void run()} />}
        {report && (
          <KeyValue
            label={t("logs.diag.doctor")}
            items={report.checks.map((c) => ({
              key: c.id,
              label: c.label,
              value: `${DOCTOR_GLYPH[c.status]} ${c.message}`,
              // A fix is a command or path: mono, never uppercased (copying "CHMOD 700 /USERS/…" would fail).
              meta: c.fix ? <code class="mono">{c.fix}</code> : undefined,
              accent: c.status === "fail",
            }))}
          />
        )}
      </div>
    </Section>
  );
}

function PathsBand() {
  const storage = useApi((s) => api.get<StorageInfo>("/storage", undefined, s));
  // Permission modes come from the doctor's `permissions` check (~/.splash 0700, settings.json 0600).
  const doctor = useApi((s) => api.read<DoctorReport>("/doctor", undefined, s));
  const perm = doctor.data?.checks.find((c) => c.id === "permissions") ?? null;
  const reveal = (target: string) =>
    void api.post("/system/reveal", { target }).catch((e) => toastError(t("logs.diag.reveal_failed"), e));
  const s = storage.data;
  const base = (settings.value?.resolved as { base?: string } | undefined)?.base ?? null;
  const row = (key: string, label: string, path: string | null | undefined, bytes: number | null | undefined, target: string | null) => ({
    key,
    label,
    value: (
      <span class="cluster path-row">
        <span class="mono path-text">{path ?? DASH}</span>
        {bytes != null && <span class="meta tnum">{formatBytes(bytes)}</span>}
        {target && (
          <Button size="s" variant="text" onClick={() => reveal(target)}>
            {t("logs.diag.reveal")}
          </Button>
        )}
      </span>
    ),
  });
  return (
    <Section label={t("logs.diag.paths")}>
      {storage.error ? (
        <LoadError thing={t("logs.diag.paths_thing")} error={storage.error} onRetry={storage.reload} />
      ) : !s ? (
        <Loading />
      ) : (
        <KeyValue
          label={t("logs.diag.paths")}
          items={[
            row("gui", t("logs.diag.path.gui"), base, null, null),
            row("data", t("logs.diag.path.data"), s.splash_data_dir, s.splash_data_bytes, "splash_data_dir"),
            row("models", t("logs.diag.path.models"), s.models_dir, s.models_bytes, "models_dir"),
            row("cache", t("logs.diag.path.cache"), s.cache_dir, s.cache_bytes, "cache_dir"),
            row("logs", t("logs.diag.path.logs"), base ? `${base}/logs` : null, null, "logs_dir"),
            {
              key: "perm",
              label: t("logs.diag.path.permissions"),
              value: perm ? `${DOCTOR_GLYPH[perm.status]} ${perm.message}` : doctor.error ? DASH : "…",
              meta: perm?.fix ? <code class="mono">{perm.fix}</code> : undefined,
              accent: perm?.status === "fail",
            },
          ]}
        />
      )}
    </Section>
  );
}
