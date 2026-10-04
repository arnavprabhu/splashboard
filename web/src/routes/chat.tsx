import { useEffect, useRef, useState } from "preact/hooks";
import { api, request } from "../api/client";
import { postStream } from "../api/stream";
import type {
  AttachmentUpload,
  Chat,
  ChatList,
  ChatMessage,
  McpToolList,
  McpCallResult,
} from "../api/models";
import {
  Button,
  ConfirmSheet,
  CopyButton,
  LoadError,
  PageHeader,
  ProgressBar,
} from "../components";
import { useApi } from "../lib/use-api";
import { engine } from "../store";
import { branchInfo, newId, pathTo, switchBranch } from "./chat/tree";
import { StreamAccumulator, ensureCallIds } from "./chat/accumulator";
import { Markdown } from "./chat/Markdown";
import {
  metaOf,
  textOf,
  toolCallsOf,
  type ModelEntry,
  type ToolCall,
} from "./chat/types";

const now = () => new Date().toISOString();
async function dataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

export default function ChatPage() {
  const [search, setSearch] = useState("");
  const list = useApi(
    (s) => api.get<ChatList>("/chats", { q: search }, s),
    [search],
  );
  const models = useApi((s) =>
    request<{ data: ModelEntry[] }>("/v1/models", { signal: s }),
  );
  const [chat, setChat] = useState<Chat | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState<ChatMessage | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState<{
    total: number;
    processed: number;
    cache: number;
  } | null>(null);
  const [toolsText, setToolsText] = useState("[]");
  const [samplingText, setSamplingText] = useState("{}");
  const [formatText, setFormatText] = useState("null");
  const [toolResults, setToolResults] = useState<Record<string, string>>({});
  const [mcp, setMcp] = useState<McpToolList | null>(null);
  const [toolApproval, setToolApproval] = useState<ToolCall | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  async function select(next: Chat) {
    setChat(next);
    setError(null);
    setEditing(null);
    setFiles([]);
    setDraft("");
    setToolsText(JSON.stringify(next.tools ?? [], null, 2));
    setSamplingText(JSON.stringify(next.sampling ?? {}, null, 2));
    setFormatText(JSON.stringify(next.response_format ?? null, null, 2));
  }
  async function fresh() {
    try {
      await select(
        await api.post<Chat>("/chats", { model: engine.value?.model }),
      );
      await list.reload();
    } catch (e) {
      setError(e);
    }
  }
  async function save(next: Chat) {
    setChat(next);
    const result = await api.put<Chat>(`/chats/${next.id}`, next);
    setChat(result);
    await list.reload();
    return result;
  }
  function configure(): Chat {
    if (!chat) throw new Error("Create or select a conversation.");
    const tools: unknown = JSON.parse(toolsText);
    const sampling: unknown = JSON.parse(samplingText);
    const format: unknown = JSON.parse(formatText);
    if (
      !Array.isArray(tools) ||
      !sampling ||
      typeof sampling !== "object" ||
      Array.isArray(sampling) ||
      (format !== null && (typeof format !== "object" || Array.isArray(format)))
    )
      throw new Error(
        "Tools must be an array; sampling and output format must be objects.",
      );
    return { ...chat, tools, sampling, response_format: format } as Chat;
  }
  async function wireMessages(current: Chat) {
    const messages: Record<string, unknown>[] = current.system
      ? [{ role: "system", content: current.system }]
      : [];
    for (const m of pathTo(current.messages ?? [], current.active_leaf)) {
      const parts: Record<string, unknown>[] =
        typeof m.content === "string"
          ? [{ type: "text", text: m.content }]
          : [...m.content];
      for (const attachment of m.attachments ?? []) {
        const blob = await request<Response>(
          `/api/admin/chats/${current.id}/${attachment.file}`,
          { raw: true },
        );
        const url = await dataUrl(await blob.blob());
        parts.push(
          attachment.kind === "image"
            ? { type: "image_url", image_url: { url } }
            : {
                type: "file",
                file: {
                  filename: attachment.name ?? "document.pdf",
                  file_data: url,
                },
              },
        );
      }
      messages.push({
        role: m.role,
        content: (m.attachments?.length ?? 0) ? parts : m.content,
        ...(m.tool_calls?.length ? { tool_calls: m.tool_calls } : {}),
        ...(m.tool_call_id ? { tool_call_id: m.tool_call_id } : {}),
      });
    }
    return messages;
  }
  async function generate(current: Chat) {
    const ctrl = new AbortController();
    abort.current = ctrl;
    setBusy(true);
    setError(null);
    const acc = new StreamAccumulator();
    const id = newId();
    const reply: ChatMessage = {
      id,
      parent: current.active_leaf,
      role: "assistant",
      content: "",
      created_at: now(),
    };
    let next = {
      ...current,
      messages: [...(current.messages ?? []), reply],
      active_leaf: id,
    };
    try {
      const messages = await wireMessages(current);
      const model =
        current.profile && current.profile !== "default"
          ? `${current.model}:${current.profile}`
          : current.model;
      for await (const event of postStream(
        "/v1/chat/completions",
        {
          ...current.sampling,
          model,
          messages,
          ...(current.tools?.length
            ? {
                tools: current.tools,
                tool_choice: current.tool_choice ?? "auto",
              }
            : {}),
          ...(current.response_format
            ? { response_format: current.response_format }
            : {}),
          stream: true,
          stream_options: { include_usage: true },
          return_progress: true,
        },
        { signal: ctrl.signal },
      )) {
        acc.push(event.data);
        const calls = ensureCallIds(acc.toolCalls, id);
        Object.assign(reply, {
          content: acc.content,
          reasoning: acc.reasoning,
          tool_calls: calls,
          meta: {
            usage: acc.usage,
            timings: acc.timings,
            finish_reason: acc.finishReason,
            ttft_ms:
              acc.firstOutputAt === null
                ? null
                : acc.firstOutputAt - acc.startedAt,
            segments: acc.segments,
            profile: current.profile,
          },
        });
        next = {
          ...next,
          messages: next.messages.map((m) => (m.id === id ? { ...reply } : m)),
        };
        setChat(next);
        setProgress(acc.firstOutputAt ? null : acc.progress);
      }
    } catch (e) {
      reply.meta = {
        ...reply.meta,
        finish_reason: ctrl.signal.aborted ? "stopped" : "error",
      };
      if (!ctrl.signal.aborted) setError(e);
    } finally {
      next = {
        ...next,
        messages: next.messages.map((m) => (m.id === id ? { ...reply } : m)),
      };
      try {
        await save(next);
      } catch (e) {
        setError(e);
      }
      setBusy(false);
      setProgress(null);
      abort.current = null;
    }
  }
  async function send() {
    if (!draft.trim() && !files.length) return;
    try {
      let current = configure();
      const attachments = [];
      for (const file of files) {
        const uploaded = await request<AttachmentUpload>(
          `/api/admin/chats/${current.id}/attachments`,
          {
            method: "POST",
            body: file,
            headers: { "Content-Type": file.type },
          },
        );
        attachments.push({
          kind: uploaded.kind,
          file: uploaded.file,
          name: file.name,
          bytes: file.size,
        });
      }
      const message: ChatMessage = {
        id: newId(),
        parent: editing ? editing.parent : current.active_leaf,
        role: "user",
        content: draft,
        created_at: now(),
        attachments,
      };
      current = {
        ...current,
        title: current.messages?.length
          ? current.title
          : draft.trim().slice(0, 80) || "Attachment",
        messages: [...(current.messages ?? []), message],
        active_leaf: message.id,
      };
      setBusy(true);
      current = await save(current);
      setDraft("");
      setFiles([]);
      setEditing(null);
      await generate(current);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }
  function attach(incoming: File[]) {
    const next = [...files, ...incoming];
    if (
      next.some(
        (f) => !f.type.startsWith("image/") && f.type !== "application/pdf",
      )
    ) {
      setError(new Error("Attach images or PDFs."));
      return;
    }
    if (next.reduce((n, f) => n + f.size, 0) > 64 * 1024 ** 2) {
      setError(new Error("Attachments are limited to 64 MiB per turn."));
      return;
    }
    setFiles(next);
  }
  async function toolResult(call: ToolCall, value: string) {
    if (!chat) return;
    try {
      const message: ChatMessage = {
        id: newId(),
        parent: chat.active_leaf,
        role: "tool",
        content: value,
        tool_call_id: call.id,
        created_at: now(),
      };
      await save({
        ...chat,
        messages: [...(chat.messages ?? []), message],
        active_leaf: message.id,
      });
    } catch (e) {
      setError(e);
    }
  }
  async function runMcp(call: ToolCall) {
    const tool = mcp?.tools.find(
      (t) => `${t.server}__${t.name}` === call.function.name,
    );
    if (!tool) return;
    setBusy(true);
    try {
      const result = await api.post<McpCallResult>("/mcp/call", {
        server: tool.server,
        tool: tool.name,
        arguments: JSON.parse(call.function.arguments),
        confirmed: true,
      });
      await toolResult(call, JSON.stringify(result));
      setToolApproval(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const thread = pathTo(chat?.messages ?? [], chat?.active_leaf);
  const answered = new Set(
    thread.filter((m) => m.role === "tool").map((m) => m.tool_call_id),
  );
  const pending = thread
    .flatMap((m) => toolCallsOf(m))
    .filter((c) => !answered.has(c.id));
  const selectedModel = models.data?.data.find((m) => m.id === chat?.model);
  return (
    <>
      <PageHeader title="Chat." />
      <div class="chat-layout">
        <aside class="chat-sidebar stack">
          <Button disabled={busy} onClick={() => void fresh()}>
            New conversation
          </Button>
          <label>
            Search conversations
            <input
              value={search}
              onInput={(e) => setSearch(e.currentTarget.value)}
            />
          </label>
          {!!list.error && (
            <LoadError
              thing="conversations"
              error={list.error}
              onRetry={list.reload}
            />
          )}
          <nav aria-label="Conversations">
            {list.data?.chats.map((c) => (
              <button
                class="chat-list-item"
                aria-current={c.id === chat?.id ? "page" : undefined}
                disabled={busy}
                key={c.id}
                onClick={() =>
                  void api
                    .get<Chat>(`/chats/${c.id}`)
                    .then(select)
                    .catch(setError)
                }
              >
                <strong>{c.title}</strong>
                <small>
                  {new Date(c.updated_at).toLocaleDateString()} · {c.snippet}
                </small>
              </button>
            ))}
          </nav>
        </aside>
        <section class="chat-thread stack" aria-label="Conversation">
          {!chat ? (
            <p>Create or select a conversation.</p>
          ) : (
            <>
              <label>
                Conversation title
                <input
                  disabled={busy}
                  value={chat.title}
                  onInput={(e) =>
                    setChat({ ...chat, title: e.currentTarget.value })
                  }
                  onBlur={() => void save(chat).catch(setError)}
                />
              </label>
              <div class="cluster">
                <label>
                  Model
                  <select
                    disabled={busy}
                    value={chat.model ?? ""}
                    onChange={(e) =>
                      setChat({ ...chat, model: e.currentTarget.value })
                    }
                  >
                    <option value="">Choose a model</option>
                    {models.data?.data.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </select>
                </label>
                <a href={`/api/admin/chats/${chat.id}/export?format=json`}>
                  Export JSON
                </a>
                <a href={`/api/admin/chats/${chat.id}/export?format=md`}>
                  Export Markdown
                </a>
                <Button disabled={busy} onClick={() => setDeleting(true)}>
                  Delete
                </Button>
              </div>
              {engine.value?.model && chat.model !== engine.value.model && (
                <p class="meta">
                  Will switch from {engine.value.model} when sent.
                </p>
              )}
              {thread.map((m) => {
                const branch = branchInfo(chat.messages ?? [], m);
                const meta = metaOf(m);
                return (
                  <article class="chat-message" key={m.id}>
                    <div class="cluster">
                      <span class="label">{m.role}</span>
                      <CopyButton text={textOf(m)} />
                      {branch.count > 1 && (
                        <>
                          <Button
                            disabled={busy || branch.index === 1}
                            aria-label="Previous branch"
                            onClick={() =>
                              void save({
                                ...chat,
                                active_leaf: switchBranch(
                                  chat.messages ?? [],
                                  m,
                                  -1,
                                ),
                              }).catch(setError)
                            }
                          >
                            ‹
                          </Button>
                          <span class="meta">
                            {branch.index} / {branch.count}
                          </span>
                          <Button
                            disabled={busy || branch.index === branch.count}
                            aria-label="Next branch"
                            onClick={() =>
                              void save({
                                ...chat,
                                active_leaf: switchBranch(
                                  chat.messages ?? [],
                                  m,
                                  1,
                                ),
                              }).catch(setError)
                            }
                          >
                            ›
                          </Button>
                        </>
                      )}
                    </div>
                    {m.reasoning && (
                      <details>
                        <summary>
                          Thinking ·{" "}
                          {meta.usage?.completion_tokens_details
                            ?.reasoning_tokens ?? "—"}{" "}
                          tokens
                        </summary>
                        <Markdown text={m.reasoning} />
                      </details>
                    )}
                    {meta.segments?.length ? (
                      meta.segments.map((seg, i) =>
                        seg.kind === "text" ? (
                          <Markdown key={i} text={seg.text} />
                        ) : (
                          <pre key={i}>
                            {JSON.stringify(toolCallsOf(m)[seg.index], null, 2)}
                          </pre>
                        ),
                      )
                    ) : (
                      <>
                        <Markdown text={textOf(m)} />
                        {toolCallsOf(m).map((c) => (
                          <pre key={c.id}>
                            {c.function.name}({c.function.arguments})
                          </pre>
                        ))}
                      </>
                    )}
                    {m.attachments?.map((a) => (
                      <a
                        key={a.file}
                        href={`/api/admin/chats/${chat.id}/${a.file}`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {a.name ?? a.file}
                      </a>
                    ))}
                    {m.role === "assistant" && (
                      <p class="meta">
                        {meta.usage?.prompt_tokens ?? "—"} in /{" "}
                        {meta.usage?.completion_tokens ?? "—"} out ·{" "}
                        {meta.usage?.prompt_tokens_details?.cached_tokens ?? 0}{" "}
                        cached · TTFT {Math.round(meta.ttft_ms ?? 0)} ms ·{" "}
                        {meta.timings?.predicted_per_second?.toFixed(1) ?? "—"}{" "}
                        tok/s · {meta.finish_reason}
                      </p>
                    )}
                    {m.role === "user" && (
                      <Button
                        disabled={busy}
                        onClick={() => {
                          setEditing(m);
                          setDraft(textOf(m));
                        }}
                      >
                        Edit & resend
                      </Button>
                    )}
                    {m.role === "assistant" && (
                      <Button
                        disabled={busy}
                        onClick={() => {
                          try {
                            void generate({
                              ...configure(),
                              active_leaf: m.parent,
                            });
                          } catch (e) {
                            setError(e);
                          }
                        }}
                      >
                        Regenerate
                      </Button>
                    )}
                  </article>
                );
              })}
              {pending.map((call) => (
                <div class="stack" key={call.id}>
                  <label>
                    Result for {call.function.name}
                    <textarea
                      value={toolResults[call.id] ?? ""}
                      onInput={(e) =>
                        setToolResults({
                          ...toolResults,
                          [call.id]: e.currentTarget.value,
                        })
                      }
                    />
                  </label>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void toolResult(call, toolResults[call.id] ?? "")
                    }
                  >
                    Submit result
                  </Button>
                  {mcp?.tools.some(
                    (t) => `${t.server}__${t.name}` === call.function.name,
                  ) && (
                    <Button
                      disabled={busy}
                      onClick={() => setToolApproval(call)}
                    >
                      Run MCP tool
                    </Button>
                  )}
                </div>
              ))}
              {!pending.length && thread.at(-1)?.role === "tool" && (
                <Button
                  disabled={busy}
                  onClick={() => void generate(configure())}
                >
                  Send tool results
                </Button>
              )}
              {progress && (
                <>
                  <ProgressBar
                    label="Reading prompt"
                    value={
                      progress.total ? progress.processed / progress.total : 0
                    }
                  />
                  <p role="status">
                    Reading prompt {progress.processed} / {progress.total} (
                    {progress.cache} cached)
                  </p>
                </>
              )}
              {!!error && <LoadError thing="chat" error={error} />}
              <form
                class="stack chat-composer"
                onSubmit={(e) => {
                  e.preventDefault();
                  void send();
                }}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  if (selectedModel?.vision !== false)
                    attach(Array.from(e.dataTransfer?.files ?? []));
                }}
              >
                {editing && (
                  <p>
                    Editing creates a new branch.{" "}
                    <Button
                      onClick={() => {
                        setEditing(null);
                        setDraft("");
                      }}
                    >
                      Cancel edit
                    </Button>
                  </p>
                )}
                <label>
                  Message
                  <textarea
                    rows={4}
                    disabled={busy || pending.length > 0}
                    value={draft}
                    onInput={(e) => setDraft(e.currentTarget.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
                        e.preventDefault();
                        void send();
                      }
                    }}
                    onPaste={(e) => {
                      if (
                        e.clipboardData?.files.length &&
                        selectedModel?.vision !== false
                      )
                        attach(Array.from(e.clipboardData.files));
                    }}
                  />
                </label>
                <div class="cluster">
                  <label
                    title={
                      selectedModel?.vision === false
                        ? "Model loaded with language-only"
                        : undefined
                    }
                  >
                    Attach images or PDFs
                    <input
                      type="file"
                      multiple
                      accept="image/*,application/pdf"
                      disabled={busy || selectedModel?.vision === false}
                      onChange={(e) => {
                        attach(Array.from(e.currentTarget.files ?? []));
                        e.currentTarget.value = "";
                      }}
                    />
                  </label>
                  {busy ? (
                    <Button onClick={() => abort.current?.abort()}>Stop</Button>
                  ) : (
                    <Button
                      type="submit"
                      variant="accent"
                      disabled={!chat.model || pending.length > 0}
                    >
                      Send
                    </Button>
                  )}
                </div>
                {files.map((file, i) => (
                  <p key={i}>
                    {file.name}{" "}
                    <Button
                      onClick={() => setFiles(files.filter((_, j) => i !== j))}
                    >
                      Remove
                    </Button>
                  </p>
                ))}
              </form>
            </>
          )}
        </section>
        {chat && (
          <aside class="chat-options stack">
            <details open>
              <summary>System</summary>
              <label>
                System prompt
                <textarea
                  disabled={busy}
                  rows={6}
                  value={chat.system ?? ""}
                  onInput={(e) =>
                    setChat({ ...chat, system: e.currentTarget.value })
                  }
                />
              </label>
            </details>
            <details>
              <summary>Sampling</summary>
              <p>
                Omitted fields use model defaults. Explicit values override the
                selected profile.
              </p>
              <label>
                Sampling JSON
                <textarea
                  class="mono"
                  disabled={busy}
                  rows={10}
                  value={samplingText}
                  onInput={(e) => setSamplingText(e.currentTarget.value)}
                />
              </label>
              <Button onClick={() => setSamplingText("{}")}>
                Reset to model defaults
              </Button>
            </details>
            <details>
              <summary>Tools</summary>
              <label>
                Tool definitions JSON
                <textarea
                  class="mono"
                  disabled={busy}
                  rows={10}
                  value={toolsText}
                  onInput={(e) => setToolsText(e.currentTarget.value)}
                />
              </label>
              <label>
                Tool choice
                <select
                  value={String(chat.tool_choice ?? "auto")}
                  onChange={(e) =>
                    setChat({ ...chat, tool_choice: e.currentTarget.value })
                  }
                >
                  <option>auto</option>
                  <option>none</option>
                  <option>required</option>
                </select>
              </label>
              <Button
                disabled={busy}
                onClick={() =>
                  void api
                    .get<McpToolList>("/mcp/tools")
                    .then((result) => {
                      setMcp(result);
                      setToolsText(
                        JSON.stringify(
                          result.tools.map((t) => ({
                            type: "function",
                            function: {
                              name: `${t.server}__${t.name}`,
                              description: t.description,
                              parameters: t.input_schema,
                            },
                          })),
                          null,
                          2,
                        ),
                      );
                    })
                    .catch(setError)
                }
              >
                Add MCP tools
              </Button>
              <p>MCP calls ask for confirmation before execution.</p>
            </details>
            <details>
              <summary>Output</summary>
              <label>
                Response format JSON
                <textarea
                  class="mono"
                  disabled={busy}
                  rows={8}
                  value={formatText}
                  onInput={(e) => setFormatText(e.currentTarget.value)}
                />
              </label>
              <p>
                Use null for text, or an object with type json_object or
                json_schema.
              </p>
            </details>
            <Button
              disabled={busy}
              onClick={() => {
                try {
                  void save(configure()).catch(setError);
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Save conversation settings
            </Button>
          </aside>
        )}
      </div>
      <ConfirmSheet
        open={deleting}
        title="Delete conversation."
        confirmLabel="Delete"
        onClose={() => setDeleting(false)}
        onConfirm={async () => {
          if (!chat) return;
          try {
            await api.del(`/chats/${chat.id}`);
            setChat(null);
            setDeleting(false);
            await list.reload();
          } catch (e) {
            setError(e);
          }
        }}
      >
        <p>
          This conversation and all its branches will be permanently deleted.
        </p>
      </ConfirmSheet>
      <ConfirmSheet
        open={!!toolApproval}
        title="Run MCP tool."
        confirmLabel="Run tool"
        busy={busy}
        onClose={() => setToolApproval(null)}
        onConfirm={() => runMcp(toolApproval!)}
      >
        <p>{toolApproval?.function.name}</p>
        <pre>{toolApproval?.function.arguments}</pre>
      </ConfirmSheet>
    </>
  );
}
