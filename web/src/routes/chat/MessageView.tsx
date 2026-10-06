/**
 * One message in the thread (docs/ui/07 §5): role label, thinking block, body with tool-call
 * blocks in stream order, the meta line with branch arrows, and the message actions.
 */
import { useEffect, useState } from 'preact/hooks';
import { Button } from '../../components/Button';
import { CodeBlock } from '../../components/CodeBlock';
import { copyText } from '../../components/CopyButton';
import { Menu } from '../../components/Menu';
import { TextArea } from '../../components/inputs';
import { toast } from '../../components/Toast';
import { formatCount, formatMs, formatTokPerSec } from '../../lib/format';
import { t } from '../../strings/chat';
import { estimateTokens } from './accumulator';
import { checkResult, shortModel } from './logic';
import { Markdown } from './Markdown';
import { metaOf, textOf, toolCallsOf, type ChatMessage, type ToolCall } from './types';

export interface ToolDraft {
  text: string;
  source: 'manual' | 'mcp' | 'skipped' | 'unknown';
  server?: string | null;
  duration_ms?: number | null;
  is_error?: boolean;
  auto?: boolean;
}

export interface ToolContext {
  /** Tool messages already in the thread, by tool_call_id. */
  results: ReadonlyMap<string, ChatMessage>;
  /** Results typed or run but not sent yet. */
  drafts: Readonly<Record<string, ToolDraft>>;
  /** Execution mode of the chat; MCP calls confirm before they run. */
  mode: 'manual' | 'mcp';
  /** Tool name → MCP server, for enabled servers. */
  mcpServer: (name: string) => { server: string; alwaysAllow: boolean } | null;
  /** Names defined in the Tools panel. */
  defined: ReadonlySet<string>;
  /** Whether this message's calls are still waiting (last assistant on the path, not streaming). */
  pending: boolean;
  onDraft: (callId: string, draft: ToolDraft | null) => void;
  onRunMcp: (call: ToolCall, server: string, always: boolean) => void;
  running: ReadonlySet<string>;
}

export interface MessageViewProps {
  message: ChatMessage;
  streaming: boolean;
  /** Live thinking state while streaming. */
  thinkingLive?: { active: boolean; ms: number | null } | null;
  branch: { index: number; count: number; newest: boolean };
  replies: number;
  busy: boolean;
  tools: ToolContext;
  onRegenerate?: () => void;
  onEdit?: () => void;
  onDelete: () => void;
  onBranch: (dir: -1 | 1) => void;
  focusId?: string;
}

function roleLabel(m: ChatMessage): string {
  if (m.role === 'user') return t('chat.role.user');
  if (m.role === 'system') return t('chat.role.system');
  if (m.role === 'tool') return t('chat.role.tool');
  const meta = metaOf(m);
  const model = meta.model ? shortModel(meta.model) : '';
  const profile = meta.profile && meta.profile !== 'default' ? ` : ${meta.profile}` : '';
  return model ? `${t('chat.role.assistant')} · ${model}${profile}` : t('chat.role.assistant');
}

function clock(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function ThinkingBlock({ text, live, ms, tokens }: { text: string; live: boolean; ms: number | null; tokens: number | null | undefined }) {
  const [open, setOpen] = useState(live);
  useEffect(() => {
    setOpen(live);
  }, [live]);
  const secs = ms === null ? null : (ms / 1000).toFixed(1);
  const parts = [t('chat.thinking.label')];
  if (secs !== null) parts.push(t('chat.thinking.elapsed', { s: secs }) + (live ? '…' : ''));
  if (!live) parts.push(typeof tokens === 'number' ? t('chat.thinking.tokens', { n: formatCount(tokens) }) : t('chat.thinking.tokens_pending'));
  return (
    <details class="disclosure chat-thinking" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary class="label">
        <span class="disclosure-glyph" aria-hidden="true">
          ▸
        </span>
        <span class="tnum">{parts.join(' · ')}</span>
      </summary>
      <div class="disclosure-body chat-thinking-body">
        <Markdown text={text} />
      </div>
    </details>
  );
}

function prettyArgs(args: string): { text: string; valid: boolean } {
  try {
    return { text: JSON.stringify(JSON.parse(args), null, 2), valid: true };
  } catch {
    return { text: args, valid: false };
  }
}

export function ToolCallBlock({ call, streaming, ctx }: { call: ToolCall; streaming: boolean; ctx: ToolContext }) {
  const sent = ctx.results.get(call.id);
  const draft = ctx.drafts[call.id];
  const args = prettyArgs(call.function.arguments);
  const mcp = ctx.mode === 'mcp' ? ctx.mcpServer(call.function.name) : null;
  const unknown = !mcp && !ctx.defined.has(call.function.name);
  const [text, setText] = useState(unknown ? t('chat.tool.unknown_prefill') : '');
  const [editing, setEditing] = useState(false);
  const meta = sent ? metaOf(sent).tool : null;
  const result = sent ? textOf(sent) : draft?.text;
  const source = sent ? (meta?.source ?? 'manual') : draft?.source;
  const server = sent ? meta?.server : draft?.server;
  const duration = sent ? meta?.duration_ms : draft?.duration_ms;
  const isError = sent ? meta?.is_error : draft?.is_error;
  const auto = sent ? meta?.auto : draft?.auto;
  const state = result !== undefined && !editing
    ? source === 'skipped'
      ? t('chat.tool.skipped')
      : source === 'mcp'
        ? `${t('chat.tool.mcp', { server: server ?? '' })}${duration != null ? ` · ${formatMs(duration)}` : ''}${auto ? ` · ${t('chat.tool.auto_allowed')}` : ''}`
        : isError
          ? t('chat.tool.error')
          : t('chat.tool.manual')
    : unknown
      ? t('chat.tool.unknown')
      : mcp
        ? t('chat.tool.mcp', { server: mcp.server })
        : t('chat.tool.pending');
  const showInput = ctx.pending && !sent && !streaming && (draft === undefined || editing);
  return (
    <div class="chat-tool" data-testid="tool-call">
      <div class="chat-tool-head">
        <span class="label">{t('chat.tool.call')}</span> <span class="mono">{call.function.name || '…'}</span>
        <span class="meta mono chat-tool-id">{call.id}</span>
      </div>
      <span class="label">{t('chat.tool.arguments')}</span>
      <pre class="mono chat-tool-pre">{args.text}</pre>
      {!args.valid && !streaming && <p class="meta">{t('chat.tool.invalid_args')}</p>}
      {!streaming && (
        <>
          <span class="label">
            {t('chat.tool.result')} · <span class={isError ? 'acc' : undefined}>{state}</span>
          </span>
          {result !== undefined && !editing && (
            <>
              <pre class={isError ? 'mono chat-tool-pre acc' : 'mono chat-tool-pre'}>{result}</pre>
              {!sent && ctx.pending && (
                <Button size="s" variant="text" onClick={() => (setText(result), setEditing(true))}>
                  {t('chat.tool.edit_result')}
                </Button>
              )}
            </>
          )}
          {showInput && mcp && !editing ? (
            <div class="stack chat-tool-confirm">
              <p class="body">{t('chat.tool.confirm_q', { tool: call.function.name, server: mcp.server })}</p>
              <div class="cluster">
                <Button size="s" variant="solid" loading={ctx.running.has(call.id)} onClick={() => ctx.onRunMcp(call, mcp.server, false)}>
                  {t('chat.tool.run')}
                </Button>
                <Button size="s" disabled={ctx.running.has(call.id)} onClick={() => ctx.onRunMcp(call, mcp.server, true)}>
                  {t('chat.tool.run_always')}
                </Button>
                <Button size="s" variant="text" onClick={() => ctx.onDraft(call.id, { text: t('chat.tool.skip_text'), source: 'skipped' })}>
                  {t('chat.tool.skip')}
                </Button>
              </div>
            </div>
          ) : (
            showInput && (
              <div class="stack chat-tool-input">
                <TextArea
                  class="mono"
                  rows={3}
                  value={text}
                  placeholder={t('chat.tool.result_placeholder')}
                  aria-label={t('chat.tool.result_label', { name: call.function.name })}
                  onChange={setText}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      ctx.onDraft(call.id, { text, source: unknown ? 'unknown' : 'manual' });
                      setEditing(false);
                    }
                  }}
                />
                <div class="cluster">
                  <Button
                    size="s"
                    variant="solid"
                    onClick={() => {
                      ctx.onDraft(call.id, { text, source: unknown ? 'unknown' : 'manual' });
                      setEditing(false);
                    }}
                  >
                    {t('chat.tool.submit')}
                  </Button>
                  <Button
                    size="s"
                    variant="text"
                    onClick={() => {
                      ctx.onDraft(call.id, { text: t('chat.tool.skip_text'), source: 'skipped' });
                      setEditing(false);
                    }}
                  >
                    {t('chat.tool.skip')}
                  </Button>
                </div>
              </div>
            )
          )}
        </>
      )}
    </div>
  );
}

function MetaLine({ m, streaming }: { m: ChatMessage; streaming: boolean }) {
  const meta = metaOf(m);
  const u = meta.usage;
  const items: Array<{ key: string; text: string; tip: string }> = [];
  if (u?.prompt_tokens != null) {
    const c = u.prompt_tokens_details?.cached_tokens ?? 0;
    items.push({
      key: 'in',
      text: c ? t('chat.meta.in_cached', { n: formatCount(u.prompt_tokens), c: formatCount(c) }) : t('chat.meta.in', { n: formatCount(u.prompt_tokens) }),
      tip: t('chat.meta.tip.in', { n: u.prompt_tokens, c }),
    });
  }
  if (u?.completion_tokens != null) {
    items.push({ key: 'out', text: t('chat.meta.out', { n: formatCount(u.completion_tokens) }), tip: t('chat.meta.tip.out', { n: u.completion_tokens }) });
  } else if (!streaming && (meta.finish_reason === 'stopped' || meta.finish_reason === 'disconnected')) {
    items.push({ key: 'out', text: t('chat.meta.out_est', { n: formatCount(meta.est_out ?? estimateTokens(textOf(m))) }), tip: t('chat.meta.tip.out_est') });
  }
  if (meta.ttft_ms != null) {
    items.push({
      key: 'ttft',
      text: t('chat.meta.ttft', { v: formatMs(meta.ttft_ms) }),
      tip: t('chat.meta.tip.ttft', { ms: meta.timings?.prompt_ms != null ? Math.round(meta.timings.prompt_ms) : '—' }),
    });
  }
  const tps = meta.timings?.predicted_per_second;
  if (tps != null) items.push({ key: 'tps', text: formatTokPerSec(tps), tip: t('chat.meta.tip.tokps', { v: tps }) });
  if (!streaming && meta.finish_reason) {
    const key = `chat.meta.finish.${meta.finish_reason}` as 'chat.meta.finish.stop';
    const label = t(key);
    items.push({ key: 'finish', text: label === key ? String(meta.finish_reason) : label, tip: t('chat.meta.tip.finish', { v: meta.finish_reason }) });
  }
  if (meta.profile && meta.profile !== 'default') {
    items.push({ key: 'profile', text: meta.profile, tip: t('chat.meta.tip.profile', { v: meta.profile, id: meta.model ?? '' }) });
  }
  const inj = meta.injected ? Object.entries(meta.injected) : [];
  if (inj.length) {
    items.push({ key: 'injected', text: t('chat.meta.injected', { n: inj.length }), tip: inj.map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join('\n') });
  }
  if (items.length === 0) return null;
  return (
    <span class="meta tnum chat-meta">
      {items.map((it, i) => (
        <span key={it.key} title={it.tip} class={it.key === 'finish' && meta.finish_reason === 'disconnected' ? 'acc' : undefined}>
          {i > 0 && ' · '}
          {it.text}
        </span>
      ))}
    </span>
  );
}

export function BranchNav({ index, count, newest, onMove }: { index: number; count: number; newest: boolean; onMove: (dir: -1 | 1) => void }) {
  if (count <= 1) return null;
  return (
    <span class="chat-branch" role="group" aria-label={t('chat.msg.branch_label', { i: index, n: count })}>
      <button type="button" class="btn" data-variant="text" data-size="s" disabled={index <= 1} aria-label={t('chat.msg.branch_prev')} onClick={() => onMove(-1)}>
        ‹
      </button>
      <span class={newest ? 'meta tnum' : 'meta tnum acc'} data-accent={newest ? undefined : 'true'}>
        {t('chat.msg.branch_count', { i: index, n: count })}
      </span>
      <button type="button" class="btn" data-variant="text" data-size="s" disabled={index >= count} aria-label={t('chat.msg.branch_next')} onClick={() => onMove(1)}>
        ›
      </button>
    </span>
  );
}

function asMarkdown(m: ChatMessage, withThinking: boolean): string {
  const parts: string[] = [];
  if (withThinking && m.reasoning) parts.push(`<details><summary>${t('chat.thinking.label')}</summary>\n\n${m.reasoning}\n\n</details>`);
  parts.push(textOf(m));
  for (const c of toolCallsOf(m)) parts.push('```json\n' + JSON.stringify({ name: c.function.name, arguments: c.function.arguments }, null, 2) + '\n```');
  return parts.filter(Boolean).join('\n\n');
}

function headLabel(m: ChatMessage): string {
  if (m.role !== 'assistant') return roleLabel(m);
  const meta = metaOf(m);
  if (!meta.model) return t('chat.role.assistant');
  return `${shortModel(meta.model)}${meta.profile && meta.profile !== 'default' ? ` : ${meta.profile}` : ''}`;
}

export function MessageView({ message: m, streaming, thinkingLive, branch, replies, busy, tools, onRegenerate, onEdit, onDelete, onBranch, focusId }: MessageViewProps) {
  const [confirm, setConfirm] = useState(false);
  const meta = metaOf(m);
  const calls = toolCallsOf(m);
  const segments = meta.segments?.length ? meta.segments : [{ kind: 'text' as const, text: textOf(m) }, ...calls.map((_, index) => ({ kind: 'tool' as const, index }))];
  const format = meta.response_format ?? null;
  const copy = async (text: string) => {
    if (await copyText(text)) toast(t('chat.msg.copied'));
  };
  const thinkingMs = thinkingLive ? thinkingLive.ms : (meta.thinking_ms ?? null);
  const isUser = m.role === 'user';
  const confirmRow = confirm && (
    <span class="cluster chat-confirm" role="alert">
      <span class="meta">{replies > 0 ? t('chat.msg.delete_confirm', { n: replies }) : t('chat.msg.delete_confirm_single')}</span>
      <Button size="s" variant="solid" onClick={() => (setConfirm(false), onDelete())}>
        {t('common.delete')}
      </Button>
      <Button size="s" variant="text" onClick={() => setConfirm(false)}>
        {t('common.cancel')}
      </Button>
    </span>
  );
  // Assistant and system messages: the actions sit in the head row (copy, regenerate, more ▾).
  const headActions = !streaming && !isUser && (
    <div class="chat-actions cluster" role="group" aria-label={t('chat.msg.actions')}>
      {m.role === 'assistant' && (
        <Button size="s" variant="text" onClick={() => void copy(textOf(m))}>
          {t('chat.msg.copy')}
        </Button>
      )}
      {m.role === 'assistant' && onRegenerate && (
        <Button size="s" variant="text" disabled={busy} onClick={onRegenerate}>
          {t('chat.msg.regenerate')}
        </Button>
      )}
      {m.role === 'system' && onEdit && (
        <Button size="s" variant="text" disabled={busy} onClick={onEdit}>
          {t('chat.msg.edit')}
        </Button>
      )}
      <Menu
        label={t('chat.msg.more')}
        variant="text"
        size="s"
        align="end"
        items={[
          ...(m.role === 'assistant'
            ? [
                { key: 'md', label: t('chat.msg.copy_as_md'), onSelect: () => void copy(asMarkdown(m, false)) },
                ...(m.reasoning ? [{ key: 'think', label: t('chat.msg.copy_with_thinking'), onSelect: () => void copy(asMarkdown(m, true)) }] : []),
              ]
            : []),
          { key: 'delete', label: t('chat.msg.delete_branch'), disabled: busy, onSelect: () => setConfirm(true) },
        ]}
      />
    </div>
  );
  const body = (
    <div class="chat-body">
      {m.role === 'assistant' && format && !streaming ? (
        (() => {
          const text = textOf(m);
          let pretty = text;
          try {
            pretty = JSON.stringify(JSON.parse(text), null, 2);
          } catch {
            /* keep raw */
          }
          const res = checkResult(text, format, meta.finish_reason === 'stopped');
          return (
            <>
              <CodeBlock code={pretty} label="JSON" />
              <p class={res.ok ? 'meta' : 'meta acc'}>{res.line}</p>
            </>
          );
        })()
      ) : (
        segments.map((seg, i) =>
          seg.kind === 'text' ? (
            seg.text ? <Markdown key={i} text={seg.text} /> : null
          ) : calls[seg.index] ? (
            <ToolCallBlock key={calls[seg.index]!.id || i} call={calls[seg.index]!} streaming={streaming} ctx={tools} />
          ) : null,
        )
      )}
      {streaming && !textOf(m) && !m.reasoning && calls.length === 0 && <p class="meta loading-dots" aria-hidden="true" />}
    </div>
  );
  const attachments = (m.attachments?.length ?? 0) > 0 && (
    <ul class="chat-attachments">
      {m.attachments!.map((a, i) => (
        <li key={a.file ?? i} class="meta">
          ▪{' '}
          <a href={a.file ? `/api/admin/chats/attachments/${encodeURIComponent(a.file.split('/').pop() ?? a.file)}` : undefined} target="_blank" rel="noopener noreferrer">
            {a.name ?? a.file}
          </a>
          {meta.attachment_pages?.[i] != null && ` · ${t('chat.att.pages', { n: meta.attachment_pages[i] ?? 0 })}`}
        </li>
      ))}
    </ul>
  );
  return (
    <article
      class="chat-message"
      data-role={m.role}
      tabIndex={-1}
      data-message={m.id}
      id={focusId}
      aria-label={roleLabel(m)}
      onKeyDown={(e) => {
        if (e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
          e.preventDefault();
          onBranch(e.key === 'ArrowLeft' ? -1 : 1);
        }
      }}
    >
      {isUser ? (
        <>
          <div class="chat-user">
            <header class="chat-message-head">
              <span class="meta">{t('chat.role.user')}</span>
              <span class="meta tnum">{clock(m.created_at)}</span>
            </header>
            {body}
            {attachments}
          </div>
          <footer class="chat-message-foot">
            <BranchNav {...branch} onMove={onBranch} />
            {!streaming && (
              <div class="chat-actions cluster" role="group" aria-label={t('chat.msg.actions')}>
                {onEdit && (
                  <Button size="s" variant="text" disabled={busy} onClick={onEdit}>
                    {t('chat.msg.edit')}
                  </Button>
                )}
                <Button size="s" variant="text" onClick={() => void copy(textOf(m))}>
                  {t('chat.msg.copy')}
                </Button>
                {!confirm && (
                  <Button size="s" variant="text" disabled={busy} onClick={() => setConfirm(true)}>
                    {t('chat.msg.delete_branch')}
                  </Button>
                )}
                {confirmRow}
              </div>
            )}
          </footer>
        </>
      ) : (
        <>
          <header class="chat-message-head">
            <span class="label chat-message-who">
              {headLabel(m)}
              <span class="meta tnum chat-message-time">{clock(m.created_at)}</span>
            </span>
            {headActions}
          </header>
          {confirmRow}
          {m.reasoning && (
            <ThinkingBlock
              text={m.reasoning}
              live={!!thinkingLive?.active}
              ms={thinkingMs}
              tokens={meta.usage?.completion_tokens_details?.reasoning_tokens}
            />
          )}
          {body}
          {attachments}
          <footer class="chat-message-foot">
            {m.role === 'assistant' && <MetaLine m={m} streaming={streaming} />}
            <BranchNav {...branch} onMove={onBranch} />
          </footer>
        </>
      )}
    </article>
  );
}
