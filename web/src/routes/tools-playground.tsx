import { useEffect, useRef, useState } from "preact/hooks";
import { request } from "../api/client";
import { readSse } from "../api/stream";
import { Button, CodeBlock, LoadError, Section } from "../components";
import { engine } from "../store";
import { ToolPage } from "./tools";
import {
  defaultBody,
  ENDPOINTS,
  endpointById,
  endpointFor,
  endpointLabel,
  resolvePath,
  templatesFor,
} from "./tools/endpoints";
import {
  curlSnippet,
  pythonOpenAiSnippet,
  jsFetchSnippet,
} from "./tools/snippets";
import { addEntry, loadHistory, saveHistory } from "./tools/history";
import { StreamAccumulator } from "./chat/accumulator";

export default function Playground() {
  const [endpoint, setEndpoint] = useState("chat");
  const ep = endpointById(endpoint);
  const [param, setParam] = useState("");
  const [body, setBody] = useState(
    defaultBody("chat", engine.value?.model ?? ""),
  );
  const [raw, setRaw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [output, setOutput] = useState("");
  const [parsed, setParsed] = useState("");
  const [view, setView] = useState("raw");
  const [status, setStatus] = useState("");
  const [headers, setHeaders] = useState("");
  const [history, setHistory] = useState(loadHistory);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);
  function choose(id: string) {
    setEndpoint(id);
    setParam("");
    setBody(defaultBody(endpointById(id).id, engine.value?.model ?? ""));
  }
  async function send() {
    const abort = new AbortController();
    ctrl.current = abort;
    setBusy(true);
    setError(null);
    setOutput("");
    setParsed("");
    setHeaders("");
    setStatus("");
    const started = performance.now();
    const path = resolvePath(ep, param);
    let code: number | null = null;
    let text = "";
    try {
      const payload = ep.body ? JSON.parse(body) : undefined;
      const response = await request<Response>(
        raw ? "/api/admin/engine/raw" + path : path,
        { method: ep.method, body: payload, raw: true, signal: abort.signal },
      );
      code = response.status;
      setStatus(`${code} ${response.statusText}`);
      setHeaders(
        [...response.headers.entries()]
          .map(([k, v]) => `${k}: ${v}`)
          .join("\n"),
      );
      if (response.headers.get("content-type")?.includes("text/event-stream")) {
        const accumulator = new StreamAccumulator();
        for await (const event of readSse(response, abort.signal)) {
          text += `${(performance.now() - started).toFixed(0)} ms · ${event.event || "message"}\n${event.data}\n\n`;
          setOutput(text);
          if (event.data !== "[DONE]") {
            try {
              const chunk: unknown = JSON.parse(event.data);
              accumulator.push(chunk);
              setParsed(
                ep.shape === "chat"
                  ? JSON.stringify(
                      {
                        content: accumulator.content,
                        reasoning: accumulator.reasoning,
                        tool_calls: accumulator.toolCalls,
                        usage: accumulator.usage,
                        timings: accumulator.timings,
                      },
                      null,
                      2,
                    )
                  : JSON.stringify(chunk, null, 2),
              );
            } catch {
              /* raw view retains non-JSON events */
            }
          }
        }
      } else {
        text = await response.text();
        setOutput(text);
        try {
          setParsed(JSON.stringify(JSON.parse(text), null, 2));
        } catch {
          setParsed(text);
        }
      }
    } catch (e) {
      if (!abort.signal.aborted) setError(e);
      text ||= String(e);
    } finally {
      const next = addEntry(history, {
        ts: Date.now(),
        method: ep.method,
        path,
        mode: raw ? "raw" : "profiles",
        model: engine.value?.model ?? null,
        status: code,
        duration_ms: performance.now() - started,
        body: ep.body ? body : null,
        response_summary: text,
      });
      setHistory(next);
      saveHistory(next);
      setBusy(false);
      ctrl.current = null;
    }
  }
  let input: unknown = null;
  try {
    input = ep.body ? JSON.parse(body) : null;
  } catch {
    /* editor displays on send */
  }
  const snippet = {
    origin: location.origin,
    endpoint: ep,
    path: resolvePath(ep, param),
    body: input,
    raw,
    auth: true,
  };
  return (
    <ToolPage tool="playground">
      <Section label="Request">
        <div class="stack">
          <label>
            Endpoint
            <select
              value={endpoint}
              disabled={busy}
              onChange={(e) => choose(e.currentTarget.value)}
            >
              {ENDPOINTS.map((e) => (
                <option key={e.id} value={e.id}>
                  {endpointLabel(e)}
                </option>
              ))}
            </select>
          </label>
          {ep.param && (
            <label>
              {ep.param}
              <input
                value={param}
                onInput={(e) => setParam(e.currentTarget.value)}
              />
            </label>
          )}
          {ep.body && (
            <>
              <label>
                Template
                <select
                  onChange={(e) =>
                    setBody(
                      JSON.stringify(
                        templatesFor(ep.id)
                          .find((t) => t.id === e.currentTarget.value)
                          ?.build(engine.value?.model ?? "") ?? {},
                        null,
                        2,
                      ),
                    )
                  }
                >
                  <option value="">Choose template</option>
                  {templatesFor(ep.id).map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.id.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Request JSON
                <textarea
                  class="mono"
                  rows={18}
                  value={body}
                  onInput={(e) => setBody(e.currentTarget.value)}
                />
              </label>
            </>
          )}
          <label>
            <input
              type="checkbox"
              checked={raw}
              onChange={(e) => setRaw(e.currentTarget.checked)}
            />
            Raw to engine · skip profile injection
          </label>
          <div class="cluster">
            <Button
              variant="accent"
              disabled={busy || (!!ep.param && !param)}
              onClick={() => void send()}
            >
              Send
            </Button>
            {busy && (
              <Button onClick={() => ctrl.current?.abort()}>Cancel</Button>
            )}
          </div>
        </div>
      </Section>
      <Section label="Response">
        {!!error && <LoadError thing="request" error={error} />}
        <p role="status">{status}</p>
        <details>
          <summary>Headers</summary>
          <pre>{headers}</pre>
        </details>
        <label>
          View
          <select value={view} onChange={(e) => setView(e.currentTarget.value)}>
            <option value="raw">Raw</option>
            <option value="parsed">Parsed</option>
          </select>
        </label>
        <CodeBlock code={view === "raw" ? output : parsed} />
      </Section>
      <Section label="Copy as">
        <details>
          <summary>curl</summary>
          <CodeBlock code={curlSnippet(snippet)} />
        </details>
        <details>
          <summary>Python · OpenAI</summary>
          <CodeBlock code={pythonOpenAiSnippet(snippet)} />
        </details>
        <details>
          <summary>JavaScript · fetch</summary>
          <CodeBlock code={jsFetchSnippet(snippet)} />
        </details>
      </Section>
      <Section label="Request history">
        {history.map((h) => (
          <div key={h.id}>
            <button
              class="text-button"
              disabled={busy}
              onClick={() => {
                const found = endpointFor(h.method, h.path);
                if (found) {
                  setEndpoint(found.endpoint.id);
                  setParam(found.param);
                  setBody(h.body ?? "");
                  setRaw(h.mode === "raw");
                }
              }}
            >
              {h.method} {h.path} · {h.status ?? "error"} ·{" "}
              {h.duration_ms?.toFixed(0)} ms
            </button>
            <p class="meta">{h.response_summary}</p>
          </div>
        ))}
      </Section>
    </ToolPage>
  );
}
