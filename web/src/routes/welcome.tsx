import { useState } from "preact/hooks";
import { useLocation } from "wouter-preact";
import { api } from "../api/client";
import type { SystemInfo } from "../api/models";
import {
  Button,
  CodeBlock,
  LoadError,
  PageHeader,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import { engine } from "../store";
import { StepEngine } from "./welcome/StepEngine";
import { StepStorage } from "./welcome/StepStorage";
import {
  progress,
  StepLayout,
  updateProgress,
  WizardContext,
} from "./welcome/frame";
import {
  initialStep,
  parseStep,
  stepSearch,
  type Step,
  type PresetId,
} from "./welcome/steps";
import { isHosted, closeWelcome } from "./welcome/host";
import {
  applyPreset,
  getInstalled,
  getPresets,
  loadEngine,
  markCompleted,
} from "./welcome/api";
import Downloader from "./models-downloader";
export default function Welcome() {
  const [, navigate] = useLocation();
  const [step, setStep] = useState<Step>(() =>
    initialStep(parseStep(location.search), progress.value),
  );
  const system = useApi((s) => api.get<SystemInfo>("/system", undefined, s));
  const presets = useApi(getPresets);
  const installed = useApi(getInstalled, [step]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const hosted = isHosted();
  function goTo(next: Step) {
    setStep(next);
    updateProgress({
      step: next,
      reached: Math.max(progress.value.reached, next) as Step,
    });
    history.replaceState(
      null,
      "",
      `${location.pathname}?${stepSearch(location.search, next)}`,
    );
  }
  async function preset(id: PresetId) {
    setBusy(true);
    try {
      await applyPreset(id);
      updateProgress({ preset: id });
      goTo(4);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!progress.value.model) return;
    setBusy(true);
    setError(null);
    try {
      await loadEngine(progress.value.model);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function finish() {
    try {
      await markCompleted((doc) => {
        doc.global.routing = {
          ...(doc.global.routing ?? {
            auto_load: true,
            switch_when_busy: "reject",
            unknown_model_fallback: false,
            load_timeout: 120,
          }),
          default_model: progress.value.model,
        };
        if (progress.value.pendingPort)
          doc.global.server = {
            ...(doc.global.server ?? { host: "127.0.0.1", port: 8000 }),
            port: progress.value.pendingPort,
          };
      });
      if (hosted) await closeWelcome(true);
      navigate("/status");
    } catch (e) {
      toastError("Could not finish setup.", e);
    }
  }
  return (
    <WizardContext.Provider value={{ step, goTo, hosted, system: system.data }}>
      <PageHeader title="Welcome." />
      {!!error && <LoadError thing="setup" error={error} />}
      {step === 1 && <StepEngine />}
      {step === 2 && <StepStorage />}
      {step === 3 && (
        <StepLayout
          lead="Choose how you will use Splash."
          footer={{ hideContinue: true }}
        >
          {!!presets.error && (
            <LoadError
              thing="presets"
              error={presets.error}
              onRetry={presets.reload}
            />
          )}{" "}
          {presets.data?.presets.map((p) => (
            <div class="stack" key={p.id}>
              <h2 class="heading">{p.label}</h2>
              <p>{p.description}</p>
              <details>
                <summary>Settings this changes</summary>
                <CodeBlock code={JSON.stringify(p.settings, null, 2)} />
              </details>
              <Button disabled={busy} onClick={() => void preset(p.id)}>
                Use {p.label}
              </Button>
            </div>
          ))}
        </StepLayout>
      )}
      {step === 4 && (
        <StepLayout
          lead="Choose a model."
          footer={{
            onContinue: () => goTo(5),
            continueDisabled: !progress.value.model,
          }}
        >
          <label>
            Installed model
            <select
              value={progress.value.model ?? ""}
              onChange={(e) =>
                updateProgress({ model: e.currentTarget.value || null })
              }
            >
              <option value="">Choose an installed model</option>
              {installed.data?.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </label>
          <Button onClick={installed.reload}>Refresh installed models</Button>
          <details>
            <summary>Download a model</summary>
            <Downloader />
          </details>
        </StepLayout>
      )}
      {step === 5 && (
        <StepLayout
          lead="Start your local server."
          footer={{
            onContinue: () => void finish(),
            continueLabel: "Open Splash",
            continueDisabled:
              engine.value?.state !== "ready" && engine.value?.state !== "busy",
          }}
        >
          <p class="mono">{progress.value.model}</p>
          <Button
            disabled={busy || engine.value?.state.startsWith("starting.")}
            variant="accent"
            onClick={() => void start()}
          >
            Load model
          </Button>
          <p role="status">{engine.value?.state.replace(/[._]/g, " ")}</p>
          <p>
            OpenAI base URL: <code>{location.origin}/v1</code>
          </p>
          <CodeBlock code="splash launch claude" />
        </StepLayout>
      )}
    </WizardContext.Provider>
  );
}
