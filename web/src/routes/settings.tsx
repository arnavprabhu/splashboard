import { useEffect, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import {
  Button,
  ConfirmSheet,
  CopyButton,
  LoadError,
  PageHeader,
  Section,
  StickySaveBar,
  toast,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import { formatBytes } from "../lib/format";
import { engine } from "../store";
import { SETTINGS_SECTIONS } from "./tabs";
import { SettingField } from "./settings/SettingField";
import { globalForm, schema, type SettingsForm } from "./settings/state";
import { settingsApi } from "./settings/api";
import { SLUG_TO_SECTION, rebindUrl } from "./settings/form";

export function SettingsSave({ form }: { form: SettingsForm }) {
  const [confirm, setConfirm] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const restart = form.plan.value.restart && !!engine.value?.model;
  async function save(restartNow: boolean) {
    const rebind = form.plan.value.rebind;
    try {
      await form.save();
      if (restartNow) await settingsApi.restartEngine();
      setConfirm(false);
      setFailure(null);
      toast("Settings saved.");
      if (rebind)
        location.assign(rebindUrl(rebind, location, "/admin/settings/server"));
    } catch (err) {
      setFailure(String(err));
      toastError("Could not save settings.", err);
    }
  }
  return (
    <>
      <StickySaveBar
        changes={form.dirty.value.length}
        restart={restart}
        saving={form.saving.value}
        invalid={form.invalid.value}
        onSave={() => (restart ? setConfirm(true) : void save(false))}
        onDiscard={form.discard}
      />
      <ConfirmSheet
        open={confirm}
        title="Restart the engine."
        confirmLabel="Save & restart"
        onConfirm={() => save(true)}
        onClose={() => setConfirm(false)}
        busy={form.saving.value}
        error={failure}
        important
      >
        <p>
          Restarting ends active requests and reloads the current model with
          these settings.
        </p>
        <Button onClick={() => void save(false)}>Save, restart later</Button>
      </ConfirmSheet>
    </>
  );
}

function SecretControls({ section }: { section: string }) {
  const [key, setKey] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [result, setResult] = useState("");
  const [action, setAction] = useState<"generate" | "delete" | null>(null);
  async function run(fn: () => Promise<unknown>) {
    try {
      await fn();
      await globalForm.load(true);
    } catch (e) {
      toastError("Secret operation failed.", e);
    }
  }
  if (section === "security")
    return (
      <Section label="API key">
        <p>
          {globalForm.envelope.value?.secrets.api_key_set
            ? "An API key is stored in Keychain."
            : "No API key is set."}
        </p>
        <div class="cluster">
          <Button
            onClick={() =>
              void run(async () =>
                setKey((await settingsApi.revealApiKey()).key),
              )
            }
          >
            Reveal
          </Button>
          <Button onClick={() => setAction("generate")}>
            Generate new key
          </Button>
          <Button onClick={() => setAction("delete")}>Delete key</Button>
        </div>
        {key && (
          <div class="cluster">
            <code>{key}</code>
            <CopyButton text={key} />
            <Button onClick={() => setKey(null)}>Hide</Button>
          </div>
        )}
        <ConfirmSheet
          open={action !== null}
          title={
            action === "generate"
              ? "Replace the API key."
              : "Delete the API key."
          }
          confirmLabel="Confirm"
          onClose={() => setAction(null)}
          onConfirm={() =>
            run(async () => {
              if (action === "generate")
                setKey((await settingsApi.generateApiKey()).key);
              else {
                await settingsApi.deleteApiKey();
                setKey(null);
              }
              setAction(null);
            })
          }
        >
          <p>Clients using the current key will need to be updated.</p>
        </ConfirmSheet>
      </Section>
    );
  return (
    <Section label="Hugging Face token">
      <p>
        The override is stored in Keychain. Without an override, Splash uses
        your Hugging Face login.
      </p>
      <label>
        Token
        <input
          type="password"
          autocomplete="off"
          value={token}
          onInput={(e) => setToken(e.currentTarget.value)}
        />
      </label>
      <div class="cluster">
        <Button
          disabled={!token}
          onClick={() =>
            void run(async () => {
              await settingsApi.setHfToken(token);
              setToken("");
            })
          }
        >
          Save token
        </Button>
        <Button
          onClick={() =>
            void run(async () =>
              setResult(
                JSON.stringify(
                  await settingsApi.testHfToken(token || undefined),
                ),
              ),
            )
          }
        >
          Test token
        </Button>
        <Button onClick={() => void run(settingsApi.deleteHfToken)}>
          Remove override
        </Button>
      </div>
      <p role="status">{result}</p>
    </Section>
  );
}

function DataControls() {
  const sizes = useApi(settingsApi.dataSizes);
  const [target, setTarget] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function clear() {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      await settingsApi.clearData(target);
      setTarget(null);
      await sizes.reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Section label="Local data">
      {!!sizes.error && (
        <LoadError
          thing="data sizes"
          error={sizes.error}
          onRetry={sizes.reload}
        />
      )}
      {sizes.data?.targets.map((t) => (
        <div class="listrow" key={t.target}>
          <span>
            {t.label} · {formatBytes(t.bytes)}
            {t.note && <small> · {t.note}</small>}
          </span>
          <Button onClick={() => setTarget(t.target)}>Clear</Button>
        </div>
      ))}
      <ConfirmSheet
        open={!!target}
        title="Clear local data."
        confirmLabel="Clear"
        typedWord="CLEAR"
        busy={busy}
        error={error}
        onClose={() => setTarget(null)}
        onConfirm={clear}
      >
        <p>
          Delete {target?.replace(/_/g, " ")} permanently. Clearing models or KV
          cache can stop the engine; clearing Responses storage reloads it.
        </p>
      </ConfirmSheet>
    </Section>
  );
}

export function JsonSetting({
  form,
  settingKey,
  label,
}: {
  form: SettingsForm;
  settingKey: string;
  label: string;
}) {
  const ref = { key: settingKey, model: form.model };
  const [text, setText] = useState(
    JSON.stringify(
      form.value(ref) ?? (settingKey.endsWith("flags") ? [] : {}),
      null,
      2,
    ),
  );
  const [error, setError] = useState("");
  return (
    <label class="stack">
      {label}
      <textarea
        class="mono"
        rows={10}
        value={text}
        onInput={(e) => {
          const value = e.currentTarget.value;
          setText(value);
          try {
            form.set(ref, JSON.parse(value));
            setError("");
          } catch {
            setError("Enter valid JSON.");
          }
        }}
      />
      {error && <span role="alert">{error}</span>}
    </label>
  );
}

export default function SettingsPage({
  params,
}: {
  params?: { section?: string };
}) {
  const form = globalForm;
  const current =
    SETTINGS_SECTIONS.find((s) => s.slug === params?.section) ??
    SETTINGS_SECTIONS[0];
  const models = useApi(settingsApi.models);
  useEffect(() => {
    void form.load();
  }, []);
  const fields =
    schema.value?.fields.filter(
      (f) => f.section === SLUG_TO_SECTION[current.slug] && f.scope !== "M",
    ) ?? [];
  return (
    <>
      <PageHeader title="Settings." />
      <section class="band">
        <div class="label-row">
          <nav class="label-row-label" aria-label="Settings sections">
            <ul class="stack settings-nav">
              {SETTINGS_SECTIONS.map((s) => (
                <li key={s.slug}>
                  <Link
                    href={`/settings/${s.slug}`}
                    class="navlink nav"
                    aria-current={s.slug === current.slug ? "page" : undefined}
                  >
                    {s.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <div class="label-row-content stack">
            <h2 class="heading">{current.label}</h2>
            {!!form.loadError.value && (
              <LoadError
                thing="settings"
                error={form.loadError.value}
                onRetry={() => void form.load(true)}
              />
            )}
            {form.envelope.value?.load_warnings.map((w) => (
              <p role="alert" key={w}>
                {w}
              </p>
            ))}
            {fields.map((field) =>
              ["chat.mcp_servers", "engine.extra_flags"].includes(field.key) ? (
                <JsonSetting
                  key={field.key}
                  form={form}
                  settingKey={field.key}
                  label={field.label}
                />
              ) : (
                <SettingField
                  key={field.key}
                  field={field}
                  form={form}
                  models={models.data?.models.map((m) => m.id)}
                />
              ),
            )}
            {current.slug === "about" && (
              <>
                <p>Splash GUI · 0.1.0</p>
                <p>Local inference powered by Splash. No telemetry.</p>
                <Link href="/logs/diagnostics">Versions and diagnostics</Link>
              </>
            )}
            {current.slug === "chat" && (
              <Button
                onClick={() =>
                  void settingsApi
                    .mcpTools()
                    .then((r) =>
                      toast(
                        `${r.tools.length} tools discovered; ${r.errors.length} server errors.`,
                      ),
                    )
                    .catch((e) => toastError("Could not discover tools.", e))
                }
              >
                Test MCP connections
              </Button>
            )}
            {current.slug === "advanced" && (
              <p>
                Crash traces can contain private conversation data. Keep them
                disabled unless needed to diagnose a problem.
              </p>
            )}
          </div>
        </div>
      </section>
      {["security", "hf"].includes(current.slug) && (
        <SecretControls key={current.slug} section={current.slug} />
      )}
      {current.slug === "data" && <DataControls />}
      <SettingsSave form={form} />
    </>
  );
}
