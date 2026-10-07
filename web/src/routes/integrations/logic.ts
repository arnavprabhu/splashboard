/**
 * Integrations page logic (docs/ui/09, SPEC §10.7, §11): launch commands, the SDK snippets,
 * desktop-app state text and the Mac-only rule. Pure functions so they can be unit tested.
 */
import type { CliIntegration, DesktopIntegration } from '../../api/models';

/** `splash launch <client>`, with `--model` only when the pick differs from the active model (D-09-1). */
export function launchCommand(cli: Pick<CliIntegration, 'command'>, pick: string, active: string | null): string {
  const base = cli.command;
  if (!pick || pick === active) return base;
  return `${base} --model ${pick}`;
}

/** Mac-only actions (Open in Terminal, Open app, View backup) need the page to be on the manager's Mac (D-09-2). */
export function isLoopbackHost(hostname: string): boolean {
  const h = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return h === 'localhost' || h === '::1' || h === '127.0.0.1' || /^127\./.test(h);
}

/** The API base the snippets use: the page origin, with 0.0.0.0 replaced by 127.0.0.1 (docs/ui/09 §5). */
export function snippetOrigin(origin: string): string {
  return origin.replace('//0.0.0.0', '//127.0.0.1');
}

export type SdkTab = 'openai_py' | 'openai_js' | 'anthropic_py' | 'anthropic_js' | 'typesafe' | 'bionic' | 'jan';
export const SDK_TABS: readonly SdkTab[] = ['openai_py', 'openai_js', 'anthropic_py', 'anthropic_js', 'typesafe', 'bionic', 'jan'];

export interface SnippetContext {
  origin: string;
  model: string;
  /** `security.api_key_required`: snippets reference SPLASH_API_KEY, never the value (D-09-7). */
  auth: boolean;
}

const py = (s: string) => JSON.stringify(s);
const KEY_NOTE = 'export SPLASH_API_KEY first (Settings → Security → Reveal)';

export function openaiPython({ origin, model, auth }: SnippetContext): string {
  const lines: string[] = [];
  if (auth) lines.push('import os', '', `# ${KEY_NOTE}`);
  lines.push(
    'from openai import OpenAI',
    '',
    `client = OpenAI(base_url=${py(`${origin}/v1`)}, api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'})`,
    '',
    'stream = client.chat.completions.create(',
    `    model=${py(model)},`,
    '    messages=[{"role": "user", "content": "Hello"}],',
    '    stream=True,',
    ')',
    'for chunk in stream:',
    '    if chunk.choices and chunk.choices[0].delta.content:',
    '        print(chunk.choices[0].delta.content, end="", flush=True)',
  );
  return lines.join('\n');
}

export function openaiJs({ origin, model, auth }: SnippetContext): string {
  const lines: string[] = [];
  if (auth) lines.push(`// ${KEY_NOTE}`);
  lines.push(
    'import OpenAI from "openai";',
    '',
    `const client = new OpenAI({ baseURL: ${py(`${origin}/v1`)}, apiKey: ${auth ? 'process.env.SPLASH_API_KEY' : '"local"'} });`,
    '',
    'const stream = await client.chat.completions.create({',
    `  model: ${py(model)},`,
    '  messages: [{ role: "user", content: "Hello" }],',
    '  stream: true,',
    '});',
    'for await (const chunk of stream) {',
    '  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");',
    '}',
  );
  return lines.join('\n');
}

export function anthropicPython({ origin, model, auth }: SnippetContext): string {
  const lines: string[] = [];
  if (auth) lines.push('import os', '', `# ${KEY_NOTE}`);
  lines.push(
    'from anthropic import Anthropic',
    '',
    `client = Anthropic(base_url=${py(origin)}, api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'})`,
    '',
    'message = client.messages.create(',
    `    model=${py(model)},`,
    '    max_tokens=1024,',
    '    messages=[{"role": "user", "content": "Hello"}],',
    '    # Thinking is off when omitted; turn it on with:',
    '    # thinking={"type": "enabled", "budget_tokens": 2048},',
    ')',
    'print(message.content[0].text)',
  );
  return lines.join('\n');
}

export function anthropicJs({ origin, model, auth }: SnippetContext): string {
  const lines: string[] = [];
  if (auth) lines.push(`// ${KEY_NOTE}`);
  lines.push(
    'import Anthropic from "@anthropic-ai/sdk";',
    '',
    `const client = new Anthropic({ baseURL: ${py(origin)}, apiKey: ${auth ? 'process.env.SPLASH_API_KEY' : '"local"'} });`,
    '',
    'const message = await client.messages.create({',
    `  model: ${py(model)},`,
    '  max_tokens: 1024,',
    '  messages: [{ role: "user", content: "Hello" }],',
    '  // Thinking is off when omitted; turn it on with:',
    '  // thinking: { type: "enabled", budget_tokens: 2048 },',
    '});',
    'console.log(message.content[0].text);',
  );
  return lines.join('\n');
}

/** The `TypeSafeClient` example from Splash DEVELOPMENT.md "Judgment contracts", model filled in. */
export function typesafePython({ origin, model, auth }: SnippetContext): string {
  const lines: string[] = [];
  if (auth) lines.push('import os', '', `# ${KEY_NOTE}`);
  lines.push(
    'from typesafe_sdk import Choice, Noul, Score, TypeSafeClient',
    '',
    'with TypeSafeClient(',
    `    base_url=${py(origin)},`,
    `    api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'},`,
    `    model=${py(model)},`,
    ') as client:',
    '    result = client.system_one(',
    '        state={"message": "I was charged twice. Please fix this today."},',
    '        questions={',
    '            "billing": Noul(instructions="Is this about billing?"),',
    '            "department": Choice(',
    '                instructions="Which team should handle this?",',
    '                criteria={"billing": None, "technical": None, "sales": None},',
    '            ),',
    '            "urgency": Score(',
    '                instructions="How urgent is the request?",',
    '                criteria=["No urgency", "This week", "Today"],',
    '            ),',
    '        },',
    '    )',
    '    print(result.choices["department"].choice)',
  );
  return lines.join('\n');
}

/** Code for the code tabs; null for the two prose tabs (Bionic, Jan). */
export function sdkSnippet(tab: SdkTab, ctx: SnippetContext): string | null {
  switch (tab) {
    case 'openai_py':
      return openaiPython(ctx);
    case 'openai_js':
      return openaiJs(ctx);
    case 'anthropic_py':
      return anthropicPython(ctx);
    case 'anthropic_js':
      return anthropicJs(ctx);
    case 'typesafe':
      return typesafePython(ctx);
    default:
      return null;
  }
}

export const TAURI_ORIGIN = 'tauri://localhost';

export type OriginState = 'allowed' | 'wildcard' | 'missing';

export function tauriOriginState(origins: readonly string[] | null | undefined): OriginState {
  const list = origins ?? [];
  if (list.includes('*')) return 'wildcard';
  return list.includes(TAURI_ORIGIN) ? 'allowed' : 'missing';
}

/** Appends exactly `tauri://localhost` (exact origins only, SPEC §8.3). */
export function withTauriOrigin(origins: readonly string[] | null | undefined): string[] {
  const list = [...(origins ?? [])];
  return list.includes(TAURI_ORIGIN) ? list : [...list, TAURI_ORIGIN];
}

// ---------- CLI rows ----------

/** First `\d+(\.\d+)+` match of a `--version` line (docs/ui/09 §3.1). */
export function parseVersion(text: string | null | undefined): string | null {
  const m = text?.match(/\d+(?:\.\d+)+/);
  return m ? m[0] : null;
}

/** Hermes profiles / Pi providers Splash's launcher created (`splash`, `splash-<port>`). */
export function removableEntries(cli: Pick<CliIntegration, 'name' | 'entries'>): string[] {
  if (cli.name !== 'hermes' && cli.name !== 'pi') return [];
  return cli.entries ?? [];
}

/** D47/D60: profile fields are defaults only (D12), so a client that sends a field
 * itself would override the profile's value. Each client's line says how
 * `splash launch` passes a profile's reasoning effort for the session. */
export type ProfileNoteClient = 'claude' | 'codex' | 'opencode' | 'hermes' | 'pi';
export function profileNoteClient(name: string): ProfileNoteClient | null {
  return ['claude', 'codex', 'opencode', 'hermes', 'pi'].includes(name) ? (name as ProfileNoteClient) : null;
}

/** Where the removable entry lives, for the confirmation sheet (docs/ui/09 §3.4). */
export function entryFile(name: 'hermes' | 'pi', entry: string): string {
  return name === 'hermes' ? `~/.hermes/profiles/${entry}/config.yaml` : '~/.pi/agent/models.json';
}

export const BACKUP_DIR = '~/.splash/integrations/backups/';

// ---------- desktop rows ----------

export type DesktopView =
  | 'not_detected'
  | 'not_connected'
  | 'connecting'
  | 'connected'
  | 'restoring'
  | 'needs_restore';

export function desktopView(app: Pick<DesktopIntegration, 'detected' | 'state'>): DesktopView {
  if (!app.detected && app.state === 'not_connected') return 'not_detected';
  return app.state;
}

/** HH:MM of a connection time, local. */
export function clock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** The four Claude Desktop slots SPEC §11.3.1 names; `null` means "follows the active model" (D-09-4). */
export const CLAUDE_SLOTS = ['claude-fable-5', 'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001'] as const;

export type Slots = Record<string, string | null>;

export function normaliseSlots(raw: Record<string, unknown> | null | undefined): Slots {
  const out: Slots = {};
  const keys = new Set<string>([...CLAUDE_SLOTS, ...Object.keys(raw ?? {})]);
  for (const k of keys) {
    const v = raw?.[k];
    out[k] = typeof v === 'string' && v ? v : null;
  }
  return out;
}

/**
 * True when the slots resolve to more than one real model, so Claude may switch models between
 * a main turn and a subagent (docs/ui/09 §4.4). `roots` maps profile IDs to their model.
 */
export function slotsSwitchModels(slots: Slots, active: string | null, roots: ReadonlyMap<string, string>): boolean {
  const models = new Set(Object.values(slots).map((v) => (v === null ? active : (roots.get(v) ?? v))));
  return models.size > 1;
}

/** "What this changes" for the desktop apps (docs/ui/09 §4.5, SPEC §11.3). Change text is in the string table. */
export const DESKTOP_FILES: Record<DesktopIntegration['name'], ReadonlyArray<{ path: string; change: string }>> = {
  'claude-desktop': [
    { path: '~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json', change: 'integrations.files.claude_config' },
    { path: '~/Library/Application Support/Claude-3p/configLibrary/_meta.json', change: 'integrations.files.claude_meta' },
    { path: '~/Library/Application Support/Claude/claude_desktop_config.json, Claude-3p/claude_desktop_config.json', change: 'integrations.files.claude_mode' },
    { path: '~/.splash/integrations/state.json, backups/claude-desktop/<timestamp>/', change: 'integrations.files.backups' },
  ],
  'codex-app': [
    { path: '~/.codex/config.toml', change: 'integrations.files.codex_config' },
    { path: '~/.splash/integrations/codex-app/models.json', change: 'integrations.files.codex_models' },
    { path: '~/.splash/integrations/codex-app/routing.json', change: 'integrations.files.codex_routing' },
    { path: '~/.codex/auth.json', change: 'integrations.files.codex_auth' },
    { path: '~/.splash/integrations/state.json, backups/codex-app/<timestamp>/', change: 'integrations.files.backups' },
  ],
};

// ---------- connect / restore progress (api.md §12.1, `integration.state` step) ----------

export type Step = NonNullable<DesktopIntegration['step']>;

/** The order the manager reports steps in (api.md §12.1). */
export function stepsFor(name: DesktopIntegration['name'], kind: 'connect' | 'restore'): Step[] {
  if (kind === 'restore') return ['quitting_app', 'restoring_files', 'opening_app'];
  return name === 'claude-desktop'
    ? ['quitting_app', 'backing_up', 'starting_gateway', 'writing_config', 'opening_app']
    : ['quitting_app', 'backing_up', 'writing_config', 'opening_app'];
}

export type StepView = 'done' | 'current' | 'pending' | 'failed';

/** Each step's state given the step the manager last reported (`done`/`failed` end the sequence). */
export function stepViews(steps: readonly Step[], current: Step | null | undefined): StepView[] {
  if (current === 'done') return steps.map(() => 'done');
  const i = current ? steps.indexOf(current) : -1;
  return steps.map((_, k) => (i < 0 ? 'pending' : k < i ? 'done' : k === i ? 'current' : 'pending'));
}

/**
 * What the Claude Desktop gateway answers on `/v1/models` for the current slots
 * (manager/splash_gui/integrations/service.py `models`): one entry per slot, named after its
 * target, else the active model, else "Splash". Computed here because the gateway listens on
 * its own loopback port without CORS, and only while connected.
 */
export function gatewayModels(slots: Slots, active: string | null): Record<string, unknown> {
  const entries = Object.entries(slots).map(([slot, target]) => ({
    id: slot,
    type: 'model',
    display_name: target || active || 'Splash',
    created_at: '2026-10-03T00:00:00Z',
    max_tokens: 262144,
    anthropic_family_tier: slot.split('-')[1] ?? null,
    is_family_default: true,
  }));
  return { data: entries, first_id: entries[0]?.id ?? null, last_id: entries[entries.length - 1]?.id ?? null, has_more: false };
}
