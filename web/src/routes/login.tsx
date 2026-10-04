import { useState } from "preact/hooks";
import { Redirect, useLocation, useSearch } from "wouter-preact";
import { api, ApiError } from "../api/client";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { PageHeader, Section } from "../components/Section";
import { useTitle } from "../lib/title";
import {
  auth,
  authKnown,
  refreshAll,
  refreshAuth,
  safeNext,
  setAuth,
} from "../store";
import { t } from "../strings/en";

export function loginErrorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return t("login.failed");
  if (err.status === 401) return t("login.wrong");
  if (err.status === 429) {
    const retry = Number(
      err.details?.retry_after ?? err.details?.retry_after_s,
    );
    return Number.isFinite(retry) && retry > 0
      ? t("login.throttled", { s: Math.ceil(retry) })
      : t("login.throttled_soon");
  }
  if (err.status === 0) return t("login.unreachable");
  return err.message;
}

/** /admin/login (docs/ui/01 §6): one admin-key field with SHOW/HIDE; returns to `?next=`. */
export default function LoginPage() {
  const [key, setKey] = useState("");
  const [shown, setShown] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [, navigate] = useLocation();
  const params = new URLSearchParams(useSearch());
  const next = safeNext(params.get("next"));
  const expired = params.get("expired") === "1";
  useTitle(t("login.page_title"));

  if (
    authKnown.value &&
    (!auth.value.admin_requires_key || auth.value.authenticated)
  ) {
    return <Redirect to={next} replace />;
  }

  const submit = async (e: Event) => {
    e.preventDefault();
    if (!key) return;
    setBusy(true);
    setError(null);
    try {
      const state = await api.post<unknown>("/auth/login", { key });
      if (state) setAuth(state);
      else await refreshAuth();
      refreshAll();
      navigate(next, { replace: true });
    } catch (err) {
      setError(loginErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const [helpBefore, helpAfter] = t("login.help").split("{cmd}");
  return (
    <>
      <PageHeader title={t("login.title")} />
      {expired && (
        <section class="band tight">
          <Banner tone="info">
            {next.startsWith("/settings")
              ? t("login.expired_settings")
              : t("login.expired")}
          </Banner>
        </section>
      )}
      <Section label={t("login.key")}>
        <form onSubmit={submit} class="stack" style={{ maxWidth: "560px" }}>
          <p class="lead">{t("login.lead")}</p>
          <div
            class="field"
            data-invalid={error ? "true" : undefined}
            style={{ borderTop: 0, paddingTop: 0 }}
          >
            <div class="field-body" style={{ flexBasis: "100%" }}>
              <label class="label" for="login-key">
                {t("login.key")}
              </label>
              <div
                class="cluster"
                style={{
                  flexWrap: "nowrap",
                  alignItems: "center",
                  gap: "12px",
                }}
              >
                <input
                  id="login-key"
                  class="input"
                  type={(shown ? "text" : "password") as "text"}
                  autocomplete="current-password"
                  spellcheck={false}
                  value={key}
                  aria-invalid={error ? "true" : undefined}
                  aria-describedby={
                    error ? "login-error login-help" : "login-help"
                  }
                  onInput={(e) => setKey(e.currentTarget.value)}
                />
                <button
                  type="button"
                  class="btn"
                  data-variant="text"
                  aria-pressed={shown}
                  onClick={() => setShown(!shown)}
                >
                  {shown ? t("common.hide") : t("common.show")}
                </button>
              </div>
              {error && (
                <p class="field-error" id="login-error" role="alert">
                  {error}
                </p>
              )}
            </div>
          </div>
          <div>
            <Button
              type="submit"
              variant="solid"
              disabled={!key}
              loading={busy}
            >
              {busy ? t("login.checking") : t("login.submit")}
            </Button>
          </div>
          <p class="body mute" id="login-help">
            {helpBefore}
            <code class="mono">splash config get security.api_key</code>
            {helpAfter}
          </p>
        </form>
      </Section>
    </>
  );
}
