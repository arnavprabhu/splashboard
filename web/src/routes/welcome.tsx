import { useState } from "preact/hooks";
import { api } from "../api/client";
import type { SystemInfo } from "../api/models";
import { PageHeader } from "../components/Section";
import { useApi } from "../lib/use-api";
import { t } from "../strings/welcome";
import { useTitle } from "../lib/title";
import "../styles/pages/welcome.css";
import { StepEngine } from "./welcome/StepEngine";
import { StepStorage } from "./welcome/StepStorage";
import { StepModel, StepStart, StepUseCase } from "./welcome/StepsLater";
import { progress, updateProgress, WizardContext } from "./welcome/frame";
import { initialStep, parseStep, stepSearch, type Step } from "./welcome/steps";
import { isHosted } from "./welcome/host";

/** The welcome wizard (docs/ui/04, SPEC §10.2): five steps, progress kept across reloads. */
export default function Welcome() {
  useTitle(t("welcome.page_title"));
  const [step, setStep] = useState<Step>(() => initialStep(parseStep(location.search), progress.value));
  const system = useApi((s) => api.get<SystemInfo>("/system", undefined, s));
  const hosted = isHosted();
  function goTo(next: Step) {
    setStep(next);
    updateProgress({ step: next, reached: Math.max(progress.value.reached, next) as Step });
    history.replaceState(null, "", `${location.pathname}?${stepSearch(location.search, next)}`);
  }
  return (
    <WizardContext.Provider value={{ step, goTo, hosted, system: system.data }}>
      <PageHeader title={t("welcome.title")} />
      {step === 1 && <StepEngine />}
      {step === 2 && <StepStorage />}
      {step === 3 && <StepUseCase />}
      {step === 4 && <StepModel />}
      {step === 5 && <StepStart />}
    </WizardContext.Provider>
  );
}
