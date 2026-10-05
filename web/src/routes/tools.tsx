import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { Link } from "wouter-preact";
import { api } from "../api/client";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { PageHeader } from "../components/Section";
import { SubNav, TOOLS_TABS } from "../components/SubNav";
import { toastError } from "../components/Toast";
import { isCancelled, withInstallConfirm } from "../lib/engine-install";
import { useTitle } from "../lib/title";
import { engine, settings } from "../store";
import { t } from "../strings/tools";

export type ToolName = "playground" | "tokenizer" | "judgments" | "benchmark";

const READY = new Set(["ready", "busy", "idle_released"]);

/** True when tools that need a loaded model can run (docs/ui/08 intro). */
export function engineReady(): boolean {
  return READY.has(engine.value?.state ?? "");
}

/** The shared frame of the four Tools tabs: sub-band, title and the "No model is loaded." banner. */
export function ToolPage({ tool, children, actions }: { tool: ToolName; children?: ComponentChildren; actions?: ComponentChildren }) {
  const e = engine.value;
  const routing = (settings.value?.settings?.global?.routing ?? {}) as { auto_load?: boolean; default_model?: string | null };
  const autoLoad = routing.auto_load !== false;
  const fallback = routing.default_model ?? e?.model ?? null;
  const [loading, setLoading] = useState(false);
  useTitle(`${t(`tools.title.${tool}`).replace(/\.$/, "")} · ${t("tools.page_title")}`);
  const stopped = !e?.model || e.state === "stopped";
  return (
    <>
      <SubNav items={TOOLS_TABS} label={t("tools.tabs")} />
      <PageHeader title={t(`tools.title.${tool}`)} size="m" actions={actions} />
      {e && stopped && (
        <section class="band tight">
          <Banner
            tone="info"
            title={t("tools.not_loaded")}
            actions={
              autoLoad && fallback ? (
                <Button
                  size="s"
                  loading={loading}
                  onClick={() => {
                    setLoading(true);
                    void withInstallConfirm((force) => api.post("/engine/load", { model: fallback, force }), fallback)
                      .catch((err) => isCancelled(err) || toastError(t("tools.load_failed"), err))
                      .finally(() => setLoading(false));
                  }}
                >
                  {t("tools.load_now")}
                </Button>
              ) : (
                <Link href="/models" class="btn" data-size="s">
                  {t("tools.open_models")}
                </Link>
              )
            }
          >
            {autoLoad && fallback ? t("tools.not_loaded_auto", { model: fallback }) : t("tools.not_loaded_manual")}
          </Banner>
        </section>
      )}
      {children}
    </>
  );
}
