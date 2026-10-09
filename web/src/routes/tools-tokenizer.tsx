import { useState } from "preact/hooks";
import { ApiError, api, request, webClient } from "../api/client";
import type { TokenPieces } from "../api/models";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { SegmentedControl } from "../components/controls";
import { CopyButton, copyText } from "../components/CopyButton";
import { Select, TextArea } from "../components/inputs";
import { Section } from "../components/Section";
import { toast } from "../components/Toast";
import { Toggle } from "../components/Toggle";
import { formatCount } from "../lib/format";
import { useApi } from "../lib/use-api";
import { engine } from "../store";
import { t } from "../strings/tools";
import "../styles/pages/tools.css";
import { REASONING_EFFORTS } from "./chat/logic";
import { ToolPage, engineReady } from "./tools";
import { JsonEditor, jsonProblem } from "./tools/JsonEditor";
import { countSpecial, isSpecialText, splitSpecial, utf8Length, visibleWhitespace, type TokenPiece } from "./tools/tokens";

type Tab = "tokenize" | "template" | "count";
const TABS: readonly Tab[] = ["tokenize", "template", "count"];

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function Tokenizer() {
  const [tab, setTab] = useState<Tab>("tokenize");
  const model = engine.value?.model ?? undefined;
  const ready = engineReady();
  return (
    <ToolPage tool="tokenizer">
      <nav class="subnav tool-tabs" aria-label={t("tools.tok.tabs")}>
        {TABS.map((k) => (
          <button key={k} type="button" class="navlink nav" aria-current={tab === k ? "page" : undefined} onClick={() => setTab(k)}>
            {t(`tools.tok.tab.${k}`)}
          </button>
        ))}
      </nav>
      {tab === "tokenize" && <TokenizeTab model={model} ready={ready} />}
      {tab === "template" && <TemplateTab model={model} ready={ready} />}
      {tab === "count" && <CountTab model={model} ready={ready} />}
    </ToolPage>
  );
}

const tokenizeFn = (model: string | undefined) => async (content: string, add_special: boolean) =>
  (await request<{ tokens: number[] }>("/tokenize", { body: { model, content, add_special }, headers: webClient("tokenizer") })).tokens;

/**
 * Pieces from the manager (`POST /tokenizer/pieces`: the model's own tokenizer under Splash's
 * Python); `503 pieces_unavailable` or any failure keeps the IDs only.
 */
export async function piecesFor(ids: number[], model: string | undefined): Promise<{ ok: boolean; tokens: TokenPiece[] }> {
  try {
    const res = await api.post<TokenPieces>("/tokenizer/pieces", { ids, ...(model ? { model } : {}) });
    const byIndex = res.pieces;
    return {
      ok: true,
      tokens: ids.map((id, i) => {
        const p = byIndex[i]?.id === id ? byIndex[i] : byIndex.find((x) => x.id === id);
        const text = p?.text ?? null;
        return { id, piece: text, special: isSpecialText(text ?? "") || isSpecialText(p?.piece ?? ""), vocab: p?.piece ?? null };
      }),
    };
  } catch (err) {
    if (err instanceof ApiError && err.code !== "pieces_unavailable" && err.status !== 503) throw err;
    return { ok: false, tokens: ids.map((id) => ({ id, piece: null, special: false })) };
  }
}

function TokenizeTab({ model, ready }: { model: string | undefined; ready: boolean }) {
  const [text, setText] = useState("The quick brown fox jumps over the lazy dog.");
  const [special, setSpecial] = useState(false);
  const [tokens, setTokens] = useState<TokenPiece[] | null>(null);
  const [piecesOk, setPiecesOk] = useState(true);
  const [view, setView] = useState<"pieces" | "ids">("pieces");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const ids = await tokenizeFn(model)(text, special);
      const res = await piecesFor(ids, model);
      setTokens(res.tokens);
      setPiecesOk(res.ok);
      if (!res.ok) setView("ids");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const showPieces = view === "pieces" && piecesOk;
  return (
    <>
      <Section label={t("tools.tok.text")}>
        <div class="stack">
          <TextArea
            class="tool-text"
            rows={6}
            value={text}
            aria-label={t("tools.tok.text")}
            onChange={setText}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void run();
              }
            }}
          />
          <div class="cluster">
            <Toggle checked={special} onChange={setSpecial} label={t("tools.tok.add_special")} />
            <span class="label">{t("tools.tok.add_special")}</span>
            <span class="meta flag mono">add_special</span>
            <Button variant={ready ? "accent" : "outline"} disabled={!ready || busy} loading={busy} title={ready ? undefined : t("tools.needs_ready")} onClick={() => void run()}>
              {t("tools.tok.run")}
            </Button>
          </div>
          <p class="field-help">{t("tools.tok.add_special_help")}</p>
          {error && <Banner tone="warn" title={t("tools.tok.failed")}>{error}</Banner>}
        </div>
      </Section>
      {tokens && (
        <Section label={t("tools.tok.tokens", { n: tokens.length })}>
          <div class="stack">
            <div class="cluster">
              <SegmentedControl
                label={t("tools.tok.view")}
                size="s"
                value={showPieces ? "pieces" : "ids"}
                options={[
                  { value: "pieces", label: t("tools.tok.view.pieces"), disabled: !piecesOk },
                  { value: "ids", label: t("tools.tok.view.ids") },
                ]}
                onChange={setView}
              />
              <CopyButton text={JSON.stringify(tokens.map((x) => x.id))} label={t("tools.tok.copy_ids")} />
            </div>
            <ul class="token-list" aria-label={t("tools.tok.tokens", { n: tokens.length })}>
              {tokens.map((tk, i) => (
                <li key={i}>
                  <button
                    type="button"
                    class="token-piece"
                    data-special={tk.special ? "true" : undefined}
                    data-alt={i % 2 === 1 ? "true" : undefined}
                    title={`${t("tools.tok.chip_tip", { id: tk.id, bytes: tk.piece ? utf8Length(tk.piece) : "—" })}${(tk as TokenPiece & { vocab?: string | null }).vocab ? ` · ${(tk as TokenPiece & { vocab?: string | null }).vocab}` : ""}`}
                    onClick={() => void copyText(String(tk.id)).then((ok) => ok && toast(t("tools.tok.copied_id", { id: tk.id })))}
                  >
                    {showPieces ? (
                      <>
                        <span class="mono">{tk.piece === null ? "·" : visibleWhitespace(tk.piece)}</span>
                        <small class="tnum">{tk.id}</small>
                      </>
                    ) : (
                      <span class="tnum">{tk.id}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
            <p class="meta">{piecesOk ? t("tools.tok.pieces_note") : t("tools.tok.ids_only")}</p>
          </div>
        </Section>
      )}
    </>
  );
}

function TemplateTab({ model, ready }: { model: string | undefined; ready: boolean }) {
  const [messages, setMessages] = useState(JSON.stringify([{ role: "system", content: "You are concise." }, { role: "user", content: "Hello" }], null, 2));
  const [tools, setTools] = useState("[]");
  const [effort, setEffort] = useState("");
  const [kwargs, setKwargs] = useState("{}");
  const [gen, setGen] = useState(true);
  const [prompt, setPrompt] = useState<string | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [ws, setWs] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const raw = useApi((s) => request<Record<string, unknown>>("/api/admin/engine/status", { signal: s }), [model], ready);
  const later = (raw.data?.chat_template as { later_system?: unknown } | undefined)?.later_system;
  const problems = [jsonProblem(messages, (v) => (Array.isArray(v) ? null : "Messages must be an array.")), jsonProblem(tools, (v) => (Array.isArray(v) ? null : "Tools must be an array.")), jsonProblem(kwargs, (v) => (v && typeof v === "object" && !Array.isArray(v) ? null : "Kwargs must be an object."))].filter(Boolean);
  async function render() {
    if (!ready || problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const toolList = JSON.parse(tools) as unknown[];
      const kw = JSON.parse(kwargs) as Record<string, unknown>;
      const res = await request<{ prompt: string }>("/apply-template", {
        headers: webClient("tokenizer"),
        body: {
          model,
          messages: JSON.parse(messages),
          ...(toolList.length ? { tools: toolList } : {}),
          ...(effort ? { reasoning_effort: effort } : {}),
          ...(Object.keys(kw).length ? { chat_template_kwargs: kw } : {}),
          add_generation_prompt: gen,
        },
      });
      setPrompt(res.prompt);
      setCount((await tokenizeFn(model)(res.prompt, false)).length);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Section label={t("tools.tok.messages")}>
        <div class="tool-grid">
          <JsonEditor id="tpl-messages" label={t("tools.tok.messages")} value={messages} onChange={setMessages} rows={10} onSubmit={() => void render()} />
          <JsonEditor id="tpl-tools" label={t("tools.tok.tools")} value={tools} onChange={setTools} rows={10} />
          <div class="stack">
            <span class="label">{t("tools.tok.options")}</span>
            <label class="stack">
              <span class="label">{t("tools.tok.effort")}</span>
              <Select value={effort} options={[{ value: "", label: t("tools.tok.effort_default") }, ...REASONING_EFFORTS.map((e) => ({ value: e, label: e }))]} onChange={setEffort} />
            </label>
            <JsonEditor id="tpl-kwargs" label={t("tools.tok.kwargs")} value={kwargs} onChange={setKwargs} rows={3} />
            <div class="cluster">
              <Toggle checked={gen} onChange={setGen} label={t("tools.tok.gen_prompt")} />
              <span class="label">{t("tools.tok.gen_prompt")}</span>
            </div>
          </div>
        </div>
        <div class="cluster">
          <Button variant={ready && !problems.length ? "accent" : "outline"} disabled={!ready || busy || problems.length > 0} loading={busy} onClick={() => void render()}>
            {t("tools.tok.render")}
          </Button>
          <span class="meta">{t("tools.tok.template_help")}</span>
        </div>
        {error && <Banner tone="warn" title={t("tools.tok.failed")}>{error}</Banner>}
      </Section>
      {prompt !== null && (
        <Section label={t("tools.tok.rendered")} meta={t("tools.tok.rendered_meta", { tokens: count === null ? "—" : formatCount(count), special: countSpecial(prompt) })}>
          <div class="stack">
            <div class="cluster">
              <Toggle checked={ws} onChange={setWs} label={t("tools.tok.whitespace")} />
              <span class="label">{t("tools.tok.whitespace")}</span>
              <CopyButton text={prompt} />
            </div>
            <pre class="mono tool-prompt" tabIndex={0}>
              {splitSpecial(prompt).map((part, i) =>
                part.special ? (
                  <span key={i} class="tool-special">
                    {part.text}
                  </span>
                ) : (
                  <span key={i}>{ws ? part.text.replace(/\n/g, "⏎\n") : part.text}</span>
                ),
              )}
            </pre>
            {later !== undefined && (
              <p class="meta">
                {t("tools.tok.mode")} · {t("tools.tok.mode_value", { v: typeof later === "string" ? later : JSON.stringify(later) })}
              </p>
            )}
          </div>
        </Section>
      )}
    </>
  );
}

function CountTab({ model, ready }: { model: string | undefined; ready: boolean }) {
  const [messages, setMessages] = useState(JSON.stringify([{ role: "user", content: "Hello" }], null, 2));
  const [system, setSystem] = useState("");
  const [tools, setTools] = useState("[]");
  const [count, setCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = [jsonProblem(messages, (v) => (Array.isArray(v) ? null : "Messages must be an array.")), jsonProblem(tools, (v) => (Array.isArray(v) ? null : "Tools must be an array."))].filter(Boolean);
  async function run() {
    if (!ready || problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const toolList = JSON.parse(tools) as unknown[];
      const res = await request<{ input_tokens: number }>("/v1/messages/count_tokens", {
        headers: webClient("tokenizer"),
        body: { model, messages: JSON.parse(messages), ...(system ? { system } : {}), ...(toolList.length ? { tools: toolList } : {}) },
      });
      setCount(res.input_tokens);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function addImage(file: File) {
    const data = await new Promise<string>((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] ?? "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
    try {
      const list = JSON.parse(messages) as Array<{ role: string; content: unknown }>;
      const last = list[list.length - 1] ?? { role: "user", content: [] };
      const blocks = typeof last.content === "string" ? [{ type: "text", text: last.content }] : Array.isArray(last.content) ? [...last.content] : [];
      blocks.push({ type: "image", source: { type: "base64", media_type: file.type, data } });
      if (list.length === 0) list.push({ role: "user", content: blocks });
      else list[list.length - 1] = { ...last, content: blocks };
      setMessages(JSON.stringify(list, null, 2));
    } catch {
      /* the editor shows the JSON error */
    }
  }
  return (
    <>
      <Section label={t("tools.tok.messages")}>
        <div class="tool-grid">
          <JsonEditor id="count-messages" label={t("tools.tok.messages")} value={messages} onChange={setMessages} rows={10} onSubmit={() => void run()} />
          <label class="stack">
            <span class="label">{t("tools.tok.system")}</span>
            <TextArea rows={6} value={system} onChange={setSystem} />
          </label>
          <JsonEditor id="count-tools" label={t("tools.tok.tools")} value={tools} onChange={setTools} rows={6} />
        </div>
        <div class="cluster">
          <label class="btn" data-variant="text" data-size="s">
            {t("tools.tok.add_image")}
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              class="visually-hidden"
              onChange={(e) => {
                const f = e.currentTarget.files?.[0];
                if (f) void addImage(f);
                e.currentTarget.value = "";
              }}
            />
          </label>
          <Button variant={ready && !problems.length ? "accent" : "outline"} disabled={!ready || busy || problems.length > 0} loading={busy} onClick={() => void run()}>
            {t("tools.tok.count")}
          </Button>
        </div>
        {error && <Banner tone="warn" title={t("tools.tok.failed")}>{error}</Banner>}
      </Section>
      {count !== null && (
        <Section label={t("tools.tok.input_tokens")}>
          <p class="display-number tnum">{formatCount(count)}</p>
          <p class="meta">{t("tools.tok.count_help")}</p>
        </Section>
      )}
    </>
  );
}
