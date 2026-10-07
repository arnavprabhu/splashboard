import { useEffect, useMemo, useState } from "preact/hooks";
import { Link, Redirect, useLocation } from "wouter-preact";
import type { SchemaField } from "../api/models";
import { ApiError } from "../api/client";
import {
  Banner,
  Button,
  ConfirmSheet,
  ExternalLink,
  LoadError,
  PageHeader,
  SearchInput,
  Sheet,
  Section,
  Select,
  Toggle,
  toast,
  toastError,
} from "../components";
import { formatBytes } from "../lib/format";
import { useApi } from "../lib/use-api";
import { useTitle } from "../lib/title";
import { t } from "../strings/settings";
import { SETTINGS_SECTIONS as SECTIONS_RAW, type SettingsSlug } from "./tabs";

/** Section names from the string table (labels in tabs.ts stay as fallbacks for other modules). */
const SETTINGS_SECTIONS = SECTIONS_RAW.map((s) => ({ slug: s.slug, label: t(`settings.section.${s.slug}` as "settings.section.server") }));
import { SettingField } from "./settings/SettingField";
import { globalForm, schema, schemaError, loadSchema, type SettingsForm } from "./settings/state";
import { settingsApi } from "./settings/api";
import { isLoopback, SECTION_TO_SLUG, SLUG_TO_SECTION } from "./settings/form";
import { SettingsSave } from "./settings/SaveFlow";
import { ApiKeyField, HfTokenField } from "./settings/Secrets";
import { DataPrivacy } from "./settings/DataPrivacy";
import { About } from "./settings/About";
import { ExtraFlags } from "./settings/ExtraFlags";
import { McpServers } from "./settings/McpServers";

export { SettingsSave };

const NOTIFICATION_SETTINGS = "x-apple.systempreferences:com.apple.Notifications-Settings.extension";
const SECRET_KEYS = new Set(["security.api_key", "hf.token"]);

/** Fields matching a search across label, key, flag and help (docs/ui/05 §1.1). */
export function searchFields(fields: readonly SchemaField[], query: string): SchemaField[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return fields.filter((f) =>
    [f.label, f.key, f.flag ?? "", f.env ?? "", f.help].some((s) => s.toLowerCase().includes(q)),
  );
}

/** `advanced.crash_trace` with the warning sheet when it turns on (docs/ui/05 §3.16). */
function CrashTraceField({ field, form }: { field: SchemaField; form: SettingsForm }) {
  const [sheet, setSheet] = useState<"on" | "off" | null>(null);
  const traces = useApi(settingsApi.traces);
  const list = traces.data?.traces ?? [];
  const total = list.reduce((n, x) => n + (x.size_bytes ?? 0), 0);
  const ref = { key: field.key, model: null };
  const [busy, setBusy] = useState(false);
  return (
    <>
      <SettingField
        field={field}
        form={form}
        control={(ids, value, set, disabled) => (
          <Toggle
            id={ids.id}
            describedBy={ids.describedBy}
            checked={Boolean(value)}
            disabled={disabled}
            onChange={(on) => {
              if (on) setSheet("on");
              else if (list.length > 0) {
                set(false);
                setSheet("off");
              } else set(false);
            }}
          />
        )}
      />
      <ConfirmSheet
        open={sheet === "on"}
        title={t("settings.crash.title")}
        confirmLabel={t("settings.crash.turn_on")}
        onClose={() => setSheet(null)}
        onConfirm={() => {
          form.set(ref, true);
          setSheet(null);
        }}
      >
        <p class="body">{t("settings.crash.body")}</p>
      </ConfirmSheet>
      <ConfirmSheet
        open={sheet === "off"}
        title={t("settings.crash.off_title")}
        confirmLabel={t("settings.crash.off_delete", { n: list.length, size: formatBytes(total) })}
        busy={busy}
        onClose={() => setSheet(null)}
        onConfirm={async () => {
          setBusy(true);
          try {
            await settingsApi.clearData("traces");
            toast(t("settings.data.cleared", { what: t("settings.crash.what"), size: formatBytes(total) }));
            await traces.reload();
            setSheet(null);
          } catch (e) {
            toastError(t("settings.data.failed", { what: t("settings.crash.what") }), e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <p class="body">{t("settings.crash.off_body", { n: list.length, size: formatBytes(total) })}</p>
        <Button variant="text" onClick={() => setSheet(null)}>
          {t("settings.crash.keep")}
        </Button>
      </ConfirmSheet>
    </>
  );
}

function CommandPreview() {
  const models = useApi(settingsApi.models);
  const model = models.data?.models[0]?.id ?? null;
  const preview = useApi((s) => settingsApi.launchPreview(model!, s), [model], !!model);
  return (
    <details class="disclosure field">
      <summary class="label">
        <span class="disclosure-glyph" aria-hidden="true">
          ▸
        </span>
        {t("settings.preview")}
      </summary>
      <div class="disclosure-body stack">
        <p class="meta">{t("settings.preview_help")}</p>
        {!model ? (
          <p class="meta">{t("settings.preview_none")}</p>
        ) : preview.error ? (
          <LoadError thing={t("settings.preview_thing")} error={preview.error} onRetry={preview.reload} />
        ) : (
          <pre class="mono codeblock-pre">{preview.data?.error ?? preview.data?.display ?? ""}</pre>
        )}
      </div>
    </details>
  );
}

/** "Reset all settings" (docs/ui/05 §3.16, G3): typed RESET, then `POST /settings/reset`; lists what was kept. */
function ResetSettings({ form }: { form: SettingsForm }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyEngine, setBusyEngine] = useState(false);
  const [kept, setKept] = useState<string[] | null>(null);
  async function run(force: boolean) {
    setBusy(true);
    setError(null);
    try {
      const res = await settingsApi.resetSettings(force);
      setKept(res.kept ?? []);
      setBusyEngine(false);
      await form.load(true);
      toast(res.engine_restarted ? t("settings.reset.done_restart") : t("settings.reset.done"));
    } catch (err) {
      if (err instanceof ApiError && err.code === "model_switch_busy") setBusyEngine(true);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="field" data-key="settings.reset" id="settings.reset">
      <span class="label">{t("settings.reset.title")}</span>
      <p class="field-help">{t("settings.reset.help")}</p>
      <div class="cluster">
        <Button size="s" onClick={() => (setKept(null), setError(null), setBusyEngine(false), setOpen(true))}>
          {t("settings.reset.button")}
        </Button>
      </div>
      {kept === null ? (
        <ConfirmSheet
          open={open}
          title={t("settings.reset.sheet_title")}
          confirmLabel={busyEngine ? t("settings.reset.force") : t("settings.reset.button")}
          typedWord="RESET"
          important={busyEngine}
          busy={busy}
          error={error}
          onClose={() => setOpen(false)}
          onConfirm={() => run(busyEngine)}
        >
          <p class="body">{t("settings.reset.body")}</p>
          {busyEngine && <p class="field-error">{t("settings.reset.busy")}</p>}
        </ConfirmSheet>
      ) : (
        <Sheet open={open} title={t("settings.reset.result_title")} onClose={() => setOpen(false)} footer={<Button variant="solid" onClick={() => setOpen(false)}>{t("common.close")}</Button>}>
          <p class="body">{t("settings.reset.kept")}</p>
          <ul class="mono" data-testid="reset-kept">
            {kept.map((k) => (
              <li key={k}>{k}</li>
            ))}
          </ul>
        </Sheet>
      )}
    </div>
  );
}

/** D44: deleting a model can remove files from the user's own Hugging Face cache. */
function SharedHfCacheNotice() {
  const storage = useApi((s) => settingsApi.storage(s));
  const info = storage.data;
  if (!info?.models_shared_with_hf_cache) return null;
  return (
    <Banner tone="warn" title={t("settings.storage.shared_title")}>
      <span data-testid="storage-shared-hf">
        {t("settings.storage.shared_body", { path: info.hf_cache_path ?? info.models_dir })}
      </span>
    </Banner>
  );
}

/**
 * Settings → Security page text (D58, SPEC §17.1): how sign-in works, and what a LAN bind over
 * plain HTTP exposes. The field help itself comes from the manager's metadata.
 */
function SecurityNotes({ form }: { form: SettingsForm }) {
  const host = form.value({ key: "server.host", model: null });
  const lan = typeof host === "string" && !isLoopback(host);
  return (
    <div class="stack" data-testid="security-notes">
      <div class="field">
        <span class="label">{t("settings.security.signin_label")}</span>
        <div class="field-body stack">
          <p class="body">{t("settings.security.signin_body")}</p>
          <p class="body">{t("settings.security.signin_off_body")}</p>
        </div>
      </div>
      <div class="field" data-testid="security-lan-tls">
        <span class="label">{t("settings.security.lan_label")}</span>
        <div class="field-body stack">
          {lan ? (
            <Banner tone="warn" title={t("settings.security.lan_warn_title")}>
              {t("settings.security.lan_body")}
            </Banner>
          ) : (
            <p class="body">{t("settings.security.lan_body")}</p>
          )}
          <p class="body mute">{t("settings.security.lan_advice")}</p>
        </div>
      </div>
    </div>
  );
}

function SectionExtras({ slug, form }: { slug: SettingsSlug; form: SettingsForm }) {
  switch (slug) {
    case "security":
      return (
        <>
          <ApiKeyField form={form} />
          <SecurityNotes form={form} />
        </>
      );
    case "hf":
      return <HfTokenField form={form} />;
    case "notifications":
      return (
        <div class="stack">
          <p class="meta">{t("settings.notifications.footer")}</p>
          <ExternalLink href={NOTIFICATION_SETTINGS}>{t("settings.notifications.open")}</ExternalLink>
        </div>
      );
    case "advanced":
      return (
        <>
          <CommandPreview />
          <ResetSettings form={form} />
        </>
      );
    case "data":
      return <DataPrivacy />;
    case "about":
      return <About />;
    default:
      return null;
  }
}

/** Memory ceiling with this Mac's RAM, for "= 40 GB · 63% of 64 GB" (docs/ui/05). */
function MemoryField({ field, form, models, hash }: { field: SchemaField; form: SettingsForm; models?: string[] | undefined; hash: string }) {
  const system = useApi(settingsApi.system);
  return <SettingField field={field} form={form} models={models} memoryBytes={system.data?.memory_bytes ?? null} highlight={hash === field.key} />;
}

function FieldFor({ field, form, models, hash }: { field: SchemaField; form: SettingsForm; models?: string[] | undefined; hash: string }) {
  if (field.key === "advanced.crash_trace") return <CrashTraceField field={field} form={form} />;
  if (field.key === "chat.mcp_servers") return <McpServers />;
  if (field.key === "serve.max_memory") return <MemoryField field={field} form={form} models={models} hash={hash} />;
  if (field.key === "engine.extra_flags")
    return <ExtraFlags form={form} options={schema.value?.engine_options.unknown ?? []} version={schema.value?.engine_options.version ?? null} />;
  return <SettingField field={field} form={form} models={models} highlight={hash === field.key} />;
}

export default function SettingsPage({ params }: { params?: { section?: string } }) {
  const form = globalForm;
  const found = SETTINGS_SECTIONS.find((s) => s.slug === params?.section);
  const [query, setQuery] = useState("");
  const [, navigate] = useLocation();
  useTitle(found ? `${found.label} · ${t("settings.page_title")}` : t("settings.page_title"));
  const models = useApi(settingsApi.models);
  const [hash, setHash] = useState(() => decodeURIComponent(location.hash.slice(1)));
  useEffect(() => {
    void form.load();
    void loadSchema();
    const onHash = () => setHash(decodeURIComponent(location.hash.slice(1)));
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  // Closing the tab with unsaved changes asks first; in-app navigation keeps the edits in the store.
  useEffect(() => {
    const onLeave = (e: BeforeUnloadEvent) => {
      if (globalForm.dirty.value.length > 0) e.preventDefault();
    };
    addEventListener("beforeunload", onLeave);
    return () => removeEventListener("beforeunload", onLeave);
  }, []);
  useEffect(() => {
    if (!hash || !schema.value) return;
    requestAnimationFrame(() => document.getElementById(hash)?.scrollIntoView({ block: "center" }));
  }, [hash, schema.value, params?.section]);

  const allFields = useMemo(
    () => (schema.value?.fields ?? []).filter((f) => f.scope !== "M" && !SECRET_KEYS.has(f.key) && f.storage !== "keychain"),
    [schema.value],
  );
  const results = useMemo(() => searchFields(allFields, query), [allFields, query]);

  if (!found) return <Redirect to="/settings/server" replace />;
  const current = found;
  const index = SETTINGS_SECTIONS.findIndex((s) => s.slug === current.slug);
  const prev = SETTINGS_SECTIONS[index - 1];
  const next = SETTINGS_SECTIONS[index + 1];
  const fields = allFields.filter((f) => f.section === SLUG_TO_SECTION[current.slug]);
  const modelIds = models.data?.models.map((m) => m.id);
  const sectionLabel = (f: SchemaField) =>
    SETTINGS_SECTIONS.find((s) => s.slug === SECTION_TO_SLUG[f.section])?.label ?? f.section;

  return (
    <>
      <PageHeader
        title={t("settings.title")}
        meta={
          <span>
            {t("settings.meta")} <Link href="/models">↗</Link>
          </span>
        }
        actions={<SearchInput value={query} onChange={setQuery} label={t("settings.search")} primary />}
      />
      {form.envelope.value?.read_only && (
        <Section tight>
          <p class="field-error">{t("settings.read_only")}</p>
        </Section>
      )}
      {query.trim() ? (
        <Section label={t("settings.search_results", { n: results.length })}>
          {!!schemaError.value && <LoadError thing={t("settings.thing_schema")} error={schemaError.value} onRetry={() => void loadSchema(true)} />}
          {results.length === 0 ? (
            <p class="body">{t("settings.search_none", { q: query.trim() })}</p>
          ) : (
            <div class="stack">
              {results.map((f) => (
                <div key={f.key} class="settings-result">
                  <Link href={`/settings/${SECTION_TO_SLUG[f.section] ?? "server"}#${f.key}`} class="meta">
                    {sectionLabel(f)}
                  </Link>
                  <FieldFor field={f} form={form} models={modelIds} hash={hash} />
                </div>
              ))}
            </div>
          )}
        </Section>
      ) : (
        <section class="band settings-band">
          <div class="label-row">
            <nav class="label-row-label settings-nav-col" aria-label={t("settings.sections")}>
              <ul class="settings-nav">
                {SETTINGS_SECTIONS.map((s) => (
                  <li key={s.slug}>
                    <Link
                      href={`/settings/${s.slug}`}
                      class="navlink nav"
                      aria-current={s.slug === current.slug ? "page" : undefined}
                    >
                      {s.slug === current.slug && (
                        <span aria-hidden="true" class="settings-current">
                          ●{" "}
                        </span>
                      )}
                      {s.label}
                    </Link>
                  </li>
                ))}
              </ul>
              <label class="settings-select">
                <span class="label">{t("settings.section_select")}</span>
                <Select
                  value={current.slug}
                  options={SETTINGS_SECTIONS.map((s) => ({ value: s.slug, label: s.label }))}
                  onChange={(v) => navigate(`/settings/${v}`)}
                />
              </label>
            </nav>
            <div class="label-row-content stack">
              <h2 class="heading">{current.label}.</h2>
              <p class="body">{t(`settings.desc.${current.slug}` as "settings.desc.server")}</p>
              {!!form.loadError.value && (
                <LoadError thing={t("settings.thing")} error={form.loadError.value} onRetry={() => void form.load(true)} />
              )}
              {!!schemaError.value && (
                <LoadError thing={t("settings.thing_schema")} error={schemaError.value} onRetry={() => void loadSchema(true)} />
              )}
              {form.envelope.value?.load_warnings.map((w) => (
                <p role="alert" class="field-error" key={w}>
                  {w}
                </p>
              ))}
              {current.slug === "storage" && <SharedHfCacheNotice />}
              {fields.map((field) => (
                <FieldFor key={field.key} field={field} form={form} models={modelIds} hash={hash} />
              ))}
              <SectionExtras slug={current.slug} form={form} />
              {fields.length === 0 && !["security", "hf", "data", "about", "notifications"].includes(current.slug) && schema.value && (
                <p class="meta">{t("settings.no_fields")}</p>
              )}
              <div class="cluster settings-pager">
                {prev ? (
                  <Link href={`/settings/${prev.slug}`} class="btn" data-variant="text">
                    {t("settings.prev", { label: prev.label })}
                  </Link>
                ) : (
                  <span />
                )}
                {next && (
                  <Link href={`/settings/${next.slug}`} class="btn" data-variant="text">
                    {t("settings.next", { label: next.label })}
                  </Link>
                )}
              </div>
            </div>
          </div>
        </section>
      )}
      <SettingsSave form={form} />
    </>
  );
}
