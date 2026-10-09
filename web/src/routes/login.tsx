import { useEffect, useState } from "preact/hooks";
import { Redirect, useLocation, useSearch } from "wouter-preact";
import { api, ApiError } from "../api/client";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { PageHeader, Section } from "../components/Section";
import { Loading } from "../components/States";
import { useTitle } from "../lib/title";
import { auth, authKnown, exchangeLoginCode, safeNext, signedIn } from "../store";
import { t } from "../strings/login";

/** Removes `code` from the address bar (and history) before it is spent, keeping `next`. */
export function stripCode(href: string): string {
  const url = new URL(href);
  url.searchParams.delete("code");
  return url.pathname + url.search + url.hash;
}

/** A one-time code is spent once, even if the page renders twice. */
const spent = new Set<string>();

/** What to do with `?code=` on mount: "exchanging" until it settles. */
type LinkState = "none" | "exchanging" | "expired" | "unreachable";

function useLoginLink(code: string | null, onDone: () => void): LinkState {
  const [state, setState] = useState<LinkState>(() => (!code ? "none" : spent.has(code) ? "expired" : "exchanging"));
  useEffect(() => {
    if (!code) return;
    history.replaceState(history.state, "", stripCode(location.href));
    if (spent.has(code)) return;
    spent.add(code);
    exchangeLoginCode(code).then(onDone, (err: unknown) => {
      setState(err instanceof ApiError && err.status === 0 ? "unreachable" : "expired");
    });
  }, [code]);
  return state;
}

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

/**
 * /admin/login: one API-key field with SHOW/HIDE; returns to `?next=`.
 * `?code=` is a one-time link minted by the menu bar or CLI: it is stripped from the address bar,
 * exchanged for the session cookie, and the user lands on `next` without typing the key.
 */
export default function LoginPage() {
  const [key, setKey] = useState("");
  const [shown, setShown] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [, navigate] = useLocation();
  const params = new URLSearchParams(useSearch());
  const next = safeNext(params.get("next"));
  const expired = params.get("expired") === "1";
  const [code] = useState(() => params.get("code"));
  const link = useLoginLink(code, () => navigate(next, { replace: true }));
  useTitle(t("login.page_title"));

  if (link === "exchanging") return <Loading label={t("login.link_signing_in")} />;
  // A 401 marks the user signed out, so a write refused while sign-in is off stays here.
  if (link === "none" && authKnown.value && auth.value.authenticated) {
    return <Redirect to={next} replace />;
  }
  const signInOff = authKnown.value && !auth.value.admin_requires_key;

  const submit = async (e: Event) => {
    e.preventDefault();
    if (!key) return;
    setBusy(true);
    setError(null);
    try {
      await signedIn(await api.post<unknown>("/auth/login", { key }));
      navigate(next, { replace: true });
    } catch (err) {
      setError(loginErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  // {cmd} becomes a <code> element: fill it with a marker and split around it.
  const [helpBefore, helpAfter] = t("login.help", { cmd: "\u0000" }).split("\u0000");
  return (
    <>
      <PageHeader title={t("login.title")} />
      {(link === "expired" || link === "unreachable") && (
        <section class="band tight">
          <Banner tone="warn">
            <span data-testid="login-link-error">
              {link === "expired" ? t("login.link_expired") : t("login.unreachable")}
            </span>
          </Banner>
        </section>
      )}
      {link === "none" && (expired || signInOff) && (
        <section class="band tight">
          <Banner tone="info">
            {expired
              ? next.startsWith("/settings")
                ? t("login.expired_settings")
                : t("login.expired")
              : next.startsWith("/settings")
                ? t("login.write_needs_session_settings")
                : t("login.write_needs_session")}
          </Banner>
        </section>
      )}
      <Section>
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
            <code class="mono">splash open</code>
            {helpAfter}
          </p>
          <p class="body mute">{t("login.menubar")}</p>
          <p class="body mute">{t("login.key_where")}</p>
        </form>
      </Section>
    </>
  );
}
