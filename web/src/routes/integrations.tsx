import { useEffect, useMemo, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import { ApiError, api, request } from "../api/client";
import type { CliIntegration, DesktopIntegration, EntriesRemoved, Integrations, LaunchPrint } from "../api/models";
import {
  Banner,
  Button,
  CodeBlock,
  ConfirmSheet,
  CopyButton,
  Disclosure,
  ExternalLink,
  KeyValue,
  LoadError,
  Loading,
  PageHeader,
  Section,
  Select,
  Sheet,
  StatusChip,
  Table,
  Tag,
  Toggle,
  Tooltip,
  toast,
  toastError,
} from "../components";
import { formatIndex, formatRelativeTime } from "../lib/format";
import { useApi } from "../lib/use-api";
import { engine, settings, useEvent } from "../store";
import { t } from "../strings/integrations";
import { useTitle } from "../lib/title";
import type { ModelEntry } from "./chat/types";
import {
  BACKUP_DIR,
  CLAUDE_SLOTS,
  DESKTOP_FILES,
  SDK_TABS,
  TAURI_ORIGIN,
  clock,
  desktopView,
  gatewayModels,
  stepsFor,
  stepViews,
  entryFile,
  isLoopbackHost,
  launchCommand,
  normaliseSlots,
  profileNoteClient,
  removableEntries,
  sdkSnippet,
  slotsSwitchModels,
  snippetOrigin,
  tauriOriginState,
  withTauriOrigin,
  type SdkTab,
  type Slots,
  type Step,
} from "./integrations/logic";
import { saveSettings } from "./welcome/api";

/** Sets IDs, profiles and paths inside a sentence in mono so a `.meta` line never uppercases them. */
function withMono(text: string, tokens: ReadonlyArray<string | null | undefined>) {
  const found = tokens.filter((x): x is string => !!x && text.includes(x));
  if (found.length === 0) return text;
  const re = new RegExp(`(${found.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`);
  return text.split(re).map((part, i) => (i % 2 === 1 ? <span key={i} class="mono">{part}</span> : part));
}

const BIONIC_GUIDE = "https://lmstudio.ai/docs/app/api";

type Global = Record<string, unknown> & {
  security?: { api_key_required?: boolean };
  server?: { allowed_origins?: string[] };
  integrations?: {
    claude_desktop?: { port?: number; slots?: Record<string, unknown> };
    codex_app?: { make_default?: boolean };
  };
};

function globalSettings(): Global {
  return (settings.value?.settings?.global ?? {}) as Global;
}

export default function IntegrationsPage() {
  useTitle(t("integrations.page_title"));
  const data = useApi((s) => api.read<Integrations>("/integrations", undefined, s));
  const models = useApi((s) => request<{ data: ModelEntry[] }>("/v1/models", { signal: s }));
  useEvent("integration.state", (d) => {
    const app = d as DesktopIntegration | null;
    if (!app?.name) return;
    data.setData((prev) =>
      prev ? { ...prev, desktop: prev.desktop.map((x) => (x.name === app.name ? { ...x, ...app } : x)) } : prev,
    );
  });

  const active = engine.value?.model ?? null;
  const entries = models.data?.data ?? [];
  const [pick, setPick] = useState<string>("");
  const model = pick || active || entries[0]?.id || "";
  const g = globalSettings();
  const auth = !!g.security?.api_key_required;
  const origin = snippetOrigin(location.origin);
  const local = isLoopbackHost(location.hostname);

  const options = useMemo(() => {
    const seen = new Set<string>();
    const out: { value: string; label: string }[] = [];
    const add = (id: string, label = id) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      out.push({ value: id, label });
    };
    if (active) add(active, t("integrations.model_active", { id: active }));
    for (const e of entries) add(e.id);
    return out;
  }, [entries, active]);

  const roots = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of entries) if (e.profile && e.root) m.set(e.id, e.root);
    return m;
  }, [entries]);

  return (
    <>
      <PageHeader
        title={t("integrations.title")}
        meta={
          <span class="cluster integrations-meta">
            <span>
              {t("integrations.server_label")} <span class="mono">{origin}</span>
            </span>
            <span>·</span>
            <span>{auth ? t("integrations.key_on") : t("integrations.key_off")}</span>
          </span>
        }
        actions={
          <label class="cluster integrations-model">
            <span class="label">{t("integrations.model")}</span>
            {options.length ? (
              <Select
                value={model}
                options={options}
                onChange={(v) => setPick(v === active ? "" : v)}
                aria-label={t("integrations.model_aria")}
              />
            ) : (
              <span class="meta">{t("integrations.model_none")}</span>
            )}
          </label>
        }
      />
      <p class="band tight body integrations-lead">{withMono(t("integrations.lead"), ["splash launch"])}</p>
      {!!data.error && (
        <Section>
          <LoadError thing={t("integrations.thing")} error={data.error} onRetry={data.reload} />
        </Section>
      )}
      {data.data?.unclean_shutdown && (
        <UncleanBand desktop={data.data.desktop} onDone={data.reload} />
      )}
      <Section label={t("integrations.cli")} id="cli">
        {(data.data?.cli ?? []).map((c, i) => (
          <CliRow
            key={c.name}
            index={i + 1}
            cli={c}
            pick={model}
            active={active}
            isProfile={roots.has(model)}
            local={local}
            onChanged={data.reload}
          />
        ))}
      </Section>
      <Section label={t("integrations.desktop")} id="desktop">
        {(data.data?.desktop ?? []).map((app, i) => (
          <DesktopRow
            key={app.name}
            index={(data.data?.cli.length ?? 0) + i + 1}
            app={app}
            local={local}
            options={options}
            roots={roots}
            onChanged={(next) =>
              data.setData((prev) =>
                prev ? { ...prev, desktop: prev.desktop.map((x) => (x.name === next.name ? next : x)) } : prev,
              )
            }
          />
        ))}
      </Section>
      <SdkBand origin={origin} model={model || "<model id>"} auth={auth} />
    </>
  );
}

// ---------- unclean shutdown (§6) ----------

function UncleanBand({ desktop, onDone }: { desktop: DesktopIntegration[]; onDone: () => void }) {
  const stuck = desktop.filter((d) => d.state === "needs_restore" || d.state === "connected");
  const [busy, setBusy] = useState(false);
  async function run(path: string, body?: unknown) {
    setBusy(true);
    try {
      await api.post(path, body);
      toast(t("integrations.desktop.restored"));
      onDone();
    } catch (e) {
      toastError(t("integrations.desktop.restore_failed"), e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Section>
      <Banner
        tone="warn"
        title={t("integrations.unclean.title")}
        actions={
          <span class="cluster">
            {stuck.map((d) => (
              <span class="cluster" key={d.name}>
                <Button size="s" disabled={busy} onClick={() => void run(`/integrations/${d.name}/connect`, { confirm_restart: true })}>
                  {t("integrations.unclean.reconnect")} · {d.label}
                </Button>
                <Button size="s" variant="solid" disabled={busy} onClick={() => void run(`/integrations/${d.name}/disconnect`)}>
                  {t("integrations.unclean.restore")} · {d.label}
                </Button>
              </span>
            ))}
            {stuck.length === 0 && (
              <Button size="s" variant="solid" disabled={busy} onClick={() => void run("/integrations/restore-all")}>
                {t("integrations.unclean.restore_all")}
              </Button>
            )}
          </span>
        }
      >
        {t("integrations.unclean.body")}
      </Banner>
    </Section>
  );
}

// ---------- CLI rows (§3) ----------

function MacOnly({ local, children }: { local: boolean; children: preact.VNode<Record<string, unknown>> }) {
  return local ? children : <Tooltip text={t("integrations.mac_only")}>{children}</Tooltip>;
}

function CliRow({
  index,
  cli,
  pick,
  active,
  isProfile,
  local,
  onChanged,
}: {
  index: number;
  cli: CliIntegration;
  pick: string;
  active: string | null;
  isProfile: boolean;
  local: boolean;
  onChanged: () => void;
}) {
  const command = launchCommand(cli, pick, active);
  const [opened, setOpened] = useState(false);
  const [termError, setTermError] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const removable = removableEntries(cli);
  useEffect(() => {
    if (!opened) return;
    const id = setTimeout(() => setOpened(false), 2000);
    return () => clearTimeout(id);
  }, [opened]);

  async function openTerminal() {
    if (!local) return;
    setTermError(false);
    try {
      await api.post(`/integrations/${cli.name}/open-terminal`, pick && pick !== active ? { model: pick } : {});
      setOpened(true);
    } catch {
      setTermError(true);
    }
  }
  async function remove() {
    setBusy(true);
    try {
      await api.del<EntriesRemoved>(`/integrations/${cli.name}/entries`);
      setRemoving(null);
      toast(t("integrations.cli.removed"));
      onChanged();
    } catch (e) {
      toastError(t("integrations.cli.remove_failed"), e);
    } finally {
      setBusy(false);
    }
  }
  const version = cli.installed
    ? cli.version
      ? t("integrations.cli.installed", { version: cli.version })
      : t("integrations.cli.installed_unknown")
    : null;
  const kind = cli.name === "hermes" || cli.name === "pi" ? cli.name : null;
  const profileNote = profileNoteClient(cli.name);
  return (
    <article class="integration-row stack" aria-labelledby={`cli-${cli.name}`}>
      <div class="integration-head">
        <h3 class="heading integration-title" id={`cli-${cli.name}`}>
          <span class="label">{formatIndex(index)} — </span>
          {cli.label}
        </h3>
        <p class="meta integration-state">
          {version ?? (
            <>
              {t("integrations.cli.not_installed")} ·{" "}
              <ExternalLink href={cli.install_url}>{t("integrations.cli.install")}</ExternalLink>
            </>
          )}
          {cli.last_launched_at && <> · {t("integrations.cli.last_launched", { when: formatRelativeTime(cli.last_launched_at) })}</>}
          {kind &&
            removable.map((entry) => (
              <span key={entry}>
                {" · "}
                {t(kind === "hermes" ? "integrations.cli.entry_hermes" : "integrations.cli.entry_pi", { entry })}{" "}
                <Button variant="text" size="s" onClick={() => setRemoving(entry)}>
                  {t("integrations.cli.remove")}
                </Button>
              </span>
            ))}
        </p>
      </div>
      <div class="integration-command">
        <code class="mono integration-cmd" aria-label={t("integrations.cli.command_aria", { name: cli.label })}>
          $ {command}
        </code>
        <span class="cluster">
          <CopyButton text={command} what={t("integrations.cli.command_aria", { name: cli.label })} />
          <MacOnly local={local}>
            <Button
              size="s"
              aria-disabled={!local ? "true" : undefined}
              onClick={() => void openTerminal()}
            >
              {opened ? t("integrations.cli.opened") : t("integrations.cli.open_terminal")}
            </Button>
          </MacOnly>
        </span>
      </div>
      {profileNote && (
        <p class="meta" data-testid={`profiles-${cli.name}`}>
          <span class="label">{t("integrations.cli.profiles")}</span> {withMono(t(`integrations.cli.profiles.${profileNote}`), [":no-think"])}
        </p>
      )}
      {!engine.value?.model && pick && (
        <p class="meta">{withMono(t("integrations.cli.will_load", { model: pick }), [pick])}</p>
      )}
      {termError && (
        <Banner tone="warn" title={t("integrations.cli.terminal_failed")}>
          {t("integrations.cli.terminal_failed_body")}
        </Banner>
      )}
      <ChangesPanel cli={cli} pick={pick} active={active} isProfile={isProfile} local={local} />
      {kind && (
        <ConfirmSheet
          open={removing !== null}
          title={t(kind === "hermes" ? "integrations.cli.remove_title_hermes" : "integrations.cli.remove_title_pi")}
          confirmLabel={t("integrations.cli.remove")}
          busy={busy}
          onClose={() => setRemoving(null)}
          onConfirm={remove}
        >
          <p class="mono">
            {t("integrations.cli.remove_body", {
              file: entryFile(kind, removing ?? ""),
              kind: t(kind === "hermes" ? "integrations.cli.remove_kind_hermes" : "integrations.cli.remove_kind_pi"),
              entry: removing ?? "",
            })}
          </p>
          <p>{t("integrations.cli.remove_backup", { dir: BACKUP_DIR })}</p>
        </ConfirmSheet>
      )}
    </article>
  );
}

/** "What this changes" (docs/ui/09 §3.3) from `POST /integrations/{client}/print?model=` (D58), fetched when opened. */
function ChangesPanel({ cli, pick, active, isProfile, local }: { cli: CliIntegration; pick: string; active: string | null; isProfile: boolean; local: boolean }) {
  const [open, setOpen] = useState(false);
  const model = pick || active || undefined;
  const print = useApi((s) => api.read<LaunchPrint>(`/integrations/${cli.name}/print`, model ? { model } : undefined, s), [cli.name, model], open);
  const kind = cli.name === "hermes" || cli.name === "pi";
  const p = print.data;
  const env = Object.entries(p?.env ?? {});
  return (
    <details class="disclosure" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary class="label">
        <span class="disclosure-glyph" aria-hidden="true">
          ▸
        </span>
        {t("integrations.cli.changes")}
      </summary>
      <div class="disclosure-body stack">
        <p class="body">{t(`integrations.cli.summary.${cli.name}` as "integrations.cli.summary.claude")}</p>
        {print.error ? (
          <LoadError thing={t("integrations.cli.changes_thing")} error={print.error} onRetry={print.reload} />
        ) : !p ? (
          open && <Loading />
        ) : (
          <>
            {!p.exact && <Tag tone="mute">{t("integrations.cli.static")}</Tag>}
            {p.model && <p class="meta mono">{p.model}</p>}
            {env.length > 0 && (
              <KeyValue
                label={t("integrations.cli.env")}
                items={env.map(([k, v]) => ({
                  key: k,
                  label: <span class="mono">{k}</span>,
                  value: <span class="mono">{v}</span>,
                  meta: p.secret_env?.includes(k) ? t("integrations.cli.secret") : undefined,
                }))}
              />
            )}
            <KeyValue
              items={[
                ...(p.removed_env?.length ? [{ key: "removed", label: t("integrations.cli.removed_env"), value: <span class="mono">{p.removed_env.join(", ")}</span> }] : []),
                ...(p.command ? [{ key: "command", label: t("integrations.cli.command"), value: <span class="mono">{p.command}</span> }] : p.args?.length ? [{ key: "args", label: t("integrations.cli.args"), value: <span class="mono">{p.args.join(" ")}</span> }] : []),
                ...(kind ? [{ key: "backups", label: t("integrations.cli.backups"), value: <span class="mono">{BACKUP_DIR}</span> }] : []),
              ]}
            />
            {(p.files ?? []).length > 0 && (
              <Table
                caption={t("integrations.cli.files")}
                rows={p.files ?? []}
                rowKey={(f) => f.path}
                columns={[
                  { key: "path", label: t("integrations.desktop.file"), render: (f) => <span class="mono ms-path">{f.path}</span> },
                  { key: "change", label: t("integrations.desktop.change"), render: (f) => f.change },
                ]}
              />
            )}
            {(p.notes ?? []).map((n) => (
              <p class="body" key={n}>
                {n}
              </p>
            ))}
          </>
        )}
        {isProfile && <Banner tone="info">{t("integrations.cli.profile_note", { name: cli.label })}</Banner>}
        {kind && <RevealBackup name={cli.name} local={local} />}
        <p class="meta">{t("integrations.cli.source")}</p>
      </div>
    </details>
  );
}

/** VIEW BACKUP (docs/ui/09 §4.5, G16): reveals the newest backup folder in Finder. */
function RevealBackup({ name, local }: { name: string; local: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <MacOnly local={local}>
      <Button
        size="s"
        variant="text"
        aria-disabled={!local ? "true" : undefined}
        loading={busy}
        onClick={() => {
          if (!local) return;
          setBusy(true);
          void api
            .post(`/integrations/${name}/reveal-backup`)
            .catch((err) => (err instanceof ApiError && err.code === "no_backup" ? toast(t("integrations.backup_none")) : toastError(t("integrations.backup_failed"), err)))
            .finally(() => setBusy(false));
        }}
      >
        {t("integrations.backup_view")}
      </Button>
    </MacOnly>
  );
}

// ---------- Desktop rows (§4) ----------

function DesktopRow({
  index,
  app,
  local,
  options,
  roots,
  onChanged,
}: {
  index: number;
  app: DesktopIntegration;
  local: boolean;
  options: { value: string; label: string }[];
  roots: ReadonlyMap<string, string>;
  onChanged: (next: DesktopIntegration) => void;
}) {
  const view = desktopView(app);
  // `?connect=<name>` (from the menu bar's Connect…, docs/ui/10 §3.5) opens the connect sheet.
  const [sheet, setSheet] = useState<"connect" | "disconnect" | null>(() =>
    new URLSearchParams(location.search).get("connect") === app.name && app.detected && desktopView(app) === "not_connected" ? "connect" : null,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const g = globalSettings();
  const port = g.integrations?.claude_desktop?.port ?? 18435;
  const ready = !!engine.value && ["ready", "busy", "idle_released"].includes(engine.value.state);

  const [seq, setSeq] = useState<"connect" | "restore" | null>(null);
  const [restarting, setRestarting] = useState(false);
  /** Runs connect or restore; progress arrives as `integration.state` steps on the row (api.md §12.1). */
  async function call(path: string, body?: unknown, ok?: string, kind: "connect" | "restore" = "connect"): Promise<DesktopIntegration | null> {
    setBusy(true);
    setError(null);
    setSeq(kind);
    try {
      const next = await api.post<DesktopIntegration>(path, body);
      onChanged(next);
      if (next.step === "failed") {
        setError(next.message ?? t("integrations.desktop.connect_failed"));
        return null;
      }
      setSheet(null);
      setSeq(null);
      if (ok) toast(ok);
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function openApp() {
    if (!local || !app.detected) return;
    try {
      await api.post(`/integrations/${app.name}/open`);
    } catch (e) {
      if (e instanceof ApiError && e.code === "app_not_found") toast(t("integrations.desktop.install_first"));
      else toastError(t("integrations.desktop.open_failed"), e);
    }
  }
  /** RESTART CODEX (09 §4.6): Codex reads config.toml at launch, so re-apply the connection; it quits and reopens. */
  async function restartCodex() {
    setRestarting(false);
    const restored = await call(`/integrations/${app.name}/disconnect`, undefined, undefined, "restore");
    if (!restored) return;
    if (await call(`/integrations/${app.name}/connect`, { confirm_restart: true }, t("integrations.codex_restarted"))) setCodexChanged(false);
  }

  const since = clock(app.connected_at);
  const versionText = app.version ? t("integrations.desktop.detected", { version: app.version }) : t("integrations.desktop.detected_bare");
  const state =
    view === "not_detected" ? (
      <>{t("integrations.desktop.not_detected")}</>
    ) : view === "connected" ? (
      <>
        {versionText} ·{" "}
        <StatusChip
          live
          announce={false}
          label={since ? t("integrations.desktop.connected_since", { time: since }) : t("integrations.desktop.connected")}
        />
      </>
    ) : view === "connecting" ? (
      <>
        {versionText} · <Tag dots>{app.step && app.step !== "done" ? t(`integrations.step.${app.step}` as "integrations.step.done") : t("integrations.desktop.connecting")}</Tag>
      </>
    ) : view === "restoring" ? (
      <>
        {versionText} · <Tag dots>{app.step && app.step !== "done" ? t(`integrations.step.${app.step}` as "integrations.step.done") : t("integrations.desktop.restoring")}</Tag>
      </>
    ) : view === "needs_restore" ? (
      <>
        {versionText} · <Tag>{t("integrations.desktop.needs_restore")}</Tag>
      </>
    ) : (
      <>
        {versionText} · {t("integrations.desktop.not_connected")}
      </>
    );
  const connected = view === "connected" || view === "needs_restore";
  const transient = view === "connecting" || view === "restoring";
  const makeDefault = !!g.integrations?.codex_app?.make_default;
  const [codexChanged, setCodexChanged] = useState(false);

  return (
    <article class="integration-row stack" aria-labelledby={`app-${app.name}`}>
      <div class="integration-head">
        <h3 class="heading integration-title" id={`app-${app.name}`}>
          <span class="label">{formatIndex(index)} — </span>
          {app.label}
        </h3>
        <p class="meta integration-state">
          {state}
          {app.untested_version && (
            <>
              {" · "}
              <Tooltip text={t("integrations.desktop.untested_tip")}>
                <span tabIndex={0}>
                  <Tag tone="mute">{t("integrations.desktop.untested")}</Tag>
                </span>
              </Tooltip>
            </>
          )}
        </p>
      </div>
      <p class="body">{t(`integrations.desktop.lead.${app.name}` as "integrations.desktop.lead.claude-desktop")}</p>
      {view === "not_detected" && <p class="meta">{t("integrations.desktop.looked")}</p>}
      {app.warning && <p class="meta">{app.warning}</p>}
      {connected && !engine.value?.model && <p class="meta">{t("integrations.desktop.engine_stopped")}</p>}
      {seq === "restore" && sheet === null && <StepList name={app.name} kind="restore" step={app.step} />}
      {app.step === "failed" && app.message && sheet === null && (
        <Banner
          tone="critical"
          title={t("integrations.desktop.step_failed")}
          actions={
            <Button size="s" onClick={() => void call(`/integrations/${app.name}/disconnect`, undefined, t("integrations.desktop.restored"), "restore")}>
              {t("integrations.unclean.restore")}
            </Button>
          }
        >
          <code class="mono">{app.message}</code>
        </Banner>
      )}
      <div class="cluster">
        {view === "not_detected" ? (
          <Tooltip text={t("integrations.desktop.install_first")}>
            <Button aria-disabled="true">{t("integrations.desktop.connect")}</Button>
          </Tooltip>
        ) : connected ? (
          <Button variant="solid" disabled={transient || busy} onClick={() => setSheet("disconnect")}>
            {t("integrations.desktop.disconnect")}
          </Button>
        ) : (
          <Button
            variant={ready ? "accent" : "outline"}
            disabled={transient || busy}
            onClick={() => setSheet("connect")}
          >
            {t("integrations.desktop.connect")}
          </Button>
        )}
        <MacOnly local={local}>
          <Button aria-disabled={!local || !app.detected ? "true" : undefined} onClick={() => void openApp()}>
            {t("integrations.desktop.open_app")}
          </Button>
        </MacOnly>
      </div>
      {app.name === "codex-app" && (
        <div class="integration-toggle">
          <Toggle
            checked={makeDefault}
            label={t("integrations.codex_default")}
            describedBy="codex-default-help"
            onChange={(on) =>
              void saveSettings((doc) => {
                const integ = (doc.global.integrations ?? {}) as Record<string, Record<string, unknown>>;
                integ.codex_app = { ...(integ.codex_app ?? {}), make_default: on };
                (doc.global as Record<string, unknown>).integrations = integ;
              })
                .then(() => setCodexChanged(connected))
                .catch((e) => toastError(t("integrations.codex_default_failed"), e))
            }
          />
          <span class="label">{t("integrations.codex_default")}</span>
          <p class="meta" id="codex-default-help">
            {withMono(t("integrations.codex_default_help"), ["~/.codex/config.toml"])}
          </p>
          {codexChanged && (
            <span class="cluster">
              <Tag>{t("integrations.desktop.restart_needed")}</Tag>
              <Button size="s" variant="text" disabled={busy} onClick={() => setRestarting(true)}>
                {t("integrations.codex_restart")}
              </Button>
            </span>
          )}
        </div>
      )}
      {app.name === "claude-desktop" && view !== "not_detected" && (
        <Disclosure summary={t("integrations.slots")}>
          <SlotsEditor options={options} roots={roots} />
        </Disclosure>
      )}
      <Disclosure summary={t("integrations.desktop.changes")}>
        <Table
          columns={[
            { key: "path", label: t("integrations.desktop.file"), render: (r) => <span class="mono">{r.path}</span> },
            { key: "change", label: t("integrations.desktop.change"), render: (r) => t(r.change as "integrations.files.backups") },
          ]}
          rows={DESKTOP_FILES[app.name]}
          rowKey={(r) => r.path}
          caption={t("integrations.desktop.changes")}
        />
        <p class="meta">
          {app.name === "claude-desktop" ? t("integrations.desktop.restore_note") : t("integrations.desktop.codex_note")}
        </p>
        {(connected || app.connected_at) && <RevealBackup name={app.name} local={local} />}
      </Disclosure>

      <Sheet
        open={sheet === "connect"}
        title={t("integrations.desktop.connect_title", { label: app.label })}
        onClose={() => setSheet(null)}
        footer={
          <>
            <Button variant="text" onClick={() => setSheet(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="accent"
              loading={busy}
              onClick={() => void call(`/integrations/${app.name}/connect`, { confirm_restart: true })}
            >
              {app.running ? `${t("integrations.desktop.connect_running")} · ${app.label}` : t("integrations.desktop.connect")}
            </Button>
          </>
        }
      >
        <p class="body">{t("integrations.desktop.connect_body", { label: app.label })}</p>
        <p class="body">
          {app.running
            ? t("integrations.desktop.connect_unsaved", { label: app.label })
            : t("integrations.desktop.connect_closed", { label: app.label })}
        </p>
        {app.name === "claude-desktop" && <p class="mono">{t("integrations.desktop.connect_gateway", { port: String(port) })}</p>}
        {seq === "connect" && <StepList name={app.name} kind="connect" step={app.step} />}
        <Disclosure summary={t("integrations.desktop.changes")}>
          <ul class="integration-files">
            {DESKTOP_FILES[app.name].map((f) => (
              <li key={f.path} class="mono">
                {f.path}
              </li>
            ))}
          </ul>
        </Disclosure>
        {error && (
          <Banner
            tone="critical"
            title={t("integrations.desktop.connect_failed")}
            actions={
              <Button size="s" onClick={() => void call(`/integrations/${app.name}/disconnect`, undefined, t("integrations.desktop.restored"), "restore")}>
                {t("integrations.unclean.restore")}
              </Button>
            }
          >
            <code class="mono">{error}</code>
          </Banner>
        )}
      </Sheet>
      <ConfirmSheet
        open={sheet === "disconnect"}
        title={t("integrations.desktop.disconnect_title", { label: app.label })}
        confirmLabel={t("integrations.desktop.disconnect")}
        busy={busy}
        error={error}
        onClose={() => setSheet(null)}
        onConfirm={() => {
          setSheet(null);
          void call(`/integrations/${app.name}/disconnect`, undefined, t("integrations.desktop.restored"), "restore");
        }}
      >
        <ul>
          <li>
            {t("integrations.desktop.disconnect_body", { since: app.connected_at ? " (" + app.connected_at.slice(0, 16).replace("T", " ") + ")" : "" })}
          </li>
          <li>{t("integrations.desktop.disconnect_restart", { label: app.label })}</li>
        </ul>
      </ConfirmSheet>
      <ConfirmSheet
        open={restarting}
        title={t("integrations.codex_restart_title")}
        confirmLabel={t("integrations.codex_restart")}
        onClose={() => setRestarting(false)}
        onConfirm={() => void restartCodex()}
      >
        <p class="body">{t("integrations.codex_restart_body")}</p>
      </ConfirmSheet>
    </article>
  );
}

/** Connect / restore progress from the manager's steps (api.md §12.1). */
function StepList({ name, kind, step }: { name: DesktopIntegration["name"]; kind: "connect" | "restore"; step: Step | null | undefined }) {
  const steps = stepsFor(name, kind);
  const views = stepViews(steps, step);
  return (
    <ol class="integration-steps" aria-live="polite" data-testid={`steps-${kind}`}>
      {steps.map((st, i) => (
        <li key={st} data-state={views[i]} class={views[i] === "pending" ? "mute" : views[i] === "current" ? "loading-dots" : undefined}>
          {t(`integrations.step.${st}` as "integrations.step.done")}
          {views[i] === "done" ? " ✓" : ""}
        </li>
      ))}
    </ol>
  );
}

function SlotsEditor({ options, roots }: { options: { value: string; label: string }[]; roots: ReadonlyMap<string, string> }) {
  const g = globalSettings();
  const stored = normaliseSlots(g.integrations?.claude_desktop?.slots);
  const [slots, setSlots] = useState<Slots>(stored);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const ACTIVE = "";
  const active = engine.value?.model ?? null;
  const choices = [{ value: ACTIVE, label: t("integrations.slots.active") }, ...options.map((o) => ({ value: o.value, label: o.value }))];
  async function save(next: Slots) {
    setBusy(true);
    try {
      await saveSettings((doc) => {
        const integ = (doc.global.integrations ?? {}) as Record<string, Record<string, unknown>>;
        integ.claude_desktop = { ...(integ.claude_desktop ?? {}), slots: next };
        (doc.global as Record<string, unknown>).integrations = integ;
      });
      setSaved(true);
    } catch (e) {
      toastError(t("integrations.slots.failed"), e);
    } finally {
      setBusy(false);
    }
  }
  const keys = Object.keys(slots).sort((a, b) => {
    const ia = (CLAUDE_SLOTS as readonly string[]).indexOf(a);
    const ib = (CLAUDE_SLOTS as readonly string[]).indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return (
    <div class="stack">
      <p class="body">{t("integrations.slots.lead")}</p>
      <Table
        columns={[
          { key: "slot", label: t("integrations.slots.slot"), render: (k) => <span class="mono">{k}</span> },
          {
            key: "model",
            label: t("integrations.slots.model"),
            render: (k) => (
              <Select
                value={slots[k] ?? ACTIVE}
                options={choices}
                aria-label={`${t("integrations.slots.model")} · ${k}`}
                onChange={(v) => {
                  setSaved(false);
                  setSlots({ ...slots, [k]: v === ACTIVE ? null : v });
                }}
              />
            ),
          },
        ]}
        rows={keys}
        rowKey={(k) => k}
        caption={t("integrations.slots")}
      />
      {slotsSwitchModels(slots, active, roots) && <Banner tone="warn">{t("integrations.slots.switch_warn")}</Banner>}
      <p class="meta">{t("integrations.slots.autoload")}</p>
      <Disclosure summary={t("integrations.slots.preview")}>
        <p class="meta">{t("integrations.slots.preview_note")}</p>
        <CodeBlock code={JSON.stringify(gatewayModels(slots, active), null, 2)} label="GET /v1/models" />
      </Disclosure>
      <div class="cluster" style={{ justifyContent: "space-between" }}>
        <Button
          variant="text"
          onClick={() => {
            const reset = Object.fromEntries(keys.map((k) => [k, null]));
            setSlots(reset);
            void save(reset);
          }}
        >
          {t("integrations.slots.reset")}
        </Button>
        <span class="cluster">
          {saved && <span class="meta" role="status">{t("integrations.slots.saved")}</span>}
          <Button variant="solid" loading={busy} onClick={() => void save(slots)}>
            {t("common.save")}
          </Button>
        </span>
      </div>
    </div>
  );
}

// ---------- SDKs & apps (§5) ----------

function SdkBand({ origin, model, auth }: { origin: string; model: string; auth: boolean }) {
  const [tab, setTab] = useState<SdkTab>("openai_py");
  const code = sdkSnippet(tab, { origin, model, auth });
  const origins = globalSettings().server?.allowed_origins;
  const originState = tauriOriginState(origins);
  const [busy, setBusy] = useState(false);
  const label = (k: SdkTab) => t(`integrations.sdk.${k}` as "integrations.sdk.openai_py");
  return (
    <Section label={t("integrations.sdk")} id="sdk">
      <div class="sdk-wrap">
      <div class="sdk-tabs" role="tablist" aria-label={t("integrations.sdk.tabs")}>
        {SDK_TABS.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            id={`sdk-tab-${k}`}
            aria-selected={tab === k}
            aria-controls="sdk-panel"
            tabIndex={tab === k ? 0 : -1}
            class="navlink nav"
            onClick={() => setTab(k)}
            onKeyDown={(e) => {
              const i = SDK_TABS.indexOf(tab);
              if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                e.preventDefault();
                const next = SDK_TABS[(i + (e.key === "ArrowRight" ? 1 : SDK_TABS.length - 1)) % SDK_TABS.length]!;
                setTab(next);
                requestAnimationFrame(() => document.getElementById(`sdk-tab-${next}`)?.focus());
              }
            }}
          >
            {label(k)}
          </button>
        ))}
      </div>
      <div class="sdk-select">
        <Select
          value={tab}
          options={SDK_TABS.map((k) => ({ value: k, label: label(k) }))}
          onChange={(v) => setTab(v as SdkTab)}
          aria-label={t("integrations.sdk.tabs")}
        />
      </div>
      <div id="sdk-panel" role="tabpanel" aria-labelledby={`sdk-tab-${tab}`} class="stack">
        {code !== null ? (
          <CodeBlock code={code} label={label(tab)} what={label(tab)} />
        ) : tab === "bionic" ? (
          <>
            <p class="body">
              {t("integrations.sdk.bionic_body", { url: origin + "/v1", model })}
            </p>
            <ExternalLink href={BIONIC_GUIDE}>{t("integrations.sdk.bionic_link")}</ExternalLink>
          </>
        ) : (
          <>
            <p class="body">{t("integrations.sdk.jan_body")}</p>
            {originState === "wildcard" ? (
              <p class="meta">{t("integrations.sdk.wildcard")}</p>
            ) : originState === "allowed" ? (
              <p class="cluster">
                <span class="label">{t("integrations.sdk.allowed")}</span>
                <Link href="/settings/server">{t("integrations.sdk.manage")}</Link>
              </p>
            ) : (
              <Button
                loading={busy}
                onClick={() => {
                  setBusy(true);
                  void saveSettings((doc) => {
                    const server = (doc.global.server ?? { host: "127.0.0.1", port: 8000 }) as { allowed_origins?: string[] };
                    server.allowed_origins = withTauriOrigin(server.allowed_origins);
                    (doc.global as Record<string, unknown>).server = server;
                  })
                    .then(() => toast(t("integrations.sdk.allowed_toast")))
                    .catch((e) => toastError(t("integrations.sdk.allow_failed"), e))
                    .finally(() => setBusy(false));
                }}
              >
                {t("integrations.sdk.allow_origin")}
              </Button>
            )}
            <p class="meta mono">{TAURI_ORIGIN}</p>
          </>
        )}
      </div>
      </div>
    </Section>
  );
}
