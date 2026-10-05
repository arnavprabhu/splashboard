import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { ApiError } from "../api/client";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { CodeBlock } from "../components/CodeBlock";
import { SegmentedControl } from "../components/controls";
import { copyText } from "../components/CopyButton";
import { Disclosure } from "../components/Disclosure";
import { Select, TextInput } from "../components/inputs";
import { Menu } from "../components/Menu";
import { Section } from "../components/Section";
import { Table } from "../components/Table";
import { Tag } from "../components/Tag";
import { toast } from "../components/Toast";
import { Tooltip } from "../components/Tooltip";
import { formatCount } from "../lib/format";
import { auth, engine, settings } from "../store";
import { t } from "../strings/tools";
import "../styles/pages/tools.css";
import { classifyError } from "./chat/logic";
import { ToolPage, engineReady } from "./tools";
import { ENDPOINTS, GROUP_ORDER, defaultBody, endpointById, endpointFor, endpointLabel, paramError, resolvePath, templatesFor, withModel, type EndpointId } from "./tools/endpoints";
import { addEntry, loadHistory, removeEntry, saveHistory, type HistoryEntry } from "./tools/history";
import { JsonEditor, jsonProblem } from "./tools/JsonEditor";
import { snippet, snippetAvailable, type SnippetKind } from "./tools/snippets";
import { TimedSseParser, assemble, emptyAssembled, preview, pretty, responseIdOf, summarize, type Assembled, type RawEvent } from "./tools/sse-timing";

type View = "raw" | "parsed" | "headers";
const KINDS: readonly SnippetKind[] = ["curl", "python_openai", "python_anthropic", "js_fetch"];
const copyLabel = (k: SnippetKind) => t(`tools.pg.copy.${k}`);

const ms = (v: number | null) => (v === null ? "—" : `${formatCount(Math.round(v))} ms`);

export default function Playground() {
  const active = engine.value?.model ?? "";
  const [endpoint, setEndpoint] = useState<EndpointId>("chat");
  const ep = endpointById(endpoint);
  const [param, setParam] = useState("");
  const [body, setBody] = useState(() => defaultBody("chat", active));
  const [raw, setRaw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rows, setRows] = useState<RawEvent[]>([]);
  const [parsed, setParsed] = useState<Assembled | null>(null);
  const [plain, setPlain] = useState<string | null>(null);
  const [status, setStatus] = useState<{ code: number; text: string; secs: number } | null>(null);
  const [headers, setHeaders] = useState<Array<[string, string]>>([]);
  const [doneAt, setDoneAt] = useState<number | null>(null);
  const [view, setView] = useState<View>("raw");
  const [open, setOpen] = useState<number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>(loadHistory);
  const [clearing, setClearing] = useState(false);
  const [truncatedNote, setTruncatedNote] = useState(false);
  const lastResponseId = useRef<string | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);
  const keyRequired = !!(settings.value?.settings?.global?.security as { api_key_required?: boolean } | undefined)?.api_key_required;

  function choose(id: EndpointId) {
    setEndpoint(id);
    const next = endpointById(id);
    setParam(next.param === "response_id" ? (lastResponseId.current ?? "") : next.param === "model_id" ? active : "");
    setBody(defaultBody(id, active));
    setTruncatedNote(false);
  }

  const bodyProblem = ep.body ? jsonProblem(body, (v) => (v && typeof v === "object" && !Array.isArray(v) ? null : "The body must be a JSON object.")) : null;
  const pErr = paramError(ep, param);
  const rawBlocked = raw && !engineReady();
  const canSend = !busy && !bodyProblem && !pErr && !rawBlocked;

  async function send() {
    if (!canSend) return;
    const abort = new AbortController();
    ctrl.current = abort;
    setBusy(true);
    setRows([]);
    setParsed(null);
    setPlain(null);
    setStatus(null);
    setHeaders([]);
    setDoneAt(null);
    setFailure(null);
    setOpen(null);
    const started = performance.now();
    const at = () => performance.now() - started;
    const path = resolvePath(ep, param);
    const url = raw ? `/api/admin/engine/raw/${path.replace(/^\//, "")}` : path;
    let code: number | null = null;
    let summary = "";
    const collected: RawEvent[] = [{ index: 1, t: 0, kind: "open", name: "open", data: "" }];
    setRows([...collected]);
    try {
      const res = await fetch(url, {
        method: ep.method,
        credentials: "same-origin",
        headers: ep.body ? { "Content-Type": "application/json", Accept: "application/json, text/event-stream" } : { Accept: "*/*" },
        body: ep.body ? body : undefined,
        signal: abort.signal,
      });
      code = res.status;
      setHeaders([...res.headers.entries()]);
      const type = res.headers.get("content-type") ?? "";
      if (type.includes("text/event-stream") && res.body) {
        const parser = new TimedSseParser();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let acc = emptyAssembled();
        const push = (items: ReturnType<TimedSseParser["push"]>) => {
          for (const it of items) {
            collected.push({ index: collected.length + 1, t: at(), kind: it.kind, name: it.name, data: it.data });
            if (it.kind === "event") acc = assemble(ep.shape, acc, it.name, it.data);
          }
        };
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          push(parser.push(decoder.decode(value, { stream: true })));
          setRows([...collected]);
          setParsed({ ...acc });
        }
        push(parser.end());
        setRows([...collected]);
        setParsed({ ...acc });
        summary = acc.error ? JSON.stringify(acc.error) : acc.text || acc.reasoning;
        const rid = responseIdOf(acc.response);
        if (rid) lastResponseId.current = rid;
      } else {
        const text = await res.text();
        collected.push({ index: collected.length + 1, t: at(), kind: "body", name: "body", data: text });
        setRows([...collected]);
        setPlain(text);
        setView("parsed");
        summary = text;
        try {
          const rid = responseIdOf(JSON.parse(text));
          if (rid) lastResponseId.current = rid;
        } catch {
          /* not JSON (metrics) */
        }
      }
      setStatus({ code: res.status, text: res.statusText, secs: at() / 1000 });
      if (!res.ok) {
        let errBody: unknown = null;
        try {
          errBody = JSON.parse(summary);
        } catch {
          /* text */
        }
        const msg = (errBody as { error?: { message?: string; code?: string } } | null)?.error;
        const ce = classifyError(new ApiError(res.status, { message: msg?.message ?? summary, type: '', code: msg?.code ?? null }, errBody));
        setFailure(ce.title);
      }
    } catch (err) {
      if (!abort.signal.aborted) {
        setFailure(err instanceof Error ? err.message : String(err));
        summary = String(err);
      }
    } finally {
      setDoneAt(at());
      const next = addEntry(history, {
        ts: Date.now(),
        method: ep.method,
        path,
        mode: raw ? "raw" : "profiles",
        model: (() => {
          try {
            return (JSON.parse(body) as { model?: string }).model ?? active ?? null;
          } catch {
            return active || null;
          }
        })(),
        status: code,
        duration_ms: at(),
        body: ep.body ? body : null,
        response_summary: summary,
      });
      setHistory(next);
      saveHistory(next);
      setBusy(false);
      ctrl.current = null;
    }
  }

  const sum = summarize(ep.shape, rows, doneAt);
  let input: unknown = null;
  try {
    input = ep.body ? JSON.parse(body) : null;
  } catch {
    input = null;
  }
  const snippetInput = { origin: location.origin.replace("//0.0.0.0", "//127.0.0.1"), endpoint: ep, path: resolvePath(ep, param), body: input, raw, auth: keyRequired };

  const grouped = useMemo(
    () => GROUP_ORDER.map((g) => ({ g, items: ENDPOINTS.filter((e) => e.group === g) })),
    [],
  );

  return (
    <ToolPage tool="playground">
      <div class="pg-columns">
        <Section label={t("tools.pg.request")}>
          <div class="stack">
            <label class="stack pg-field">
              <span class="label">{t("tools.pg.endpoint")}</span>
              <span class="select">
                <select value={endpoint} disabled={busy} onChange={(e) => choose(e.currentTarget.value as EndpointId)}>
                  {grouped.map(({ g, items }) => (
                    <optgroup key={g} label={t(`tools.pg.group.${g}`)}>
                      {items.map((e) => (
                        <option key={e.id} value={e.id}>
                          {endpointLabel(e)}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </span>
            </label>
            {ep.param && (
              <label class="stack pg-field">
                <span class="label">{t(`tools.pg.param.${ep.param}`)}</span>
                <TextInput class="mono" value={param} onChange={setParam} invalid={!!param && pErr === "response_id"} />
                {param && pErr === "response_id" && <span class="field-error">{t("tools.pg.param_response")}</span>}
              </label>
            )}
            <div class="stack pg-field">
              <span class="label" id="pg-mode">
                {t("tools.pg.mode")}
              </span>
              <SegmentedControl
                label={t("tools.pg.mode")}
                value={raw ? "raw" : "profiles"}
                options={[
                  { value: "profiles", label: t("tools.pg.mode.profiles") },
                  { value: "raw", label: t("tools.pg.mode.raw") },
                ]}
                onChange={(v) => setRaw(v === "raw")}
              />
              <p class="field-help">{t(raw ? "tools.pg.mode_help.raw" : "tools.pg.mode_help.profiles")}</p>
            </div>
            {ep.body && (
              <>
                <JsonEditor
                  id="pg-body"
                  label={t("tools.pg.body")}
                  value={body}
                  onChange={(v) => (setBody(v), setTruncatedNote(false))}
                  rows={18}
                  onSubmit={() => void send()}
                  extra={
                    templatesFor(ep.id).length > 0 && (
                      <Select
                        aria-label={t("tools.pg.template")}
                        value=""
                        options={[
                          { value: "", label: `${t("tools.pg.template")} ▾` },
                          ...templatesFor(ep.id).map((tpl) => ({ value: tpl.id, label: t(tpl.label as "tools.template.chat") })),
                        ]}
                        onChange={(v) => {
                          const tpl = templatesFor(ep.id).find((x) => x.id === v);
                          if (tpl) setBody(JSON.stringify(tpl.build(active), null, 2));
                        }}
                      />
                    )
                  }
                />
                {active && (
                  <Button size="s" variant="text" onClick={() => setBody(withModel(body, active))}>
                    {t("tools.model")}: <span class="mono">{active}</span>
                  </Button>
                )}
                {truncatedNote && <Banner tone="info">{t("tools.pg.history_truncated")}</Banner>}
              </>
            )}
            <Disclosure summary={t("tools.pg.headers")}>
              <p class="meta">{keyRequired && auth.value.authenticated ? t("tools.pg.headers_auth") : t("tools.pg.headers_none")}</p>
            </Disclosure>
            <div class="cluster">
              {busy ? (
                <Button variant="solid" onClick={() => ctrl.current?.abort()}>
                  {t("tools.pg.stop")}
                </Button>
              ) : rawBlocked ? (
                <Tooltip text={t("tools.pg.raw_needs_engine")}>
                  <Button variant="accent" aria-disabled="true">
                    {t("tools.pg.send")}
                  </Button>
                </Tooltip>
              ) : (
                <Button variant={canSend ? "accent" : "outline"} disabled={!canSend} onClick={() => void send()}>
                  {t("tools.pg.send")}
                </Button>
              )}
              {raw && <Tag>{t("tools.pg.raw_tag")}</Tag>}
              <span class="meta">⌘↵</span>
              <Menu
                label={t("tools.pg.copy_as")}
                variant="text"
                items={KINDS.map((k) => ({
                  key: k,
                  label: copyLabel(k),
                  disabled: !snippetAvailable(k, ep),
                  detail: snippetAvailable(k, ep) ? undefined : t("tools.pg.copy_disabled"),
                  onSelect: () => {
                    const text = snippet(k, snippetInput);
                    if (text) void copyText(text).then((ok) => ok && toast(t("tools.pg.copied", { what: copyLabel(k) })));
                  },
                }))}
              />
            </div>
          </div>
        </Section>
        <Section label={t("tools.pg.response")}>
          <div class="stack">
            <div class="cluster pg-status">
              {status ? (
                <span class={status.code >= 400 ? "label acc" : "label"} data-testid="pg-status">
                  {t("tools.pg.status", { status: (status.code + " " + status.text).trim(), secs: status.secs.toFixed(2) })}
                </span>
              ) : (
                <span class="meta">{busy ? "…" : t("tools.pg.idle")}</span>
              )}
              {raw && status && <span class="meta">{t("tools.pg.raw_header")}</span>}
            </div>
            {failure && <Banner tone="warn">{failure}</Banner>}
            <SegmentedControl
              label={t("tools.pg.views")}
              size="s"
              value={view}
              options={[
                { value: "raw", label: t("tools.pg.view.raw") },
                { value: "parsed", label: t("tools.pg.view.parsed") },
                { value: "headers", label: t("tools.pg.view.headers") },
              ]}
              onChange={setView}
            />
            {view === "raw" && rows.length > 0 && (
              <>
                <Table
                  class="pg-events"
                  caption={t("tools.pg.view.raw")}
                  rows={rows}
                  rowKey={(r) => String(r.index)}
                  columns={[
                    { key: "n", label: t("tools.pg.col.n"), align: "right", render: (r) => <span class="tnum">{r.index}</span> },
                    { key: "t", label: t("tools.pg.col.t"), align: "right", render: (r) => <span class="tnum">{Math.round(r.t)}</span> },
                    { key: "event", label: t("tools.pg.col.event"), render: (r) => <span class={r.kind === "comment" ? "mute mono" : "mono"}>{r.name}</span> },
                    {
                      key: "data",
                      label: t("tools.pg.col.data"),
                      render: (r) =>
                        r.data ? (
                          <button type="button" class="text-button mono pg-data" aria-expanded={open === r.index} onClick={() => setOpen(open === r.index ? null : r.index)}>
                            {preview(r.data, 80)}
                          </button>
                        ) : null,
                    },
                  ]}
                  detail={(r) => (open === r.index ? <CodeBlock code={pretty(r.data)} /> : null)}
                />
                {doneAt !== null && (
                  <p class="meta tnum">
                    {t("tools.pg.footer", { n: sum.events, fd: ms(sum.firstData), ft: ms(sum.firstToken), done: ms(sum.done) })}
                  </p>
                )}
              </>
            )}
            {view === "parsed" &&
              (plain !== null ? (
                <CodeBlock code={pretty(plain)} />
              ) : parsed ? (
                <div class="stack">
                  {parsed.error != null && <CodeBlock label={t("tools.pg.parsed.error")} code={JSON.stringify(parsed.error, null, 2)} />}
                  {parsed.text && <CodeBlock label={t("tools.pg.parsed.text")} code={parsed.text} wrap />}
                  {parsed.reasoning && <CodeBlock label={t("tools.pg.parsed.reasoning")} code={parsed.reasoning} wrap />}
                  {parsed.toolCalls.length > 0 && <CodeBlock label={t("tools.pg.parsed.tools")} code={JSON.stringify(parsed.toolCalls, null, 2)} />}
                  {parsed.usage != null && <CodeBlock label={t("tools.pg.parsed.usage")} code={JSON.stringify(parsed.usage, null, 2)} />}
                  {parsed.timings != null && <CodeBlock label={t("tools.pg.parsed.timings")} code={JSON.stringify(parsed.timings, null, 2)} />}
                  {parsed.progress != null && <CodeBlock label={t("tools.pg.parsed.progress")} code={JSON.stringify(parsed.progress, null, 2)} />}
                </div>
              ) : null)}
            {view === "headers" && headers.length > 0 && (
              <Table
                caption={t("tools.pg.view.headers")}
                rows={headers}
                rowKey={(h) => h[0]}
                columns={[
                  { key: "k", label: t("tools.pg.header"), render: (h) => <span class="mono">{h[0]}</span> },
                  { key: "v", label: t("tools.pg.value"), render: (h) => <span class="mono pg-data">{h[1]}</span> },
                ]}
              />
            )}
          </div>
        </Section>
      </div>
      <Section label={t("tools.pg.history")} meta={t("tools.pg.history_meta")}>
        <div class="stack">
          {history.length === 0 ? (
            <p class="meta">{t("tools.pg.history_empty")}</p>
          ) : (
            <ul class="pg-history">
              {history.map((h) => (
                <li key={h.id} class="pg-history-row">
                  <button
                    type="button"
                    class="text-button pg-history-load"
                    aria-label={t("tools.pg.history_load", { method: h.method, path: h.path })}
                    onClick={() => {
                      const found = endpointFor(h.method, h.path);
                      if (!found) return;
                      setEndpoint(found.endpoint.id);
                      setParam(found.param);
                      if (h.body !== null) setBody(h.body);
                      setRaw(h.mode === "raw");
                      setTruncatedNote(!!h.truncated);
                    }}
                  >
                    <span class="tnum">{new Date(h.ts).toTimeString().slice(0, 5)}</span> · <span class="mono">
                      {h.method} {h.path}
                    </span>{" "}
                    · <span class={h.status !== null && h.status >= 400 ? "acc" : undefined}>{h.status ?? "—"}</span> · {((h.duration_ms ?? 0) / 1000).toFixed(2)} s ·{" "}
                    {h.mode === "raw" ? t("tools.pg.raw_tag") : t("tools.pg.mode.profiles")}
                    {h.model ? <> · <span class="mono">{h.model}</span></> : null}
                  </button>
                  {h.response_summary && <span class="meta pg-summary">{h.response_summary}</span>}
                  <Button
                    size="s"
                    variant="text"
                    class="pg-history-del"
                    onClick={() => {
                      const next = removeEntry(history, h.id);
                      setHistory(next);
                      saveHistory(next);
                    }}
                  >
                    {t("tools.pg.history_delete")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {history.length > 0 &&
            (clearing ? (
              <span class="cluster" role="alert">
                <span class="meta">{t("tools.pg.history_clear_q")}</span>
                <Button
                  size="s"
                  variant="solid"
                  onClick={() => {
                    setHistory([]);
                    saveHistory([]);
                    setClearing(false);
                  }}
                >
                  {t("tools.pg.history_clear")}
                </Button>
                <Button size="s" variant="text" onClick={() => setClearing(false)}>
                  {t("common.cancel")}
                </Button>
              </span>
            ) : (
              <Button size="s" variant="text" onClick={() => setClearing(true)}>
                {t("tools.pg.history_clear")}
              </Button>
            ))}
        </div>
      </Section>
    </ToolPage>
  );
}
