import { useState } from "preact/hooks";
import { api } from "../api/client";
import type { DesktopIntegration, Integrations } from "../api/models";
import {
  Button,
  ConfirmSheet,
  CopyButton,
  LoadError,
  PageHeader,
  Section,
  toast,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import { engine } from "../store";
import { saveSettings } from "./welcome/api";

export default function IntegrationsPage() {
  const data = useApi((s) =>
    api.get<Integrations>("/integrations", undefined, s),
  );
  const [connecting, setConnecting] = useState<DesktopIntegration | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function action(path: string, body?: unknown) {
    setBusy(true);
    setError(null);
    try {
      await api.post(path, body);
      setConnecting(null);
      await data.reload();
    } catch (e) {
      setError(String(e));
      toastError("Integration action failed.", e);
    } finally {
      setBusy(false);
    }
  }
  const model = engine.value?.model ?? "your-model";
  const snippets = [
    [
      "OpenAI · Python",
      `from openai import OpenAI\nclient = OpenAI(base_url=${JSON.stringify(location.origin + "/v1")}, api_key="YOUR_SPLASH_KEY")\nprint(client.chat.completions.create(model=${JSON.stringify(model)}, messages=[{"role":"user","content":"Hello"}]))`,
    ],
    [
      "Anthropic · Python",
      `from anthropic import Anthropic\nclient = Anthropic(base_url=${JSON.stringify(location.origin)}, api_key="YOUR_SPLASH_KEY")\nprint(client.messages.create(model=${JSON.stringify(model)}, max_tokens=256, messages=[{"role":"user","content":"Hello"}]))`,
    ],
    [
      "JavaScript · fetch",
      `const response = await fetch(${JSON.stringify(location.origin + "/v1/chat/completions")}, {\n  method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer YOUR_SPLASH_KEY"},\n  body: JSON.stringify({model: ${JSON.stringify(model)}, messages: [{role: "user", content: "Hello"}]})\n});\nconsole.log(await response.json());`,
    ],
  ];
  return (
    <>
      <PageHeader title="Integrations." />
      {!!data.error && (
        <LoadError
          thing="integrations"
          error={data.error}
          onRetry={data.reload}
        />
      )}
      {data.data?.unclean_shutdown && (
        <Section label="Restore needed">
          <p>A previous session ended before restoring app configurations.</p>
          <Button
            disabled={busy}
            onClick={() => void action("/integrations/restore-all")}
          >
            Restore all apps
          </Button>
        </Section>
      )}
      <Section label="CLI agents">
        {data.data?.cli.map((c) => (
          <div class="integration-row stack" key={c.name}>
            <h2 class="heading">{c.label}</h2>
            <p class="meta">
              {c.installed
                ? `${c.version ?? "Installed"} · ${c.path}`
                : "Not installed"}
            </p>
            <div class="cluster">
              <code>{c.command}</code>
              <CopyButton text={c.command} />
              {c.installed ? (
                <Button
                  disabled={busy}
                  onClick={() =>
                    void action(`/integrations/${c.name}/open-terminal`)
                  }
                >
                  Open in Terminal
                </Button>
              ) : (
                <a
                  href={c.install_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Install
                </a>
              )}
            </div>
            <details>
              <summary>What this changes</summary>
              <pre>{JSON.stringify(c.changes, null, 2)}</pre>
            </details>
          </div>
        ))}
      </Section>
      <Section label="Desktop apps">
        {data.data?.desktop.map((app) => (
          <div class="integration-row stack" key={app.name}>
            <h2 class="heading">{app.label}</h2>
            <p>
              {app.detected ? (app.version ?? "Detected") : "Not detected"} ·{" "}
              {app.state.replace(/_/g, " ")}
            </p>
            <p class="meta">{app.warning}</p>
            {app.untested_version && (
              <p role="alert">This app version has not been tested.</p>
            )}
            <Button
              disabled={!app.detected || busy}
              onClick={() =>
                app.state === "connected" || app.state === "needs_restore"
                  ? void action(`/integrations/${app.name}/disconnect`)
                  : setConnecting(app)
              }
            >
              {app.state === "connected" || app.state === "needs_restore"
                ? "Disconnect & restore"
                : "Connect"}
            </Button>
          </div>
        ))}
      </Section>
      {snippets.map(([label, code]) => (
        <Section key={label} label={label!}>
          <pre class="mono">{code}</pre>
          <CopyButton text={code!} />
        </Section>
      ))}
      <Section label="Other apps">
        <p>
          Use the OpenAI base URL <code>{location.origin}/v1</code> in
          compatible apps.
        </p>
        <Button
          onClick={() =>
            void saveSettings((doc) => {
              const server = doc.global.server ?? {
                host: "127.0.0.1",
                port: 8000,
              };
              server.allowed_origins = [
                ...new Set([
                  ...(server.allowed_origins ?? []),
                  "tauri://localhost",
                ]),
              ];
              doc.global.server = server;
            })
              .then(() => toast("Tauri origin allowed."))
              .catch((e) => toastError("Could not allow origin.", e))
          }
        >
          Allow origin tauri://localhost
        </Button>
      </Section>
      <ConfirmSheet
        open={!!connecting}
        title={`Connect ${connecting?.label ?? "app"}.`}
        confirmLabel="Restart & connect"
        onClose={() => setConnecting(null)}
        busy={busy}
        error={error}
        onConfirm={() =>
          action(`/integrations/${connecting!.name}/connect`, {
            confirm_restart: true,
          })
        }
      >
        <p>
          {connecting?.label} will restart. Its previous configuration is backed
          up and restored when you disconnect or quit Splash GUI.
        </p>
      </ConfirmSheet>
    </>
  );
}
