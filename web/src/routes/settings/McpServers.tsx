/**
 * Settings → Chat & MCP servers: a table with the live state from
 * `POST /mcp/tools` (it starts the servers), an Add/Edit server sheet (stdio command or http URL) and Import mcp.json.
 * Saves immediately through `PUT /mcp/servers` (not the save bar).
 *
 * `env` and `headers` values live in the Keychain: the manager returns each one as
 * `{secret: true, masked}`. Sending that object back keeps the value; a plain string sets a
 * new one. Values are write-only here: the masked text is shown, never sent as a value.
 */
import { useState } from 'preact/hooks';
import type { MaskedSecret, McpServerView, McpValue } from '../../api/mcp';
import { Button } from '../../components/Button';
import { ConfirmSheet } from '../../components/ConfirmSheet';
import { SegmentedControl } from '../../components/controls';
import { TextArea, TextInput } from '../../components/inputs';
import { Sheet } from '../../components/Sheet';
import { LoadError, Loading } from '../../components/States';
import { Table } from '../../components/Table';
import { toast, toastError } from '../../components/Toast';
import { Toggle } from '../../components/Toggle';
import { useApi } from '../../lib/use-api';
import { t } from '../../strings/settings';
import { settingsApi } from './api';

type Servers = Record<string, McpServerView>;

export function isMasked(v: unknown): v is MaskedSecret {
  return !!v && typeof v === 'object' && (v as { secret?: unknown }).secret === true;
}

/** Text that is (or contains) a masked value: never accepted as a new secret. */
export function looksMasked(v: string): boolean {
  return v.includes('••••');
}

/** An existing env/header entry in the edit sheet. */
export interface SecretRow {
  key: string;
  original: McpValue;
  /** A new value typed by the user; null keeps the original. */
  replace: string | null;
  removed: boolean;
}

export function secretRows(values: Record<string, McpValue> | undefined): SecretRow[] {
  return Object.entries(values ?? {}).map(([key, original]) => ({ key, original, replace: null, removed: false }));
}

/**
 * The `env`/`headers` object to send: untouched entries go back as the object the manager
 * returned, replaced ones as the new plain string, removed ones are dropped, then new pairs.
 */
export function mergeSecrets(rows: readonly SecretRow[], added: Record<string, string>): Record<string, McpValue> {
  const out: Record<string, McpValue> = {};
  for (const r of rows) {
    if (r.removed) continue;
    const next = r.replace?.trim();
    out[r.key] = next && !looksMasked(next) ? next : r.original;
  }
  for (const [k, v] of Object.entries(added)) if (!looksMasked(v)) out[k] = v;
  return out;
}

export interface Draft {
  mode: 'add' | 'edit';
  name: string;
  transport: 'stdio' | 'http';
  command: string;
  args: string;
  /** New pairs (KEY=value lines). */
  env: string;
  url: string;
  /** New pairs (Name: value lines). */
  headers: string;
  envRows: SecretRow[];
  headerRows: SecretRow[];
  enabled: boolean;
  always_allow: boolean;
}

export const EMPTY: Draft = { mode: 'add', name: '', transport: 'stdio', command: '', args: '', env: '', url: '', headers: '', envRows: [], headerRows: [], enabled: true, always_allow: false };

export function editDraft(name: string, s: McpServerView): Draft {
  return {
    ...EMPTY,
    mode: 'edit',
    name,
    transport: s.url ? 'http' : 'stdio',
    command: s.command ?? '',
    args: (s.args ?? []).join('\n'),
    url: s.url ?? '',
    envRows: secretRows(s.env),
    headerRows: secretRows(s.headers),
    enabled: s.enabled,
    always_allow: s.always_allow,
  };
}

/** `KEY=value` lines → object. */
export function parsePairs(text: string, sep = '='): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const i = line.indexOf(sep);
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

export function draftError(d: Draft, existing: readonly string[]): string | null {
  if (!/^[A-Za-z0-9_.-]{1,64}$/.test(d.name)) return t('settings.mcp.name_rule');
  if (d.mode === 'add' && existing.includes(d.name)) return t('settings.mcp.name_taken');
  if (d.transport === 'stdio' && !d.command.trim()) return t('settings.mcp.command_needed');
  if (d.transport === 'http' && !/^https?:\/\/\S+$/.test(d.url.trim())) return t('settings.mcp.url_rule');
  const typed = [...d.envRows, ...d.headerRows].map((r) => r.replace ?? '');
  const added = d.transport === 'stdio' ? Object.values(parsePairs(d.env)) : Object.values(parsePairs(d.headers, ':'));
  if ([...typed, ...added].some(looksMasked)) return t('settings.mcp.masked_echo');
  return null;
}

export function draftServer(d: Draft): McpServerView {
  const flags = { enabled: d.mode === 'edit' ? d.enabled : true, always_allow: d.mode === 'edit' ? d.always_allow : false };
  return d.transport === 'stdio'
    ? { command: d.command.trim(), args: d.args.split('\n').map((s) => s.trim()).filter(Boolean), env: mergeSecrets(d.envRows, parsePairs(d.env)), ...flags }
    : { url: d.url.trim(), headers: mergeSecrets(d.headerRows, parsePairs(d.headers, ':')), ...flags };
}

/** One masked value per line for the table (`KEY ••••a1b2`). */
function secretSummary(s: McpServerView): string {
  const vals = { ...(s.env ?? {}), ...(s.headers ?? {}) };
  return Object.entries(vals)
    .map(([k, v]) => `${k} ${isMasked(v) ? v.masked : '••••'}`)
    .join('\n');
}

/** Existing env/header entries: masked value, Replace (write-only) and Remove. */
function SecretRows({ rows, onChange, kind }: { rows: SecretRow[]; onChange: (rows: SecretRow[]) => void; kind: 'env' | 'headers' }) {
  if (rows.length === 0) return null;
  const set = (i: number, patch: Partial<SecretRow>) => onChange(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  return (
    <ul class="mcp-secrets" data-testid={`mcp-secrets-${kind}`}>
      {rows.map((r, i) => (
        <li key={r.key} class="mcp-secret" data-removed={r.removed || undefined}>
          <span class="mono">{r.key}</span>
          <span class="mono meta">{r.removed ? t('settings.mcp.secret_removed') : isMasked(r.original) ? r.original.masked : '••••'}</span>
          {r.replace !== null ? (
            <TextInput
              class="mono"
              type="password"
              autoComplete="off"
              value={r.replace}
              aria-label={t('settings.mcp.secret_new', { key: r.key })}
              placeholder={t('settings.mcp.secret_placeholder')}
              onChange={(v) => set(i, { replace: v })}
            />
          ) : null}
          <span class="cluster">
            {!r.removed && (
              <Button size="s" variant="text" onClick={() => set(i, { replace: r.replace === null ? '' : null })}>
                {r.replace === null ? t('settings.mcp.secret_replace') : t('settings.mcp.secret_keep')}
              </Button>
            )}
            <Button size="s" variant="text" onClick={() => set(i, { removed: !r.removed, replace: null })}>
              {r.removed ? t('settings.mcp.secret_undo') : t('settings.mcp.remove')}
            </Button>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Only plain string values from an imported file (a masked object or a non-string is dropped). */
function plainValues(v: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) if (typeof x === 'string' && !looksMasked(x)) out[k] = x;
  return out;
}

/** Claude-style `mcp.json` (`{"mcpServers": {name: {command, args, env} | {url, headers}}}`). */
export function importMcpJson(text: string): Servers | null {
  try {
    const v = JSON.parse(text) as { mcpServers?: Record<string, Record<string, unknown>> };
    const src = v.mcpServers ?? (v as Record<string, Record<string, unknown>>);
    const out: Servers = {};
    for (const [name, s] of Object.entries(src)) {
      if (!s || typeof s !== 'object') return null;
      out[name] = {
        ...(typeof s.command === 'string' ? { command: s.command, args: Array.isArray(s.args) ? s.args.map(String) : [], env: plainValues(s.env) } : {}),
        ...(typeof s.url === 'string' ? { url: s.url, headers: plainValues(s.headers) } : {}),
        enabled: true,
        always_allow: false,
      };
    }
    return out;
  } catch {
    return null;
  }
}

export function McpServers() {
  const servers = useApi(settingsApi.mcpServers);
  const tools = useApi(settingsApi.mcpTools);
  const [adding, setAdding] = useState<Draft | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const list = servers.data?.servers ?? {};
  async function save(next: Servers, message: string) {
    setBusy(true);
    try {
      await settingsApi.saveMcpServers(next);
      await servers.reload();
      void tools.reload();
      toast(message);
      return true;
    } catch (err) {
      toastError(t('settings.mcp.failed'), err);
      return false;
    } finally {
      setBusy(false);
    }
  }
  const state = (name: string) => {
    const err = tools.data?.errors.find((e) => e.server === name);
    if (err) return t('settings.mcp.state_failed', { error: err.message });
    const n = tools.data?.tools.filter((x) => x.server === name).length ?? null;
    return n === null ? '—' : t('settings.mcp.state_ok', { n });
  };
  const err = adding ? draftError(adding, Object.keys(list)) : null;
  const imported = importing !== null ? importMcpJson(importing) : null;
  return (
    <div class="field stack" data-key="chat.mcp_servers" id="chat.mcp_servers">
      <div class="field-head cluster">
        <span class="label">{t('settings.mcp.title')}</span>
      </div>
      <p class="field-help">{t('settings.mcp.help')}</p>
      {servers.error ? (
        <LoadError thing={t('settings.mcp.thing')} error={servers.error} onRetry={servers.reload} />
      ) : !servers.data ? (
        <Loading />
      ) : (
        <Table
          caption={t('settings.mcp.title')}
          rows={Object.entries(list)}
          rowKey={([name]) => name}
          empty={t('settings.mcp.none')}
          columns={[
            { key: 'name', label: t('settings.mcp.name'), render: ([name]) => <span class="mono">{name}</span> },
            { key: 'transport', label: t('settings.mcp.transport'), render: ([, s]) => (s.url ? 'http' : 'stdio') },
            { key: 'target', label: t('settings.mcp.target'), render: ([, s]) => <span class="mono ms-path">{s.url ?? [s.command, ...(s.args ?? [])].join(' ')}</span> },
            { key: 'secrets', label: t('settings.mcp.secrets'), render: ([, s]) => <span class="mono meta mcp-secret-summary">{secretSummary(s) || '—'}</span> },
            { key: 'state', label: t('settings.mcp.state'), render: ([name]) => <span class="meta">{state(name)}</span> },
            {
              key: 'on',
              label: t('settings.mcp.enabled'),
              render: ([name, s]) => <Toggle checked={s.enabled} label={`${t('settings.mcp.enabled')} · ${name}`} onChange={(on) => void save({ ...list, [name]: { ...s, enabled: on } }, t('settings.mcp.saved'))} />,
            },
            {
              key: 'allow',
              label: t('settings.mcp.always_allow'),
              render: ([name, s]) => <Toggle checked={s.always_allow} label={`${t('settings.mcp.always_allow')} · ${name}`} onChange={(on) => void save({ ...list, [name]: { ...s, always_allow: on } }, t('settings.mcp.saved'))} />,
            },
            {
              key: 'remove',
              label: t('settings.mcp.remove'),
              hideLabel: true,
              render: ([name, s]) => (
                <span class="cluster">
                  <Button size="s" variant="text" aria-label={t('settings.mcp.edit_label', { name })} onClick={() => setAdding(editDraft(name, s))}>
                    {t('settings.mcp.edit')}
                  </Button>
                  <Button size="s" variant="text" onClick={() => setRemoving(name)}>
                    {t('settings.mcp.remove')}
                  </Button>
                </span>
              ),
            },
          ]}
        />
      )}
      <div class="cluster">
        <Button size="s" onClick={() => setAdding({ ...EMPTY })}>
          {t('settings.mcp.add')}
        </Button>
        <Button size="s" variant="text" onClick={() => setImporting('')}>
          {t('settings.mcp.import')}
        </Button>
        <Button size="s" variant="text" onClick={() => void tools.reload()}>
          {t('settings.mcp_test')}
        </Button>
      </div>
      <Sheet
        open={!!adding}
        title={adding?.mode === 'edit' ? t('settings.mcp.edit_title') : t('settings.mcp.add_title')}
        onClose={() => setAdding(null)}
        busy={busy}
        footer={
          <>
            <Button variant="text" onClick={() => setAdding(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="solid" disabled={!!err} loading={busy} onClick={async () => adding && (await save({ ...list, [adding.name]: draftServer(adding) }, t('settings.mcp.saved'))) && setAdding(null)}>
              {adding?.mode === 'edit' ? t('settings.mcp.save') : t('settings.mcp.add')}
            </Button>
          </>
        }
      >
        {adding && (
          <div class="stack">
            <label class="stack">
              <span class="label">{t('settings.mcp.name')}</span>
              <TextInput class="mono" value={adding.name} disabled={adding.mode === 'edit'} onChange={(v) => setAdding({ ...adding, name: v })} />
            </label>
            {adding.mode === 'add' && (
              <SegmentedControl label={t('settings.mcp.transport')} value={adding.transport} options={[{ value: 'stdio', label: 'stdio' }, { value: 'http', label: 'http' }]} onChange={(v) => setAdding({ ...adding, transport: v })} />
            )}
            <p class="field-help">{t('settings.mcp.keychain_note')}</p>
            {adding.transport === 'stdio' ? (
              <>
                <label class="stack">
                  <span class="label">{t('settings.mcp.command')}</span>
                  <TextInput class="mono" value={adding.command} placeholder="npx" onChange={(v) => setAdding({ ...adding, command: v })} />
                </label>
                <label class="stack">
                  <span class="label">{t('settings.mcp.args')}</span>
                  <TextArea class="mono" rows={3} value={adding.args} onChange={(v) => setAdding({ ...adding, args: v })} />
                </label>
                {adding.envRows.length > 0 && <span class="label">{t('settings.mcp.env_saved')}</span>}
                <SecretRows kind="env" rows={adding.envRows} onChange={(envRows) => setAdding({ ...adding, envRows })} />
                <label class="stack">
                  <span class="label">{adding.mode === 'edit' ? t('settings.mcp.env_add') : t('settings.mcp.env')}</span>
                  <TextArea class="mono" rows={3} value={adding.env} placeholder="KEY=value" onChange={(v) => setAdding({ ...adding, env: v })} />
                </label>
              </>
            ) : (
              <>
                <label class="stack">
                  <span class="label">{t('settings.mcp.url')}</span>
                  <TextInput class="mono" value={adding.url} placeholder="https://" onChange={(v) => setAdding({ ...adding, url: v })} />
                </label>
                {adding.headerRows.length > 0 && <span class="label">{t('settings.mcp.headers_saved')}</span>}
                <SecretRows kind="headers" rows={adding.headerRows} onChange={(headerRows) => setAdding({ ...adding, headerRows })} />
                <label class="stack">
                  <span class="label">{adding.mode === 'edit' ? t('settings.mcp.headers_add') : t('settings.mcp.headers')}</span>
                  <TextArea class="mono" rows={3} value={adding.headers} placeholder="Authorization: Bearer …" onChange={(v) => setAdding({ ...adding, headers: v })} />
                </label>
              </>
            )}
            {err && adding.name && <p class="field-error">{err}</p>}
          </div>
        )}
      </Sheet>
      <Sheet
        open={importing !== null}
        title={t('settings.mcp.import_title')}
        onClose={() => setImporting(null)}
        busy={busy}
        footer={
          <>
            <Button variant="text" onClick={() => setImporting(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="solid" disabled={!imported} loading={busy} onClick={async () => imported && (await save({ ...list, ...imported }, t('settings.mcp.imported', { n: Object.keys(imported).length }))) && setImporting(null)}>
              {t('settings.mcp.import')}
            </Button>
          </>
        }
      >
        <TextArea class="mono" rows={14} value={importing ?? ''} aria-label={t('settings.mcp.import_title')} onChange={setImporting} placeholder='{"mcpServers": {"weather": {"command": "npx", "args": ["weather-mcp"]}}}' />
        {importing && !imported && <p class="field-error">{t('settings.json_invalid')}</p>}
      </Sheet>
      <ConfirmSheet
        open={!!removing}
        title={t('settings.mcp.remove_title')}
        confirmLabel={t('settings.mcp.remove')}
        busy={busy}
        onClose={() => setRemoving(null)}
        onConfirm={async () => {
          if (!removing) return;
          const { [removing]: _, ...rest } = list;
          if (await save(rest, t('settings.mcp.saved'))) setRemoving(null);
        }}
      >
        <p class="mono">{removing}</p>
      </ConfirmSheet>
    </div>
  );
}
