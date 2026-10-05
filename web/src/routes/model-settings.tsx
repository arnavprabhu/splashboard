import { useEffect, useState } from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import type { ModelFingerprints, ProfileOut, ProfilesView } from "../api/models";
import { Button } from "../components/Button";
import { ConfirmSheet } from "../components/ConfirmSheet";
import { SegmentedControl } from "../components/controls";
import { CopyButton } from "../components/CopyButton";
import { TextInput } from "../components/inputs";
import { KeyValue } from "../components/KeyValue";
import { PageHeader, Section } from "../components/Section";
import { Sheet } from "../components/Sheet";
import { LoadError, Loading } from "../components/States";
import { StatusChip } from "../components/StatusChip";
import { SubNav } from "../components/SubNav";
import { Table } from "../components/Table";
import { toast, toastError } from "../components/Toast";
import { DASH, formatRelativeTime, formatTokens } from "../lib/format";
import { modelIdFromParams } from "../lib/model-id";
import { useApi } from "../lib/use-api";
import { useTitle } from "../lib/title";
import { engine } from "../store";
import { t } from "../strings/settings";
import { settingsApi } from "./settings/api";
import { OverlayEditor, overlayErrors } from "./settings/OverlayEditor";
import { SettingsSave } from "./settings/SaveFlow";
import { SettingField } from "./settings/SettingField";
import { modelForm, schema, loadSchema } from "./settings/state";
import { overlaySummary, profileNameError } from "./settings/validate";
import { MODELS_TABS } from "./tabs";

type Sec = "overrides" | "sampling" | "profiles" | "info";
const SECTIONS: readonly Sec[] = ["overrides", "sampling", "profiles", "info"];

function shortName(id: string): string {
  return id.slice(id.indexOf("/") + 1);
}

export default function ModelSettingsPage({ params = {} }: { params?: Record<string, string | undefined> }) {
  const id = modelIdFromParams(params);
  const form = modelForm(id);
  const [query, setQuery] = useSearchParams();
  const raw = query.get("section");
  const section: Sec = (SECTIONS as readonly string[]).includes(raw ?? "") ? (raw as Sec) : location.hash === "#profiles" ? "profiles" : "overrides";
  const setSection = (s: Sec) =>
    setQuery(
      (p) => {
        const n = new URLSearchParams(p);
        n.set("section", s);
        return n;
      },
      { replace: true },
    );
  const profiles = useApi((s) => settingsApi.profiles(id, s), [id]);
  const detail = useApi((s) => settingsApi.model(id, s), [id]);
  // RAM for the Memory ceiling's "= 40 GB · 63% of 64 GB" line (docs/ui/05).
  const system = useApi(settingsApi.system);
  useEffect(() => {
    void form.load();
    void loadSchema();
  }, [id]);
  const e = engine.value;
  const isActive = e?.model === id;
  useTitle(`${shortName(id)} · ${t("settings.page_title")}`);

  return (
    <>
      <SubNav items={MODELS_TABS} label="Models" exact />
      <PageHeader
        title={`${shortName(id)}.`}
        size="m"
        meta={
          <span class="cluster ms-meta">
            <span class="mono">{id}</span>
            <span>· {t("settings.model.subtitle")}</span>
            {isActive && <StatusChip state={e?.state} announce={false} />}
            <Link href="/models">{t("settings.model.back")}</Link>
          </span>
        }
        actions={
          <SegmentedControl
            label={t("settings.model.sections")}
            value={section}
            options={SECTIONS.map((s) => ({ value: s, label: t(`settings.model.section.${s}`) }))}
            onChange={setSection}
          />
        }
      />
      {section === "overrides" && (
        <Section label={t("settings.model.section.overrides")} meta={t("settings.model.overrides_meta")}>
          {!!form.loadError.value && <LoadError thing={t("settings.model.thing")} error={form.loadError.value} onRetry={() => void form.load(true)} />}
          <div class="stack">
            {(schema.value?.fields ?? [])
              .filter((f) => f.scope !== "G")
              .map((field) => (
                <SettingField
                  key={field.key}
                  field={field}
                  form={form}
                  model={id}
                  memoryBytes={system.data?.memory_bytes ?? null}
                  disabledReason={field.disabled_for_legacy && detail.data?.legacy ? t("settings.legacy") : null}
                />
              ))}
          </div>
        </Section>
      )}
      {section === "sampling" && <SamplingDefaults id={id} view={profiles.data} error={profiles.error} onSaved={profiles.reload} />}
      {section === "profiles" && <Profiles id={id} view={profiles.data} error={profiles.error} onRetry={profiles.reload} onSaved={(v) => profiles.setData(v)} />}
      {section === "info" && (
        <Section label={t("settings.model.section.info")}>
          {detail.error ? (
            <LoadError thing={t("settings.model.i.thing")} error={detail.error} onRetry={detail.reload} />
          ) : !detail.data ? (
            <Loading />
          ) : (
            <KeyValue
              label={t("settings.model.section.info")}
              items={[
                { key: "tpl", label: t("settings.model.i.template"), value: detail.data.chat_template_mode ?? t("settings.model.i.load_once") },
                { key: "rev", label: t("settings.model.i.revision"), value: <span class="mono">{detail.data.commit ?? detail.data.revision ?? DASH}</span> },
                {
                  key: "path",
                  label: t("settings.model.i.path"),
                  value: detail.data.link_path ? (
                    <span class="cluster">
                      <span class="mono ms-path">{detail.data.link_path}</span>
                      <CopyButton text={detail.data.link_path} />
                    </span>
                  ) : (
                    DASH
                  ),
                },
                { key: "last", label: t("settings.model.i.last"), value: detail.data.last_used_at ? formatRelativeTime(detail.data.last_used_at) : t("settings.model.i.load_once") },
                ...fingerprintRows(detail.data.fingerprints ?? null),
              ]}
            />
          )}
        </Section>
      )}
      <SettingsSave form={form} saveText={isActive && form.plan.value.restart ? t("settings.model.save_reload") : undefined} />
    </>
  );
}

/** Identity from this model's last load (`GET /models/{id}.fingerprints`, S3-21); "load once" before that. */
export function fingerprintRows(f: ModelFingerprints | null) {
  const v = (x: string | null | undefined) => (x ? <span class="mono ms-path">{x}</span> : t("settings.model.i.load_once"));
  const kv = f?.kv_format ? `${f.kv_format}${f.kv_quantization ? ` / ${f.kv_quantization}` : ""}` : null;
  return [
    { key: "build", label: t("settings.model.i.build"), value: v(f?.build_id) },
    { key: "layout", label: t("settings.model.i.layout"), value: v(f?.loaded_model_layout_sha256) },
    { key: "target", label: t("settings.model.i.target"), value: v(f?.target_model_sha256) },
    { key: "kv", label: t("settings.model.i.kv"), value: v(kv) },
    { key: "ctx", label: t("settings.model.i.context"), value: f?.max_context ? formatTokens(f.max_context) : t("settings.model.i.load_once") },
    { key: "recorded", label: t("settings.model.i.recorded"), value: f?.recorded_at ? formatRelativeTime(f.recorded_at) : t("settings.model.i.load_once") },
  ];
}

function SamplingDefaults({ id, view, error, onSaved }: { id: string; view: ProfilesView | null; error: unknown; onSaved: () => void }) {
  const [value, setValue] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (view) setValue({ ...(view.sampling_defaults ?? {}) });
  }, [view]);
  if (error) return <Section label={t("settings.model.section.sampling")}><LoadError thing={t("settings.model.thing_profiles")} error={error} onRetry={onSaved} /></Section>;
  if (!value) return <Section label={t("settings.model.section.sampling")}><Loading /></Section>;
  const errs = overlayErrors(value);
  return (
    <Section label={t("settings.model.section.sampling")} meta={t("settings.model.sampling_meta")}>
      <div class="stack">
        <OverlayEditor value={value} onChange={setValue} idPrefix="sd" />
        <div class="cluster">
          <Button
            variant="solid"
            loading={busy}
            disabled={Object.keys(errs).length > 0}
            onClick={() => {
              setBusy(true);
              void settingsApi
                .saveProfiles(id, { sampling_defaults: value as never })
                .then(() => {
                  toast(t("settings.model.sampling_saved"));
                  onSaved();
                })
                .catch((err) => toastError(t("settings.model.p.failed"), err))
                .finally(() => setBusy(false));
            }}
          >
            {t("settings.model.save_sampling")}
          </Button>
        </div>
      </div>
    </Section>
  );
}

function Profiles({ id, view, error, onRetry, onSaved }: { id: string; view: ProfilesView | null; error: unknown; onRetry: () => void; onSaved: (v: ProfilesView) => void }) {
  const [editing, setEditing] = useState<{ original: string | null; name: string; overlay: Record<string, unknown> } | null>(null);
  const [deleting, setDeleting] = useState<ProfileOut | null>(null);
  const [busy, setBusy] = useState(false);
  if (error) return <Section label={t("settings.model.section.profiles")}><LoadError thing={t("settings.model.thing_profiles")} error={error} onRetry={onRetry} /></Section>;
  if (!view) return <Section label={t("settings.model.section.profiles")}><Loading /></Section>;
  const names = view.profiles.map((p) => p.name);
  async function save(profiles: Record<string, Record<string, unknown> | null>) {
    setBusy(true);
    try {
      const next = await settingsApi.saveProfiles(id, { profiles: profiles as never });
      onSaved(next);
      toast(t("settings.model.p.saved", { n: next.profiles.length }));
      return true;
    } catch (err) {
      toastError(t("settings.model.p.failed"), err);
      return false;
    } finally {
      setBusy(false);
    }
  }
  const nameErr = editing
    ? editing.name === "default" && editing.original !== "default"
      ? t("settings.model.p.reserved")
      : profileNameError(editing.name, names, editing.original)
    : null;
  const overlayErr = editing ? overlayErrors(editing.overlay) : {};
  return (
    <Section label={t("settings.model.section.profiles")} meta={t("settings.model.profiles_meta")}>
      <div class="stack">
        <Table
          caption={t("settings.model.section.profiles")}
          rows={view.profiles}
          rowKey={(p) => p.name}
          columns={[
            { key: "name", label: t("settings.model.p.profile"), render: (p) => <span class="mono">{p.name}</span> },
            { key: "id", label: t("settings.model.p.id"), render: (p) => <span class="mono ms-path">{p.id}</span> },
            { key: "fields", label: t("settings.model.p.fields"), render: (p) => <span class="meta">{overlaySummary(p.overlay) || t("settings.model.p.none")}</span> },
            {
              key: "actions",
              label: t("settings.model.p.actions"),
              render: (p) => (
                <span class="cluster nowrap">
                  <CopyButton text={p.id} label={t("settings.model.p.copy_id")} what={p.id} />
                  {p.name !== "default" && (
                    <>
                      <Button size="s" variant="text" onClick={() => setEditing({ original: p.name, name: p.name, overlay: { ...p.overlay } })}>
                        {t("settings.model.p.edit")}
                      </Button>
                      {p.builtin && p.modified ? (
                        <Button size="s" variant="text" onClick={() => void save({ [p.name]: {} })}>
                          {t("settings.model.p.reset")}
                        </Button>
                      ) : null}
                      <Button size="s" variant="text" onClick={() => setDeleting(p)}>
                        {t("settings.model.p.delete")}
                      </Button>
                    </>
                  )}
                </span>
              ),
            },
          ]}
        />
        <div class="cluster">
          <Button onClick={() => setEditing({ original: null, name: "", overlay: {} })}>{t("settings.model.p.new")}</Button>
        </div>
      </div>
      <Sheet
        open={!!editing}
        title={editing?.original ? t("settings.model.p.edit_title", { name: editing.original }) : t("settings.model.p.new_title")}
        onClose={() => setEditing(null)}
        busy={busy}
        footer={
          <>
            <Button variant="text" onClick={() => setEditing(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="solid"
              loading={busy}
              disabled={!!nameErr || !editing?.name || Object.keys(overlayErr).length > 0}
              onClick={async () => {
                if (!editing) return;
                const patch: Record<string, Record<string, unknown> | null> = { [editing.name]: editing.overlay };
                if (editing.original && editing.original !== editing.name) patch[editing.original] = null;
                if (await save(patch)) setEditing(null);
              }}
            >
              {t("common.save")}
            </Button>
          </>
        }
      >
        {editing && (
          <div class="stack">
            <label class="stack">
              <span class="label">{t("settings.model.p.name")}</span>
              <TextInput class="mono" value={editing.name} invalid={!!nameErr} disabled={!!editing.original && view.profiles.find((p) => p.name === editing.original)?.builtin} onChange={(v) => setEditing({ ...editing, name: v })} />
              {nameErr && editing.name && <span class="field-error">{nameErr}</span>}
            </label>
            <p class="meta">{t("settings.model.p.overlay_meta", { id: id + ":" + (editing.name || "…") })}</p>
            <OverlayEditor value={editing.overlay} onChange={(overlay) => setEditing({ ...editing, overlay })} idPrefix="pf" />
          </div>
        )}
      </Sheet>
      <ConfirmSheet
        open={!!deleting}
        title={t("settings.model.p.delete_title")}
        confirmLabel={t("settings.model.p.delete")}
        busy={busy}
        onClose={() => setDeleting(null)}
        onConfirm={async () => {
          if (deleting && (await save({ [deleting.name]: null }))) setDeleting(null);
        }}
      >
        <p class="body mono">{deleting ? t("settings.model.p.delete_body", { id: deleting.id }) : ""}</p>
      </ConfirmSheet>
    </Section>
  );
}
