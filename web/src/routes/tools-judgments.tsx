import { useState } from "preact/hooks";
import { ApiError, request } from "../api/client";
import { Banner } from "../components/Banner";
import { Button, ExternalLink } from "../components/Button";
import { SegmentedControl } from "../components/controls";
import { copyText } from "../components/CopyButton";
import { Disclosure } from "../components/Disclosure";
import { TextArea, TextInput } from "../components/inputs";
import { Section } from "../components/Section";
import { toast } from "../components/Toast";
import { Tooltip } from "../components/Tooltip";
import { formatCount, formatIndex } from "../lib/format";
import { engine, settings } from "../store";
import { t } from "../strings/tools";
import "../styles/pages/tools.css";
import { ToolPage, engineReady } from "./tools";
import { JsonEditor } from "./tools/JsonEditor";
import {
  LETTERS,
  MAX_OPTIONS,
  MAX_QUESTIONS,
  answerView,
  detailKey,
  newQuestion,
  questionErrors,
  questionsFromBody,
  semifBody,
  semifErrors,
  shortHash,
  systemOneBody,
  type Bar,
  type QType,
  type Question,
  type SemifRow,
} from "./tools/judgments";
import { curlSnippet, typesafeSnippet } from "./tools/snippets";
import { endpointById, SEMIF_EXAMPLE, SYSTEMONE_EXAMPLE } from "./tools/endpoints";

type Tab = "systemone" | "semif";
const CLEF_URL = "https://blog.cloudflare.com/clef-decision-models/";

function Bars({ bars, compact }: { bars: Bar[]; compact?: boolean }) {
  return (
    <ul class={compact ? "jd-bars jd-bars-row" : "jd-bars"}>
      {bars.map((b) => (
        <li key={b.label} class="jd-bar">
          <span class="jd-bar-label">{b.label}</span>
          <span class="jd-track" role="img" aria-label={`${b.label} ${b.p.toFixed(2)}`}>
            <span class="jd-fill" data-winner={b.winner ? "true" : undefined} style={{ width: `${Math.round(b.p * 100)}%` }} />
          </span>
          <span class="tnum">{b.p.toFixed(2)}</span>
        </li>
      ))}
    </ul>
  );
}

export default function Judgments() {
  const [tab, setTab] = useState<Tab>("systemone");
  const model = engine.value?.model ?? "";
  const clef = /clef/i.test(model);
  return (
    <ToolPage tool="judgments">
      {clef && (
        <section class="band tight">
          <Banner tone="info" title={t("tools.jd.clef_title")} actions={<ExternalLink href={CLEF_URL}>{t("tools.jd.clef_link")}</ExternalLink>}>
            {t("tools.jd.clef_body")}
          </Banner>
        </section>
      )}
      <nav class="subnav tool-tabs" aria-label={t("tools.jd.tabs")}>
        {(["systemone", "semif"] as const).map((k) => (
          <button key={k} type="button" class="navlink nav" aria-current={tab === k ? "page" : undefined} onClick={() => setTab(k)}>
            {t(`tools.jd.tab.${k}`)}
          </button>
        ))}
      </nav>
      <p class="meta jd-caveat" data-testid="jd-caveat">
        {t("tools.jd.caveat")}
      </p>
      {tab === "systemone" ? <SystemOne model={model} /> : <Semif model={model} />}
    </ToolPage>
  );
}

function SystemOne({ model }: { model: string }) {
  const ready = engineReady();
  const auth = !!(settings.value?.settings?.global?.security as { api_key_required?: boolean } | undefined)?.api_key_required;
  const [stateKind, setStateKind] = useState<"text" | "json">("text");
  const [stateText, setStateText] = useState(SYSTEMONE_EXAMPLE.state.message);
  const [questions, setQuestions] = useState<Question[]>(() => questionsFromBody(SYSTEMONE_EXAMPLE) ?? []);
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [result, setResult] = useState<{ answers: Record<string, Record<string, unknown>>; tokens: number | null; secs: number } | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [errorDetail, setErrorDetail] = useState<unknown>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const errors = questionErrors(questions);
  let state: unknown = stateText;
  let stateError: string | null = null;
  if (stateKind === "json") {
    try {
      state = JSON.parse(stateText);
    } catch {
      stateError = t("tools.jd.state_invalid");
    }
  }
  const body = systemOneBody(model, state, questions);
  const canRun = ready && !busy && !stateError && Object.keys(errors).length === 0 && questions.length > 0;
  const update = (uid: string, patch: Partial<Question>) => setQuestions(questions.map((q) => (q.uid === uid ? { ...q, ...patch } : q)));
  const add = (type: QType) => setQuestions([...questions, newQuestion(type, questions.map((q) => q.key))]);

  async function run() {
    if (!canRun) return;
    setBusy(true);
    setFailure(null);
    setServerErrors({});
    setErrorDetail(null);
    const started = performance.now();
    try {
      const res = await request<{ answers: Record<string, Record<string, unknown>>; usage?: { input_tokens?: number } }>("/v1/systemone", { body });
      setResult({ answers: res.answers ?? {}, tokens: res.usage?.input_tokens ?? null, secs: (performance.now() - started) / 1000 });
    } catch (e) {
      setResult(null);
      if (e instanceof ApiError && e.status === 422) {
        const detail = (e.body as { detail?: Array<{ loc?: unknown; msg?: string }> } | null)?.detail ?? [];
        const map: Record<string, string> = {};
        for (const d of detail) {
          const key = detailKey(d.loc);
          const q = questions.find((x) => x.key === key);
          if (q) map[q.uid] = d.msg ?? e.message;
        }
        setServerErrors(map);
        setErrorDetail(detail);
      }
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Section label={t("tools.jd.state")}>
        <div class="stack">
          <SegmentedControl
            label={t("tools.jd.state_kind")}
            size="s"
            value={stateKind}
            options={[
              { value: "text", label: t("tools.jd.state.text") },
              { value: "json", label: t("tools.jd.state.json") },
            ]}
            onChange={setStateKind}
          />
          <TextArea class={stateKind === "json" ? "mono" : undefined} rows={4} value={stateText} onChange={setStateText} aria-label={t("tools.jd.state")} invalid={!!stateError} />
          {stateError && <p class="field-error">{stateError}</p>}
        </div>
      </Section>
      <Section label={t("tools.jd.questions")} meta={t("tools.jd.count", { n: questions.length })}>
        <div class="stack">
          <div class="cluster">
            {(["noul", "choice", "score"] as const).map((k) =>
              questions.length >= MAX_QUESTIONS ? (
                <Tooltip key={k} text={t("tools.jd.max_questions")}>
                  <Button size="s" aria-disabled="true">
                    {t(`tools.jd.add.${k}`)}
                  </Button>
                </Tooltip>
              ) : (
                <Button key={k} size="s" onClick={() => add(k)}>
                  {t(`tools.jd.add.${k}`)}
                </Button>
              ),
            )}
          </div>
          {questions.map((q, i) => (
            <fieldset key={q.uid} class="jd-question" data-invalid={errors[q.uid] || serverErrors[q.uid] ? "true" : undefined}>
              <legend class="label">
                {formatIndex(i + 1)} — {q.type}
              </legend>
              <Button size="s" variant="text" class="jd-remove" onClick={() => setQuestions(questions.filter((x) => x.uid !== q.uid))}>
                {t("tools.jd.remove")}
              </Button>
              <label class="jd-row">
                <span class="label">{t("tools.jd.key")}</span>
                <TextInput class="mono" value={q.key} onChange={(v) => update(q.uid, { key: v })} />
              </label>
              <label class="jd-row">
                <span class="label">{t("tools.jd.instructions")}</span>
                <TextInput value={q.instructions} onChange={(v) => update(q.uid, { instructions: v })} />
              </label>
              {q.type === "noul" && (
                <Disclosure summary={t("tools.jd.criteria_noul")}>
                  <label class="jd-row">
                    <span class="label">{t("tools.jd.true")}</span>
                    <TextInput value={q.noulTrue} onChange={(v) => update(q.uid, { noulTrue: v })} />
                  </label>
                  <label class="jd-row">
                    <span class="label">{t("tools.jd.false")}</span>
                    <TextInput value={q.noulFalse} onChange={(v) => update(q.uid, { noulFalse: v })} />
                  </label>
                </Disclosure>
              )}
              {q.type === "choice" && (
                <div class="stack">
                  <span class="label">{t("tools.jd.criteria")}</span>
                  {q.labels.map((l, li) => (
                    <div key={li} class="cluster jd-option">
                      <TextInput class="mono" aria-label={`${t("tools.jd.label")} ${li + 1}`} value={l.label} onChange={(v) => update(q.uid, { labels: q.labels.map((x, j) => (j === li ? { ...x, label: v } : x)) })} />
                      <TextInput aria-label={`${t("tools.jd.description")} ${li + 1}`} placeholder={t("tools.jd.description")} value={l.description} onChange={(v) => update(q.uid, { labels: q.labels.map((x, j) => (j === li ? { ...x, description: v } : x)) })} />
                      <Button size="s" variant="text" aria-label={`${t("tools.jd.remove")} ${l.label}`} onClick={() => update(q.uid, { labels: q.labels.filter((_, j) => j !== li) })}>
                        ×
                      </Button>
                    </div>
                  ))}
                  <Button size="s" variant="text" onClick={() => update(q.uid, { labels: [...q.labels, { label: "", description: "" }] })}>
                    {t("tools.jd.add_label")}
                  </Button>
                </div>
              )}
              {q.type === "score" && (
                <div class="stack">
                  <span class="label">{t("tools.jd.levels")}</span>
                  <div class="cluster">
                    {q.levels.map((lv, li) => (
                      <span key={li} class="cluster jd-option">
                        <span class="tnum">{li + 1}</span>
                        <TextInput aria-label={`${t("tools.jd.levels")} ${li + 1}`} value={lv} onChange={(v) => update(q.uid, { levels: q.levels.map((x, j) => (j === li ? v : x)) })} />
                        <Button size="s" variant="text" aria-label={`${t("tools.jd.remove")} ${lv}`} onClick={() => update(q.uid, { levels: q.levels.filter((_, j) => j !== li) })}>
                          ×
                        </Button>
                      </span>
                    ))}
                  </div>
                  <Button size="s" variant="text" onClick={() => update(q.uid, { levels: [...q.levels, ""] })}>
                    {t("tools.jd.add_level")}
                  </Button>
                </div>
              )}
              {(errors[q.uid] || serverErrors[q.uid]) && <p class="field-error">{errors[q.uid] ?? serverErrors[q.uid]}</p>}
            </fieldset>
          ))}
          <details
            class="disclosure"
            open={jsonOpen}
            onToggle={(e) => {
              const open = (e.currentTarget as HTMLDetailsElement).open;
              setJsonOpen(open);
              if (open) setJsonText(JSON.stringify(body, null, 2));
            }}
          >
            <summary class="label">
              <span class="disclosure-glyph" aria-hidden="true">
                ▸
              </span>
              {t("tools.jd.edit_json")}
            </summary>
            <div class="disclosure-body">
              <JsonEditor
                id="jd-json"
                label={t("tools.jd.edit_json")}
                value={jsonText}
                onChange={(v) => {
                  setJsonText(v);
                  try {
                    const parsed = JSON.parse(v) as { state?: unknown };
                    const qs = questionsFromBody(parsed);
                    if (qs) setQuestions(qs);
                    if (typeof parsed.state === "string") (setStateKind("text"), setStateText(parsed.state));
                    else if (parsed.state !== undefined) (setStateKind("json"), setStateText(JSON.stringify(parsed.state, null, 2)));
                  } catch {
                    /* the editor shows the error */
                  }
                }}
              />
              {(() => {
                try {
                  return questionsFromBody(JSON.parse(jsonText)) === null ? <p class="meta">{t("tools.jd.json_unrepresentable")}</p> : null;
                } catch {
                  return null;
                }
              })()}
            </div>
          </details>
          <div class="cluster">
            <Button variant={canRun ? "accent" : "outline"} disabled={!canRun} loading={busy} title={ready ? undefined : t("tools.needs_ready")} onClick={() => void run()}>
              {t("tools.jd.run")}
            </Button>
            <span class="meta">⌘↵</span>
            <Button
              variant="text"
              onClick={() => void copyText(typesafeSnippet({ origin: location.origin, auth, body: body as Record<string, unknown> })).then((ok) => ok && toast(t("tools.pg.copied", { what: "typesafe-sdk" })))}
            >
              {t("tools.jd.copy_typesafe")}
            </Button>
          </div>
          {failure && (
            <Banner tone="warn" title={t("tools.jd.failed")}>
              <code class="mono">{failure}</code>
            </Banner>
          )}
          {errorDetail != null && (
            <Disclosure summary={t("tools.jd.error_details")}>
              <pre class="mono">{JSON.stringify(errorDetail, null, 2)}</pre>
            </Disclosure>
          )}
        </div>
      </Section>
      {result && (
        <Section label={t("tools.jd.results")} meta={t("tools.jd.results_meta", { tokens: result.tokens === null ? "—" : formatCount(result.tokens), secs: result.secs.toFixed(1) })}>
          <div class="stack">
            {Object.entries(result.answers).map(([key, a], i) => {
              const q = questions.find((x) => x.key === key);
              const v = answerView(q, a);
              return (
                <div key={key} class="jd-answer">
                  <div class="jd-answer-head">
                    <span class="label">
                      {formatIndex(i + 1)} {key} · {String(a.type)}
                    </span>
                    {/* Facts are separate items spaced by the gap: no "·" is left dangling when a line wraps or a fact is absent. */}
                    <span class="label tnum jd-answer-facts" data-testid="jd-answer-facts">
                      <span>{v.head}</span>
                      {v.noInference && <span>{t("tools.jd.no_inference")}</span>}
                      {v.confidence !== null && !v.noInference && (
                        <Tooltip text={t("tools.jd.confidence_tip")}>
                          <span tabIndex={0}>{t("tools.jd.confidence", { v: v.confidence.toFixed(2) })} ⓘ</span>
                        </Tooltip>
                      )}
                    </span>
                  </div>
                  {!v.noInference && <Bars bars={v.bars} compact={a.type === "score"} />}
                </div>
              );
            })}
          </div>
        </Section>
      )}
    </>
  );
}

function Semif({ model }: { model: string }) {
  const ready = engineReady();
  const auth = !!(settings.value?.settings?.global?.security as { api_key_required?: boolean } | undefined)?.api_key_required;
  const [row, setRow] = useState<SemifRow>(() => ({ ...SEMIF_EXAMPLE, options: SEMIF_EXAMPLE.options.map((o) => ({ ...o })) }));
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const err = semifErrors(row);
  const body = semifBody(model, row);
  const canRun = ready && !busy && !err && !!row.id.trim() && !!row.question.trim() && !!row.state.trim();
  async function run() {
    if (!canRun) return;
    setBusy(true);
    setFailure(null);
    try {
      setResult(await request<Record<string, unknown>>("/v1/judgments", { body }));
    } catch (e) {
      setResult(null);
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const probs = (result?.probabilities as number[] | undefined) ?? [];
  const logits = (result?.option_logits as number[] | undefined) ?? [];
  const ids = (result?.option_ids as string[] | undefined) ?? row.options.map((o) => o.id);
  const max = Math.max(...probs);
  const sha = typeof result?.prompt_sha256 === "string" ? result.prompt_sha256 : "";
  return (
    <>
      <Section label={t("tools.jd.tab.semif")}>
        <div class="stack">
          <label class="jd-row">
            <span class="label">{t("tools.jd.id")}</span>
            <TextInput class="mono" value={row.id} onChange={(v) => setRow({ ...row, id: v })} />
          </label>
          <label class="jd-row">
            <span class="label">{t("tools.jd.state")}</span>
            <TextArea rows={3} value={row.state} onChange={(v) => setRow({ ...row, state: v })} />
          </label>
          <label class="jd-row">
            <span class="label">{t("tools.jd.question")}</span>
            <TextInput value={row.question} onChange={(v) => setRow({ ...row, question: v })} />
          </label>
          <div class="cluster">
            <span class="label">{t("tools.jd.options")}</span>
            <span class="meta tnum">{t("tools.jd.options_count", { n: row.options.length })}</span>
            {row.options.length >= MAX_OPTIONS ? (
              <Tooltip text={t("tools.jd.max_options")}>
                <Button size="s" aria-disabled="true">
                  {t("tools.jd.add_option")}
                </Button>
              </Tooltip>
            ) : (
              <Button size="s" onClick={() => setRow({ ...row, options: [...row.options, { id: "", description: "" }] })}>
                {t("tools.jd.add_option")}
              </Button>
            )}
          </div>
          {row.options.map((o, i) => (
            <div key={i} class="cluster jd-option">
              <span class="label">{LETTERS[i]}</span>
              <TextInput class="mono" aria-label={`${t("tools.jd.option_id")} ${LETTERS[i]}`} value={o.id} onChange={(v) => setRow({ ...row, options: row.options.map((x, j) => (j === i ? { ...x, id: v } : x)) })} />
              <TextInput aria-label={`${t("tools.jd.description")} ${LETTERS[i]}`} placeholder={t("tools.jd.description")} value={o.description} onChange={(v) => setRow({ ...row, options: row.options.map((x, j) => (j === i ? { ...x, description: v } : x)) })} />
              <Button size="s" variant="text" aria-label={`${t("tools.jd.remove")} ${LETTERS[i]}`} onClick={() => setRow({ ...row, options: row.options.filter((_, j) => j !== i) })}>
                ×
              </Button>
            </div>
          ))}
          {err && <p class="field-error">{err}</p>}
          <div class="cluster">
            <Button variant={canRun ? "accent" : "outline"} disabled={!canRun} loading={busy} onClick={() => void run()}>
              {t("tools.jd.run")}
            </Button>
            <Button
              variant="text"
              onClick={() =>
                void copyText(curlSnippet({ origin: location.origin, endpoint: endpointById("judgments"), path: "/v1/judgments", body, auth })).then((ok) => ok && toast(t("tools.pg.copied", { what: "curl" })))
              }
            >
              {t("tools.jd.copy_curl")}
            </Button>
          </div>
          {failure && (
            <Banner tone="warn" title={t("tools.jd.failed")}>
              <code class="mono">{failure}</code>
            </Banner>
          )}
        </div>
      </Section>
      {result && (
        <Section
          label={t("tools.jd.result")}
          meta={
            <span class="cluster">
              {sha && (
                <>
                  <span class="mono">{t("tools.jd.sha", { v: shortHash(sha) })}</span>
                  <Button size="s" variant="text" onClick={() => void copyText(sha)}>
                    {t("common.copy")}
                  </Button>
                </>
              )}
              {Array.isArray(result.answer_token_ids) && <span class="mono">{t("tools.jd.answer_tokens", { v: JSON.stringify(result.answer_token_ids) })}</span>}
            </span>
          }
        >
          <ul class="jd-bars">
            {ids.map((id, i) => (
              <li key={id} class="jd-bar jd-bar-semif">
                <span class="jd-bar-label">
                  <span class="label">{LETTERS[i]}</span> <span class="mono">{id}</span>
                </span>
                <span class="meta tnum">{t("tools.jd.logit", { v: (logits[i] ?? 0).toFixed(2) })}</span>
                <span class="meta tnum">{t("tools.jd.p", { v: (probs[i] ?? 0).toFixed(2) })}</span>
                <span class="jd-track" role="img" aria-label={`${id} ${(probs[i] ?? 0).toFixed(2)}`}>
                  <span class="jd-fill" data-winner={probs[i] === max ? "true" : undefined} style={{ width: `${Math.round((probs[i] ?? 0) * 100)}%` }} />
                </span>
              </li>
            ))}
          </ul>
          {typeof result.input_tokens === "number" && <p class="meta">{t("tools.jd.usage", { n: formatCount(result.input_tokens) })}</p>}
        </Section>
      )}
    </>
  );
}
