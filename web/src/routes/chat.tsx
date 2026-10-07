import type { ComponentChildren } from "preact";
import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Link, useLocation } from "wouter-preact";
import { ApiError, api, request } from "../api/client";
import { postStream } from "../api/stream";
import type { AttachmentUpload, ChatList, McpCallResult, McpServers, McpToolList, ProfilesView } from "../api/models";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { CodeBlock } from "../components/CodeBlock";
import { ConfirmSheet } from "../components/ConfirmSheet";
import { ProgressBar } from "../components/ProgressBar";
import { Sheet } from "../components/Sheet";
import { Empty, LoadError } from "../components/States";
import { toast, toastError } from "../components/Toast";
import { formatCount } from "../lib/format";
import { stateDisplay } from "../lib/engine-state";
import { useApi } from "../lib/use-api";
import { useTitle } from "../lib/title";
import { engine, settings } from "../store";
import { t } from "../strings/chat";
import "../styles/pages/chat.css";
import { StreamAccumulator, ensureCallIds, estimateTokens } from "./chat/accumulator";
import { Composer } from "./chat/Composer";
import { ConversationList } from "./chat/ConversationList";
import {
  MAX_AUTO_RETRIES,
  attachmentError,
  autoRetry,
  autoTitle,
  checkTools,
  classifyError,
  emptySampling,
  hasSamplingErrors,
  modelRows,
  outputBody,
  outputError,
  outputForm,
  requestModel,
  samplingBody,
  samplingErrors,
  samplingForm,
  shortModel,
  toolChoiceBody,
  toolChoiceError,
  type OutputForm,
  type SamplingForm,
  type ToolChoiceKind,
} from "./chat/logic";
import { isCancelled, withInstallConfirm } from "../lib/engine-install";
import { ErrorBanner, type ChatErrorState } from "./chat/ErrorBanner";
import { adminReturnPath } from "./chat/returnPath";
import { AdminLinks, HomeLink, Sidebar } from "./chat/Sidebar";
import { MessageView, type ToolContext, type ToolDraft } from "./chat/MessageView";
import { ContextMeter, StatTiles } from "./chat/ChatStats";
import { ModelSelector } from "./chat/ModelSelector";
import { liveSample } from "../store/live";
import { PANEL_TABS, SidePanel, type PanelTab } from "./chat/SidePanel";
import { branchInfo, defaultLeaf, newId, pathTo, rememberLeaf, removeBranch, replyCount, switchBranch, type LeafMemory } from "./chat/tree";
import { metaOf, textOf, toolCallsOf, type Chat, type ChatMessage, type ChatSummary, type DraftAttachment, type ModelEntry, type ToolCall } from "./chat/types";

const PANEL_KEY = "chat.panel.open";
const LIST_KEY = "chat.list.open";
/** Router base without the trailing slash ("/admin"); the shell keeps its own copy. */
const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const LAST_KEY = "chat.panel.last";
const now = () => new Date().toISOString();

function readLocal<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}
function writeLocal(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* per-browser convenience only */
  }
}

interface PanelLast {
  sampling?: Record<string, unknown>;
  mode?: "manual" | "mcp";
  servers?: Record<string, boolean>;
}

function blankChat(model: string | null): Chat {
  return { id: "", title: t("chat.default_title"), created_at: now(), updated_at: now(), model, profile: "default", system: "", sampling: {}, tools: [], tool_choice: "auto", response_format: null, messages: [], active_leaf: null };
}

function fileName(file: string): string {
  return file.split("/").pop() ?? file;
}

async function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

type Width = "wide" | "mid" | "narrow";

const widthOf = (x: number): Width => (x >= 1100 ? "wide" : x >= 900 ? "mid" : "narrow");

/** Wide: three columns; mid: the panel becomes a sheet; narrow: the conversations too (07 §2). */
function useWidth(ref: { current: HTMLElement | null }): Width {
  const [w, setW] = useState<Width>(() => widthOf(typeof innerWidth === "number" && innerWidth > 0 ? innerWidth : 1200));
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => {
      setW(widthOf(entry?.contentRect.width ?? 1200));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return w;
}

/** Within 48 px of the end: the thread follows new output (07 §2: auto-follow until the user scrolls up). */
export function nearBottom(el: Pick<HTMLElement, "scrollHeight" | "scrollTop" | "clientHeight">): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48;
}

/**
 * The workspace fills the viewport (chat.css), so the message list is the scrolling region at
 * every width. It follows new output until the user scrolls up.
 */
function useThreadFollow(messages: { current: HTMLElement | null }, mounted: unknown, content: unknown) {
  const follow = useRef(true);
  useEffect(() => {
    const el = messages.current;
    if (!el) return;
    // Only a scroll upwards stops following: the event of our own scroll to the end can arrive
    // after the reply has grown again, when the list is no longer near its end.
    let lastTop = el.scrollTop;
    const onScroll = () => {
      if (nearBottom(el)) follow.current = true;
      else if (el.scrollTop < lastTop) follow.current = false;
      lastTop = el.scrollTop;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    // The composer growing (or the window shrinking) makes the list shorter without a scroll
    // event: keep the end in view while following.
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
      if (follow.current) el.scrollTop = el.scrollHeight;
    });
    ro?.observe(el);
    // Markdown and code highlighting can grow a reply after the render that scrolled.
    let frame = 0;
    const mo = typeof MutationObserver === "undefined" ? null : new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (follow.current) el.scrollTop = el.scrollHeight;
      });
    });
    mo?.observe(el, { childList: true, subtree: true, characterData: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      ro?.disconnect();
      mo?.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [mounted]);
  useEffect(() => {
    const el = messages.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [content]);
  return follow;
}

export default function ChatPage({ params }: { params?: { cid?: string } }) {
  const cid = params?.cid ? decodeURIComponent(params.cid) : null;
  const [, navigate] = useLocation();
  const e = engine.value;
  const active = e?.model ?? null;
  const autoLoad = (settings.value?.settings?.global?.routing as { auto_load?: boolean } | undefined)?.auto_load !== false;

  // ---------- list ----------
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setQ(query.trim()), 200);
    return () => clearTimeout(id);
  }, [query]);
  const list = useApi((s) => api.get<ChatList>("/chats", q ? { q } : undefined, s), [q]);

  // ---------- models ----------
  const models = useApi((s) => request<{ data: ModelEntry[] }>("/v1/models", { signal: s }), [active]);
  const rows = useMemo(() => modelRows(models.data?.data ?? []), [models.data]);

  // ---------- the open conversation ----------
  const [chat, setChat] = useState<Chat | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [notFound, setNotFound] = useState(false);
  const chatRef = useRef<Chat | null>(null);
  chatRef.current = chat;
  /** A conversation this page just created: its URL changes, but it must not be re-fetched. */
  const createdId = useRef<string | null>(null);
  const memory = useRef<LeafMemory>(new Map());
  const dataUrls = useRef(new Map<string, string>());

  // panel state
  const last = useMemo(() => readLocal<PanelLast>(LAST_KEY, {}), []);
  const [sampling, setSampling] = useState<SamplingForm>(() => samplingForm(last.sampling));
  const [undo, setUndo] = useState<{ form: SamplingForm; profile: string } | null>(null);
  const [toolsText, setToolsText] = useState("[]");
  const [choice, setChoice] = useState<ToolChoiceKind>("auto");
  const [named, setNamed] = useState("");
  const [parallel, setParallel] = useState(true);
  const [mode, setMode] = useState<"manual" | "mcp">(last.mode ?? "manual");
  const [serverOn, setServerOn] = useState<Record<string, boolean>>(last.servers ?? {});
  const [output, setOutput] = useState<OutputForm>(() => outputForm(null));
  const [tab, setTab] = useState<PanelTab>("sampling");
  const layout = useRef<HTMLDivElement>(null);
  const width = useWidth(layout);
  const messagesEl = useRef<HTMLDivElement>(null);
  const following = useThreadFollow(messagesEl, `${notFound}:${!!loadError}`, chat);
  const [panelOpen, setPanelOpen] = useState(() => readLocal(PANEL_KEY, true));
  const [listOpen, setListOpen] = useState(() => readLocal(LIST_KEY, true));
  const [home] = useState(() => adminReturnPath(BASE));
  const [listSheet, setListSheet] = useState(false);
  const [panelSheet, setPanelSheet] = useState(false);

  const adopt = useCallback((doc: Chat) => {
    setChat(doc);
    setSampling(samplingForm(doc.sampling as Record<string, unknown>));
    setToolsText(JSON.stringify(doc.tools ?? [], null, 2));
    const tc = doc.tool_choice;
    if (tc && typeof tc === "object") {
      setChoice("named");
      setNamed(String(((tc as { function?: { name?: string } }).function?.name) ?? ""));
    } else setChoice(tc === "none" || tc === "required" ? tc : "auto");
    setParallel((doc.sampling as Record<string, unknown> | undefined)?.parallel_tool_calls !== false);
    setOutput(outputForm(doc.response_format as Record<string, unknown> | null));
    if (!doc.active_leaf && doc.messages?.length) setChat({ ...doc, active_leaf: defaultLeaf(doc.messages) });
  }, []);

  useEffect(() => {
    setNotFound(false);
    setLoadError(null);
    setDrafts({});
    setError(null);
    if (!cid) {
      if (!chatRef.current || chatRef.current.id) {
        const fresh = blankChat(active ?? rows[0]?.id ?? null);
        setChat({ ...fresh, sampling: last.sampling ?? {} });
        setSampling(samplingForm(last.sampling));
        setToolsText("[]");
        setChoice("auto");
        setOutput(outputForm(null));
      }
      return;
    }
    if (chatRef.current?.id === cid || createdId.current === cid) return;
    const ctrl = new AbortController();
    api
      .get<Chat>(`/chats/${encodeURIComponent(cid)}`, undefined, ctrl.signal)
      .then((doc) => adopt(doc))
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
        else setLoadError(err);
      });
    return () => ctrl.abort();
  }, [cid]);

  // A new chat picks the active model once the list arrives.
  useEffect(() => {
    if (chat && !chat.id && !chat.model && (active || rows[0])) setChat({ ...chat, model: active ?? rows[0]!.id });
  }, [active, rows.length]);

  const model = chat?.model ?? null;
  const profile = chat?.profile ?? "default";
  const profiles = useApi((s) => api.get<ProfilesView>(`/models/${model}/profiles`, undefined, s), [model], !!model);
  const profileNames = useMemo(() => {
    const names = (profiles.data?.profiles ?? []).map((p) => p.name);
    return names.length ? names : ["default"];
  }, [profiles.data]);
  const overlay = (profiles.data?.profiles.find((p) => p.name === profile)?.overlay ?? {}) as Record<string, unknown>;
  const modelDefaults = (profiles.data?.sampling_defaults ?? {}) as Record<string, unknown>;
  const row = rows.find((r) => r.id === model);
  const context = row?.context ?? null;

  // later system messages (docs/ui/07 §9.2) from the raw /status of the active model
  const raw = useApi((s) => api.get<Record<string, unknown>>("/engine/status", undefined, s), [active, model], !!active && model === active);
  const laterSystem = useMemo(() => {
    const ct = (raw.data?.chat_template ?? null) as { later_system?: unknown } | null;
    const v = ct?.later_system;
    if (typeof v === "string") return v as "native" | "patched" | "unsupported";
    if (v && typeof v === "object") {
      const map = v as Record<string, string>;
      return (map[toolsCount() > 0 ? "tool_use" : "default"] ?? map.default ?? null) as "native" | "patched" | "unsupported" | null;
    }
    return null;
  }, [raw.data]);

  // MCP
  const mcpServers = useApi((s) => api.get<McpServers>("/mcp/servers", undefined, s), [], mode === "mcp");
  const mcpTools = useApi((s) => api.post<McpToolList>("/mcp/tools", undefined, s), [], mode === "mcp");

  // ---------- derived validation ----------
  const toolCheck = useMemo(() => checkTools(toolsText), [toolsText]);
  function toolsCount() {
    return toolCheck.tools.length;
  }
  const enabledMcp = useMemo(() => {
    if (mode !== "mcp") return [];
    const servers = mcpServers.data?.servers ?? {};
    return (mcpTools.data?.tools ?? []).filter((tl) => servers[tl.server]?.enabled !== false && serverOn[tl.server] !== false);
  }, [mode, mcpServers.data, mcpTools.data, serverOn]);
  const collisions = enabledMcp.filter((tl) => toolCheck.names.includes(tl.name)).map((tl) => ({ name: tl.name, server: tl.server }));
  const allTools = useMemo(() => {
    const mcpNames = new Set(enabledMcp.map((x) => x.name));
    const own = toolCheck.tools.filter((x) => !mcpNames.has(String((x.function as { name?: string } | undefined)?.name)));
    return [
      ...own,
      ...enabledMcp.map((x) => ({ type: "function", function: { name: x.name, description: x.description ?? "", parameters: x.input_schema ?? { type: "object", properties: {} } } })),
    ];
  }, [toolCheck, enabledMcp]);
  const constrained = allTools.length > 0 || output.kind !== "text";
  const sErrors = samplingErrors(sampling, context);
  const cErr = toolChoiceError(choice, allTools.length);
  const oErr = outputError(output);

  // Persist panel values into the chat doc and remember them for new chats.
  const panelDoc = useMemo(() => {
    const body = samplingBody(sampling, { allowIgnoreEos: true });
    if (!parallel) body.parallel_tool_calls = false;
    return {
      sampling: body,
      tools: toolCheck.error ? undefined : toolCheck.tools,
      tool_choice: choice === "named" ? toolChoiceBody("named", named) : choice,
      response_format: outputBody(output),
    };
  }, [sampling, parallel, toolCheck, choice, named, output]);
  useEffect(() => {
    if (!chat) return;
    writeLocal(LAST_KEY, { sampling: panelDoc.sampling, mode, servers: serverOn } satisfies PanelLast);
    const next = { ...chat, sampling: panelDoc.sampling, tool_choice: panelDoc.tool_choice, response_format: panelDoc.response_format, ...(panelDoc.tools ? { tools: panelDoc.tools } : {}) };
    if (JSON.stringify([next.sampling, next.tools, next.tool_choice, next.response_format]) !== JSON.stringify([chat.sampling, chat.tools, chat.tool_choice, chat.response_format])) {
      setChat(next);
      if (next.id) saveSoon(next, 800);
    }
  }, [panelDoc, mode, serverOn]);

  // ---------- persistence: coalesced PUTs, latest wins (§8.4) ----------
  const saving = useRef<{ inFlight: boolean; next: Chat | null; timer: ReturnType<typeof setTimeout> | undefined }>({ inFlight: false, next: null, timer: undefined });
  const flush = useCallback(async () => {
    const s = saving.current;
    if (s.inFlight || !s.next) return;
    const doc = s.next;
    s.next = null;
    s.inFlight = true;
    try {
      await api.put<Chat>(`/chats/${encodeURIComponent(doc.id)}`, { ...doc, updated_at: now() });
      void list.reload();
    } catch (err) {
      toastError(t("chat.list.couldnt_save"), err);
    } finally {
      s.inFlight = false;
      if (s.next) void flush();
    }
  }, []);
  const saveSoon = useCallback(
    (doc: Chat, delay = 0) => {
      if (!doc.id) return;
      const s = saving.current;
      s.next = doc;
      clearTimeout(s.timer);
      s.timer = setTimeout(() => void flush(), delay);
    },
    [flush],
  );

  // ---------- streaming ----------
  const [busy, setBusy] = useState(false);
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ total: number; processed: number; cache: number } | null>(null);
  /** Prompt tokens of the running request (kept after output starts, for the live numbers). */
  const [promptTotal, setPromptTotal] = useState<number | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [thinking, setThinking] = useState<{ active: boolean; ms: number | null } | null>(null);
  const [error, setError] = useState<ChatErrorState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);

  // composer
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const [editing, setEditing] = useState<ChatMessage | null>(null);
  const [drafts, setDrafts] = useState<Record<string, ToolDraft>>({});
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [showRequest, setShowRequest] = useState(false);

  useEffect(() => {
    if (!notice) return;
    const id = setTimeout(() => setNotice(null), 2000);
    return () => clearTimeout(id);
  }, [notice]);

  useTitle(chat?.id ? t("chat.doc_title", { title: chat.title }) : t("chat.page_title"));
  const thread = useMemo(() => pathTo(chat?.messages ?? [], chat?.active_leaf), [chat]);
  const visible = thread.filter((m) => m.role !== "tool");
  const results = useMemo(() => new Map(thread.filter((m) => m.role === "tool" && m.tool_call_id).map((m) => [m.tool_call_id!, m])), [thread]);
  const lastAssistant = [...thread].reverse().find((m) => m.role === "assistant");
  const lastNode = thread[thread.length - 1];
  const pendingCalls: ToolCall[] =
    !busy && lastAssistant && lastNode?.id === lastAssistant.id && metaOf(lastAssistant).finish_reason === "tool_calls"
      ? toolCallsOf(lastAssistant).filter((c) => !results.has(c.id))
      : [];
  const waitingFor = pendingCalls.filter((c) => !drafts[c.id]).length;

  async function wireMessages(doc: Chat, path: ChatMessage[]): Promise<Record<string, unknown>[]> {
    const out: Record<string, unknown>[] = doc.system?.trim() ? [{ role: "system", content: doc.system }] : [];
    for (const m of path) {
      const atts = m.attachments ?? [];
      if (atts.length === 0) {
        out.push({
          role: m.role,
          content: m.content,
          ...(m.tool_calls?.length ? { tool_calls: m.tool_calls } : {}),
          ...(m.tool_call_id ? { tool_call_id: m.tool_call_id } : {}),
        });
        continue;
      }
      const parts: Record<string, unknown>[] = typeof m.content === "string" ? (m.content ? [{ type: "text", text: m.content }] : []) : [...m.content];
      for (const a of atts) {
        let url = dataUrls.current.get(a.file);
        if (!url) {
          const res = await request<Response>(`/api/admin/chats/attachments/${encodeURIComponent(fileName(a.file))}`, { raw: true });
          url = await blobToDataUrl(await res.blob());
          dataUrls.current.set(a.file, url);
        }
        parts.push(a.kind === "image" ? { type: "image_url", image_url: { url } } : { type: "file", file: { filename: a.name ?? "document.pdf", file_data: url } });
      }
      out.push({ role: m.role, content: parts });
    }
    return out;
  }

  function requestBody(doc: Chat, messages: Record<string, unknown>[]): Record<string, unknown> {
    const body: Record<string, unknown> = {
      ...samplingBody(sampling, { allowIgnoreEos: !constrained }),
      model: requestModel(doc.model ?? "", doc.profile),
      messages,
      stream: true,
      stream_options: { include_usage: true },
      return_progress: true,
    };
    // Splash refuses stop with tools or structured output (server/frontend.py).
    if (constrained) delete body.stop;
    if (allTools.length) {
      body.tools = allTools;
      body.tool_choice = choice === "named" ? toolChoiceBody("named", named) : choice;
      if (!parallel) body.parallel_tool_calls = false;
    }
    const fmt = outputBody(output);
    if (fmt) body.response_format = fmt;
    return body;
  }

  /** Streams one assistant reply under `doc.active_leaf`. Returns false when it failed before any output. */
  async function generate(doc: Chat, retries = 0, waitForIdle = false): Promise<boolean> {
    // A new turn scrolls to the end and follows it until the user scrolls up.
    following.current = true;
    const ctrl = new AbortController();
    abort.current = ctrl;
    setBusy(true);
    setError(null);
    setWaiting(true);
    const started = Date.now();
    setStartedAt(started);
    setPromptTotal(null);
    const acc = new StreamAccumulator();
    const id = newId();
    const reply: ChatMessage = { id, parent: doc.active_leaf ?? null, role: "assistant", content: "", created_at: now(), meta: { model: doc.model, profile: doc.profile, response_format: outputBody(output) } };
    let next: Chat = { ...doc, messages: [...(doc.messages ?? []), reply], active_leaf: id };
    setChat(next);
    setStreamingId(doc.id || null);
    rememberLeaf(memory.current, next.messages ?? [], id);
    let body: Record<string, unknown> | null = null;
    let lastSave = Date.now();
    // Reduced motion: no ticking; the time appears when the block closes (07 §5.4).
    const still = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    const tick = setInterval(() => {
      const snap = acc.snapshot();
      if (snap.reasoningStartedAt !== null) setThinking({ active: snap.thinking, ms: still && snap.thinking ? null : snap.thinkingMs });
    }, 100);
    const apply = () => {
      const calls = ensureCallIds(acc.toolCalls, id.slice(0, 8));
      const updated: ChatMessage = {
        ...reply,
        content: acc.content,
        reasoning: acc.reasoning || null,
        tool_calls: calls.length ? (calls as unknown as Record<string, unknown>[]) : null,
        meta: {
          ...reply.meta,
          usage: acc.usage as Record<string, unknown> | null,
          timings: acc.timings as Record<string, unknown> | null,
          finish_reason: acc.finishReason,
          ttft_ms: acc.ttftMs,
          thinking_ms: acc.thinkingMs(),
          duration_ms: Date.now() - started,
          segments: acc.segments.map((s) => ({ ...s })),
        },
      };
      next = { ...next, messages: (next.messages ?? []).map((m) => (m.id === id ? updated : m)) };
      setChat(next);
      return updated;
    };
    try {
      const messages = await wireMessages(doc, pathTo(doc.messages ?? [], doc.active_leaf));
      body = requestBody(doc, messages);
      let requestId: string | null = null;
      for await (const evt of postStream("/v1/chat/completions", body, {
        signal: ctrl.signal,
        onHeaders: (h) => (requestId = h.get("x-splash-request-id")),
        ...(waitForIdle ? { headers: { "X-Splash-Switch": "wait" } } : {}),
      })) {
        acc.push(evt.data);
        setWaiting(false);
        setProgress(acc.outputStarted ? null : acc.progress);
        setPromptTotal(acc.progress?.total ?? null);
        apply();
        if (Date.now() - lastSave > 2000) {
          lastSave = Date.now();
          saveSoon(next);
        }
      }
      const done = apply();
      if (!done.meta?.finish_reason) {
        // Stop aborts the fetch, which can end the stream instead of throwing: that is "stopped", not a dropped connection.
        const stopped = ctrl.signal.aborted;
        done.meta = { ...done.meta, finish_reason: stopped ? "stopped" : "disconnected", est_out: estimateTokens(acc.content) };
        next = { ...next, messages: (next.messages ?? []).map((m) => (m.id === id ? done : m)) };
        setChat(next);
        if (!stopped) setError({ ...classifyError(new Error("disconnected")), kind: "generic", title: t("chat.error.disconnected"), body: null, retries: 0, retryIn: null });
      }
      saveSoon(next);
      if (requestId) void attachInjected(id, requestId);
      return true;
    } catch (err) {
      const aborted = ctrl.signal.aborted;
      if (aborted || acc.outputStarted) {
        const done = apply();
        done.meta = { ...done.meta, finish_reason: aborted ? "stopped" : "disconnected", est_out: estimateTokens(acc.content) };
        next = { ...next, messages: (next.messages ?? []).map((m) => (m.id === id ? done : m)) };
        setChat(next);
        saveSoon(next);
        if (!aborted) setError({ ...classifyError(err, { model: doc.model, active }), title: t("chat.error.disconnected"), retries: 0, retryIn: null, request: body });
        return true;
      }
      // Nothing streamed: drop the placeholder and report (§10).
      next = { ...doc };
      setChat(next);
      const ce = classifyError(err, { model: doc.model, active });
      const auto = autoRetry(ce.kind) && retries < MAX_AUTO_RETRIES;
      setError({ ...ce, request: body, retries, retryIn: auto ? (ce.retryAfter ?? (ce.kind === "queue_full" ? 1 : ce.kind === "busy" ? 10 : 5)) : null });
      return false;
    } finally {
      clearInterval(tick);
      setThinking(null);
      setBusy(false);
      setWaiting(false);
      setProgress(null);
      setPromptTotal(null);
      setStartedAt(null);
      setStreamingId(null);
      if (abort.current === ctrl) abort.current = null;
    }
  }

  /** The fields the proxy injected for this turn (G4: `x-splash-request-id` → its usage row). */
  async function attachInjected(messageId: string, requestId: string) {
    try {
      const rows = await api.get<{ rows: Array<{ injected?: Record<string, unknown> | null }> }>("/usage/requests", { request_id: requestId, limit: 1 });
      const injected = rows.rows[0]?.injected;
      if (!injected || !Object.keys(injected).length) return;
      setChat((cur) => {
        if (!cur) return cur;
        const next = { ...cur, messages: (cur.messages ?? []).map((m) => (m.id === messageId ? { ...m, meta: { ...m.meta, injected } } : m)) };
        saveSoon(next);
        return next;
      });
    } catch {
      /* the meta item is optional */
    }
  }

  // Retry-After countdowns (D-07-11).
  const retryRef = useRef<() => void>(() => undefined);
  useEffect(() => {
    if (!error || error.retryIn === null) return;
    if (error.retryIn <= 0) {
      retryRef.current();
      return;
    }
    const id = setTimeout(() => setError((prev) => (prev && prev.retryIn !== null ? { ...prev, retryIn: prev.retryIn - 1 } : prev)), 1000);
    return () => clearTimeout(id);
  }, [error?.retryIn]);

  const pendingSend = useRef<{ text: string; attachments: DraftAttachment[]; editing: ChatMessage | null } | null>(null);

  async function ensureSaved(doc: Chat): Promise<Chat> {
    if (doc.id) return doc;
    const created = await api.post<Chat>("/chats", { model: doc.model, profile: doc.profile, title: doc.title });
    const merged: Chat = { ...doc, id: created.id, created_at: created.created_at, updated_at: created.updated_at };
    chatRef.current = merged;
    createdId.current = created.id;
    setChat(merged);
    navigate(`/chat/${encodeURIComponent(created.id)}`, { replace: true });
    void list.reload();
    return merged;
  }

  async function send(retries = 0, waitForIdle = false) {
    const doc = chatRef.current;
    if (!doc || busy) return;
    const text = pendingSend.current?.text ?? draft;
    const atts = pendingSend.current?.attachments ?? attachments;
    const edit = pendingSend.current?.editing ?? editing;
    if (!text.trim() && atts.length === 0) return;
    pendingSend.current = { text, attachments: atts, editing: edit };
    setDraft("");
    setAttachments([]);
    setEditing(null);
    try {
      let saved = await ensureSaved(doc);
      const stored = [];
      for (const a of atts) {
        if (a.file) {
          stored.push({ kind: a.kind, file: a.file, name: a.name, bytes: a.bytes });
          continue;
        }
        const blob = await (await fetch(a.dataUrl)).blob();
        const up = await request<AttachmentUpload>(`/api/admin/chats/${encodeURIComponent(saved.id)}/attachments`, { method: "POST", body: blob, headers: { "Content-Type": a.mime } });
        dataUrls.current.set(up.file, a.dataUrl);
        a.file = up.file;
        stored.push({ kind: up.kind, file: up.file, name: a.name, bytes: up.bytes });
      }
      const message: ChatMessage = {
        id: newId(),
        parent: edit ? (edit.parent ?? null) : (saved.active_leaf ?? null),
        role: "user",
        content: text,
        created_at: now(),
        attachments: stored,
        meta: { attachment_pages: atts.map((a) => a.pages) },
      };
      const title = saved.title === t("chat.default_title") && !(saved.messages ?? []).some((m) => m.role === "user") ? autoTitle(text) || saved.title : saved.title;
      saved = { ...saved, title, messages: [...(saved.messages ?? []), message], active_leaf: message.id };
      setChat(saved);
      saveSoon(saved);
      const ok = await generate(saved, retries, waitForIdle);
      if (ok) pendingSend.current = null;
      else {
        // Keep the text in the composer; the message never reached the model (§10).
        const reverted = { ...saved, messages: (saved.messages ?? []).filter((m) => m.id !== message.id), active_leaf: message.parent ?? null, title: doc.title };
        setChat(reverted);
        saveSoon(reverted);
        setDraft(text);
        setAttachments(atts);
        if (edit) setEditing(edit);
      }
    } catch (err) {
      setDraft(text);
      setAttachments(atts);
      setError({ ...classifyError(err, { model: doc.model, active }), retries: 0, retryIn: null });
      pendingSend.current = null;
    }
  }

  async function regenerate(m: ChatMessage) {
    const doc = chatRef.current;
    if (!doc || busy) return;
    await generate({ ...doc, active_leaf: m.parent ?? null });
  }

  async function continueWithTools() {
    const doc = chatRef.current;
    if (!doc || !lastAssistant) return;
    let parent = lastAssistant.id;
    const added: ChatMessage[] = [];
    for (const c of toolCallsOf(lastAssistant)) {
      if (results.has(c.id)) continue;
      const d = drafts[c.id];
      if (!d) return;
      const msg: ChatMessage = {
        id: newId(),
        parent,
        role: "tool",
        content: d.text,
        tool_call_id: c.id,
        created_at: now(),
        meta: { tool: { source: d.source, server: d.server ?? null, duration_ms: d.duration_ms ?? null, is_error: d.is_error ?? false, auto: d.auto ?? false, name: c.function.name } },
      };
      added.push(msg);
      parent = msg.id;
    }
    const next = { ...doc, messages: [...(doc.messages ?? []), ...added], active_leaf: parent };
    setChat(next);
    setDrafts({});
    saveSoon(next);
    await generate(next);
  }

  /** `always` also saves "always allow" for the server; `auto` marks a call that ran without a click. */
  async function runMcp(call: ToolCall, server: string, always: boolean, auto = always) {
    setRunning((s) => new Set(s).add(call.id));
    try {
      if (always) {
        try {
          const cur = mcpServers.data?.servers ?? {};
          const entry = cur[server];
          if (entry) {
            await api.put("/mcp/servers", { servers: { ...cur, [server]: { ...entry, always_allow: true } } });
            void mcpServers.reload();
          }
        } catch (err) {
          toastError(t("chat.tool.allow_failed", { server }), err);
        }
      }
      let args: unknown = {};
      try {
        args = JSON.parse(call.function.arguments || "{}");
      } catch {
        args = {};
      }
      const started = Date.now();
      const res = await api.post<McpCallResult>("/mcp/call", { server, tool: call.function.name, arguments: args, confirmed: true });
      const text = res.content.map((c) => (typeof (c as { text?: unknown }).text === "string" ? (c as { text: string }).text : JSON.stringify(c))).join("\n");
      setDrafts((d) => ({ ...d, [call.id]: { text, source: "mcp", server, duration_ms: res.duration_ms ?? Date.now() - started, is_error: res.is_error, auto } }));
    } catch (err) {
      toastError(t("chat.tool.mcp_failed"), err);
    } finally {
      setRunning((s) => {
        const n = new Set(s);
        n.delete(call.id);
        return n;
      });
    }
  }

  // MCP: auto-run calls to always-allowed servers; continue when every call has a result.
  useEffect(() => {
    if (mode !== "mcp" || pendingCalls.length === 0) return;
    for (const c of pendingCalls) {
      const srv = enabledMcp.find((x) => x.name === c.function.name);
      if (srv && srv.always_allow && !drafts[c.id] && !running.has(c.id)) void runMcp(c, srv.server, false, true);
    }
    if (pendingCalls.every((c) => drafts[c.id]) && pendingCalls.some((c) => drafts[c.id]?.source === "mcp")) void continueWithTools();
  }, [drafts, pendingCalls.length, mode]);

  function stop() {
    abort.current?.abort();
  }

  function startEdit(m: ChatMessage) {
    if (m.role === "system") {
      setTab("system");
      return;
    }
    setEditing(m);
    setDraft(textOf(m));
    setAttachments([]);
  }

  function deleteBranch(m: ChatMessage) {
    const doc = chatRef.current;
    if (!doc) return;
    const out = removeBranch(doc.messages ?? [], doc.active_leaf, m.id, memory.current);
    const next = { ...doc, messages: out.messages, active_leaf: out.activeLeaf };
    setChat(next);
    saveSoon(next);
  }

  function moveBranch(m: ChatMessage, dir: -1 | 1) {
    const doc = chatRef.current;
    if (!doc) return;
    rememberLeaf(memory.current, doc.messages ?? [], doc.active_leaf);
    const leaf = switchBranch(doc.messages ?? [], m, dir, memory.current);
    if (!leaf) return;
    const next = { ...doc, active_leaf: leaf };
    setChat(next);
    saveSoon(next);
  }

  function insertSystem() {
    const doc = chatRef.current;
    if (!doc) return;
    const msg: ChatMessage = { id: newId(), parent: doc.active_leaf ?? null, role: "system", content: "", created_at: now() };
    const next = { ...doc, messages: [...(doc.messages ?? []), msg], active_leaf: msg.id };
    setChat(next);
    saveSoon(next);
  }

  function pick(m: string, p: string) {
    const doc = chatRef.current;
    if (!doc) return;
    const next = { ...doc, model: m, profile: p };
    setChat(next);
    saveSoon(next);
  }

  // ---------- list actions ----------
  const [deleting, setDeleting] = useState<ChatSummary | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  async function rename(id: string, title: string) {
    try {
      const doc = id === chatRef.current?.id ? chatRef.current : await api.get<Chat>(`/chats/${encodeURIComponent(id)}`);
      if (!doc) return;
      const next = { ...doc, title };
      if (id === chatRef.current?.id) setChat(next);
      await api.put(`/chats/${encodeURIComponent(id)}`, next);
      void list.reload();
    } catch (err) {
      toastError(t("chat.rename.failed"), err);
    }
  }
  async function confirmDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      if (deleting.id === chatRef.current?.id) abort.current?.abort();
      await api.del(`/chats/${encodeURIComponent(deleting.id)}`);
      toast(t("chat.delete.done", { title: deleting.title }));
      const wasOpen = deleting.id === chatRef.current?.id;
      setDeleting(null);
      await list.reload();
      if (wasOpen) {
        const rest = (list.data?.chats ?? []).filter((c) => c.id !== deleting.id);
        navigate(rest[0] ? `/chat/${encodeURIComponent(rest[0].id)}` : "/chat");
        if (!rest[0]) setChat(blankChat(active));
      }
    } catch (err) {
      toastError(t("chat.delete.failed"), err);
    } finally {
      setDeleteBusy(false);
    }
  }

  function newChat() {
    abort.current?.abort();
    setChat(blankChat(active ?? rows[0]?.id ?? null));
    setDraft("");
    setAttachments([]);
    setEditing(null);
    setDrafts({});
    setError(null);
    navigate("/chat");
    setListSheet(false);
  }

  // ---------- keyboard (§12) ----------
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape" && abort.current) {
        stop();
        return;
      }
      const el = ev.target as HTMLElement | null;
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return;
      const k = ev.key;
      if (k === "n") newChat();
      else if (k === "p") togglePanel();
      else if (k === "l") toggleList();
      else if (k === "m") document.querySelector<HTMLButtonElement>('[data-testid="chat-model"]')?.click();
      else if (["1", "2", "3", "4"].includes(k)) openTab(PANEL_TABS[Number(k) - 1]!); else if (k === "e") {
        const lastUser = [...visible].reverse().find((m) => m.role === "user");
        if (lastUser) startEdit(lastUser);
      } else if (k === "r") {
        if (lastAssistant) void regenerate(lastAssistant);
      } else if (k === "[" || k === "]") {
        const el = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>(".chat-message");
        const id = el?.dataset.message ?? lastAssistant?.id;
        const m = visible.find((x) => x.id === id);
        if (m) moveBranch(m, k === "[" ? -1 : 1);
      } else if (k === "j" || k === "k") {
        const items = Array.from(document.querySelectorAll<HTMLElement>(".chat-message"));
        const i = items.indexOf(document.activeElement as HTMLElement);
        items[Math.min(items.length - 1, Math.max(0, i + (k === "j" ? 1 : -1)))]?.focus();
      } else return;
      ev.preventDefault();
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  });

  function toggleList() {
    if (width === "narrow") setListSheet((v) => !v);
    else {
      const next = !listOpen;
      setListOpen(next);
      writeLocal(LIST_KEY, next);
    }
  }

  function openTab(next: PanelTab) {
    setTab(next);
    if (width === "wide") setPanelOpen(true);
    else setPanelSheet(true);
  }

  function togglePanel() {
    if (width === "wide") {
      const next = !panelOpen;
      setPanelOpen(next);
      writeLocal(PANEL_KEY, next);
    } else setPanelSheet((v) => !v);
  }

  // ---------- render ----------
  const noModels = !models.loading && rows.length === 0 && !active;
  const blocked = !model
    ? t("chat.composer.disabled_no_model")
    : hasSamplingErrors(sErrors)
      ? t("chat.composer.fix_panel")
      : toolCheck.error || cErr
        ? t("chat.composer.fix_tools")
        : oErr
          ? t("chat.composer.fix_output")
          : attachmentError(attachments)
            ? t("chat.composer.fix_attachments")
            : !autoLoad && model !== active
              ? t("chat.composer.disabled_load")
              : null;
  retryRef.current = () => {
    if (!error) return;
    const n = error.retries + 1;
    setError(null);
    if (pendingSend.current) void send(n);
    else if (chatRef.current) void generate(chatRef.current, n);
  };

  const [loadingModel, setLoadingModel] = useState(false);
  const loadNow = async () => {
    if (!model) return;
    setLoadingModel(true);
    try {
      await withInstallConfirm((force) => api.post("/engine/load", { model, force }), model);
    } catch (err) {
      if (!isCancelled(err)) toastError(t("chat.model.loading_failed"), err);
    } finally {
      setLoadingModel(false);
    }
  };

  const toolCtx: ToolContext = {
    results,
    drafts,
    mode,
    mcpServer: (name) => {
      const x = enabledMcp.find((tl) => tl.name === name);
      return x ? { server: x.server, alwaysAllow: x.always_allow } : null;
    },
    defined: new Set([...toolCheck.names, ...enabledMcp.map((x) => x.name)]),
    pending: pendingCalls.length > 0,
    onDraft: (callId, d) =>
      setDrafts((prev) => {
        const n = { ...prev };
        if (d) n[callId] = d;
        else delete n[callId];
        return n;
      }),
    onRunMcp: (call, server, always) => void runMcp(call, server, always),
    running,
  };

  const panelFor = (headActions: ComponentChildren, footer: ComponentChildren) =>
    chat && (
    <SidePanel
      headActions={headActions}
      footer={footer}
      tab={tab}
      onTab={setTab}
      sampling={sampling}
      onSampling={setSampling}
      errors={sErrors}
      profile={profile}
      profiles={profileNames}
      onProfile={(p) => model && pick(model, p)}
      profileOverlay={overlay}
      modelDefaults={modelDefaults}
      model={model}
      constrained={constrained}
      onReset={() => {
        setUndo({ form: sampling, profile });
        setSampling(emptySampling());
        if (model) pick(model, "default");
        setTimeout(() => setUndo(null), 10_000);
      }}
      undo={undo ? () => (setSampling(undo.form), model && pick(model, undo.profile), setUndo(null)) : null}
      system={chat.system ?? ""}
      onSystem={(s) => {
        const next = { ...chat, system: s };
        setChat(next);
        saveSoon(next, 800);
      }}
      laterSystem={laterSystem}
      onInsertSystem={insertSystem}
      toolsText={toolsText}
      onToolsText={setToolsText}
      toolsError={toolCheck.error}
      toolNames={toolCheck.names}
      choice={choice}
      onChoice={setChoice}
      named={named || toolCheck.names[0] || ""}
      onNamed={setNamed}
      choiceError={cErr}
      parallel={parallel}
      onParallel={setParallel}
      mode={mode}
      onMode={setMode}
      servers={Object.entries(mcpServers.data?.servers ?? {}).map(([name, s]) => ({
        name,
        tools: (mcpTools.data?.tools ?? []).filter((x) => x.server === name).length,
        enabled: s.enabled,
        alwaysAllow: s.always_allow,
        on: serverOn[name] !== false && s.enabled,
        error: (mcpTools.data?.errors ?? []).find((x) => x.server === name)?.message ?? null,
      }))}
      onServerOn={(name, on) => setServerOn({ ...serverOn, [name]: on })}
      onAlwaysAllow={(name, on) => {
        const cur = mcpServers.data?.servers ?? {};
        const entry = cur[name];
        if (!entry) return;
        void api
          .put("/mcp/servers", { servers: { ...cur, [name]: { ...entry, always_allow: on } } })
          .then(() => mcpServers.reload())
          .catch((err) => toastError(t("chat.tool.allow_failed", { server: name }), err));
      }}
      collisions={collisions}
      toolsPending={pendingCalls.length > 0}
      output={output}
      onOutput={setOutput}
      outputError={oErr}
    />
  );

  const listEl = (
    <ConversationList
      chats={list.data?.chats ?? null}
      error={list.error}
      onRetry={list.reload}
      query={query}
      onQuery={setQuery}
      activeId={chat?.id || null}
      streamingId={streamingId}
      onNew={newChat}
      onRename={(id, title) => void rename(id, title)}
      onDelete={setDeleting}
      onOpen={() => setListSheet(false)}
    />
  );

  const errorBanner = error && (
    <ErrorBanner
      error={error}
      onRetry={() => retryRef.current()}
      onSwitchWhenIdle={() => {
        setError(null);
        if (pendingSend.current) void send(0, true);
        else if (chatRef.current) void generate(chatRef.current, 0, true);
      }}
      onOpenTab={openTab}
      onTurnOffEos={() => setSampling({ ...sampling, ignore_eos: false })}
      onRemoveAttachments={() => setAttachments([])}
      onShowRequest={() => setShowRequest(true)}
      onDismiss={() => setError(null)}
    />
  );

  const sideOpen = width !== "narrow" && listOpen;
  const panelShown = width === "wide" && panelOpen;
  const panelExpanded = width === "wide" ? panelOpen : panelSheet;
  const listExpanded = width === "narrow" ? listSheet : listOpen;
  const isActiveModel = !!model && model === active;
  const live = isActiveModel && (e?.state === "ready" || e?.state === "busy");
  const version = e?.engine_version ? t("nav.engine", { version: e.engine_version }) : null;

  /** A column toggle unmounts the button that was clicked: hand focus to its counterpart. */
  const toggleFocus = (toggle: () => void, target: string) => () => {
    toggle();
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-chat-toggle="${target}"]`)?.focus());
  };

  const topBar = (
    <div class="chat-top" data-testid="chat-top">
      <div class="chat-top-start cluster">
        {!sideOpen && (
          <>
            <HomeLink href={home} />
            <Button size="s" variant="text" aria-expanded={listExpanded} aria-label={t("chat.side.show_label")} data-chat-toggle="list-show" onClick={width === "narrow" ? toggleList : toggleFocus(toggleList, "list-hide")}>
              {t("chat.list_toggle")} {listExpanded ? "▾" : "▸"}
            </Button>
          </>
        )}
      </div>
      <div class="chat-top-center">
        <ModelSelector
          rows={rows}
          profilesFor={(id) => (id === model ? profileNames : rows.find((r) => r.id === id)?.profiles.length ? ["default", ...rows.find((r) => r.id === id)!.profiles] : ["default"])}
          model={model}
          profile={profile}
          onPick={pick}
          active={active}
          engineState={e?.state ?? null}
          autoLoad={autoLoad}
          onLoadNow={() => void loadNow()}
          loading={loadingModel}
        />
      </div>
      <div class="chat-top-end cluster">
        {!panelShown && (
          <Button size="s" variant="text" aria-expanded={panelExpanded} data-chat-toggle="panel-show" onClick={width === "wide" ? toggleFocus(togglePanel, "panel-hide") : togglePanel}>
            {t("chat.panel_toggle")} {panelExpanded ? "▾" : "▸"}
          </Button>
        )}
      </div>
    </div>
  );

  const statusLine = (
    <div class="chat-statusline">
      <span class="meta chat-statusline-model">
        <span class="chat-dot" data-live={live ? "true" : "false"} aria-hidden="true">
          ●
        </span>
        <span class="visually-hidden">{t("chat.status.model", { state: isActiveModel ? stateDisplay(e?.state ?? null).label : t("chat.model.not_loaded") })} · </span>
        <span class="mono">{model ? shortModel(model) : t("chat.status.unselected")}</span>
      </span>
      <ContextMeter thread={thread} busy={busy} promptTotal={promptTotal ?? progress?.total ?? null} context={row?.context ?? null} contextEstimated={!!row?.estimated} />
    </div>
  );

  const threadEl = (
    <section class="chat-main" aria-label={t("chat.thread_label")}>
      <h1 class="visually-hidden">{chat?.title || t("chat.page_title")}</h1>
      {topBar}
      {!!models.error && (
        <div class="chat-band">
          <LoadError thing={t("chat.model.load_models_failed")} error={models.error} onRetry={models.reload} />
        </div>
      )}
      {notFound ? (
        <div class="chat-messages">
          <Empty title={t("chat.not_found")} action={<Button onClick={newChat}>{t("chat.list.new")}</Button>} />
        </div>
      ) : loadError ? (
        <div class="chat-messages">
          <LoadError thing={t("chat.load_chat")} error={loadError} />
        </div>
      ) : (
        <>
          {noModels && (
            <div class="chat-band">
              <Banner
                tone="info"
                actions={
                  <Link href="/models/downloader" class="btn" data-size="s">
                    {t("chat.empty.open_downloader")}
                  </Link>
                }
              >
                {t("chat.empty.no_models")}
              </Banner>
            </div>
          )}
          <div class="chat-messages" ref={messagesEl} data-testid="chat-messages">
            {visible.length === 0 ? (
              <Empty size="l" title={t("chat.empty.statement")}>
                {t("chat.empty.line")}
              </Empty>
            ) : (
              visible.map((m, i) => {
                const isStreaming = busy && i === visible.length - 1 && m.role === "assistant";
                return (
                  <MessageView
                    key={m.id}
                    message={m}
                    streaming={isStreaming}
                    thinkingLive={isStreaming ? thinking : null}
                    branch={branchInfo(chat?.messages ?? [], m)}
                    replies={replyCount(chat?.messages ?? [], m.id)}
                    busy={busy}
                    tools={{ ...toolCtx, pending: toolCtx.pending && m.id === lastAssistant?.id }}
                    onRegenerate={m.role === "assistant" ? () => void regenerate(m) : undefined}
                    onEdit={m.role === "user" || m.role === "system" ? () => startEdit(m) : undefined}
                    onDelete={() => deleteBranch(m)}
                    onBranch={(dir) => moveBranch(m, dir)}
                  />
                );
              })
            )}
          </div>
          <div class="chat-bottom">
            {(waiting || progress) && (
              <div class="chat-progress stack" role="status">
                {progress ? (
                  <>
                    <span class="meta tnum">
                      {progress.cache
                        ? t("chat.progress.reading_cached", { p: formatCount(progress.processed), t: formatCount(progress.total), c: formatCount(progress.cache) })
                        : t("chat.progress.reading", { p: formatCount(progress.processed), t: formatCount(progress.total) })}
                    </span>
                    <ProgressBar
                      live
                      label={t("chat.progress.label")}
                      value={progress.total ? progress.processed / progress.total : 0}
                      valueText={t("chat.progress.reading", { p: formatCount(progress.processed), t: formatCount(progress.total) })}
                    />
                  </>
                ) : (
                  <span class="meta loading-dots">
                    {e?.state?.startsWith("starting") ? t("chat.progress.loading") : (e?.queued ?? 0) > 0 ? t("chat.progress.queued") : t("chat.progress.waiting")}
                  </span>
                )}
              </div>
            )}
            {errorBanner}
            {notice && <p class="meta" role="status">{notice}</p>}
            {pendingCalls.length > 0 ? (
              <div class="cluster chat-waiting">
                <span class="label">{t("chat.tool.waiting", { n: waitingFor || pendingCalls.length })}</span>
                <Button variant={waitingFor === 0 ? "accent" : "outline"} disabled={waitingFor > 0} onClick={() => void continueWithTools()}>
                  {t("chat.tool.send_all")}
                </Button>
              </div>
            ) : (
              <Composer
                value={draft}
                onChange={setDraft}
                attachments={attachments}
                onAttachments={setAttachments}
                placeholder={model ? t("chat.composer.placeholder", { name: shortModel(model) }) : t("chat.composer.placeholder_plain")}
                streaming={busy}
                blocked={blocked}
                vision={row?.vision ?? null}
                editing={!!editing}
                disabled={noModels}
                onSend={() => void send()}
                onStop={stop}
                onCancelEdit={() => (setEditing(null), setDraft(""))}
                onEditLast={() => {
                  const lastUser = [...visible].reverse().find((m) => m.role === "user");
                  if (lastUser) startEdit(lastUser);
                }}
                onNotice={setNotice}
              />
            )}
            {statusLine}
          </div>
        </>
      )}
    </section>
  );

  const tiles = <StatTiles thread={thread} busy={busy} live={liveSample.value} promptTotal={promptTotal ?? progress?.total ?? null} startedAt={startedAt} />;

  return (
    <>
      <div class="chat-app" ref={layout} data-width={width} data-side={sideOpen ? "open" : "closed"} data-panel={panelShown ? "open" : "closed"}>
        {sideOpen && (
          <Sidebar home={home} version={version} onHide={toggleFocus(toggleList, "list-show")}>
            {listEl}
          </Sidebar>
        )}
        {threadEl}
        {panelShown && chat && (
          <aside class="chat-options" aria-label={t("chat.panel.label")}>
            {panelFor(
              <Button size="s" variant="text" class="chat-panel-hide" aria-label={t("chat.panel.hide_label")} aria-expanded="true" data-chat-toggle="panel-hide" onClick={toggleFocus(togglePanel, "panel-show")}>
                {t("chat.panel.hide")}
              </Button>,
              tiles,
            )}
          </aside>
        )}
      </div>
      <Sheet open={width === "narrow" && listSheet} title={t("chat.list_sheet_title")} onClose={() => setListSheet(false)}>
        <div class="chat-sheet-list">
          {listEl}
          <AdminLinks />
        </div>
      </Sheet>
      <Sheet open={width !== "wide" && panelSheet && !!chat} title={t("chat.panel_sheet_title")} onClose={() => setPanelSheet(false)}>
        <div class="chat-sheet-panel">{panelFor(null, tiles)}</div>
      </Sheet>
      <Sheet open={showRequest} title={t("chat.request_sheet")} onClose={() => setShowRequest(false)}>
        <CodeBlock code={JSON.stringify(error?.request ?? {}, null, 2)} label="JSON" />
      </Sheet>
      <ConfirmSheet
        open={!!deleting}
        title={t("chat.delete.title")}
        confirmLabel={t("chat.delete.confirm_plain")}
        busy={deleteBusy}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
      >
        <p class="body">
          {deleting
            ? t("chat.delete.summary", {
                title: deleting.title,
                messages: t("chat.delete.messages", { n: deleting.message_count }),
                attachments: t("chat.delete.no_attachments"),
              })
            : ""}
        </p>
        {deleting && deleting.id === streamingId && <p class="meta">{t("chat.delete.streaming")}</p>}
      </ConfirmSheet>
    </>
  );
}
