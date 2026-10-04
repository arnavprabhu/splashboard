/**
 * Settings → Chat & MCP servers (docs/ui/05 §3.11): a table with the live state from
 * `GET /mcp/tools`, an Add server sheet (stdio command or http URL) and Import mcp.json.
 * Saves immediately through `PUT /mcp/servers` (not the save bar).
 */
import { useState } from 'preact/hooks';
import type { McpServer } from '../../api/models';
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

type Servers = Record<string, McpServer>;

interface Draft {
  name: string;
  transport: 'stdio' | 'http';
  command: string;
  args: string;
  env: string;
  url: string;
  headers: string;
}

const EMPTY: Draft = { name: '', transport: 'stdio', command: '', args: '', env: '', url: '', headers: '' };

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
  if (existing.includes(d.name)) return t('settings.mcp.name_taken');
  if (d.transport === 'stdio' && !d.command.trim()) return t('settings.mcp.command_needed');
  if (d.transport === 'http' && !/^https?:\/\/\S+$/.test(d.url.trim())) return t('settings.mcp.url_rule');
  return null;
}

export function draftServer(d: Draft): McpServer {
  return d.transport === 'stdio'
    ? { command: d.command.trim(), args: d.args.split('\n').map((s) => s.trim()).filter(Boolean), env: parsePairs(d.env), enabled: true, always_allow: false }
    : { url: d.url.trim(), headers: parsePairs(d.headers, ':'), enabled: true, always_allow: false };
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
        ...(typeof s.command === 'string' ? { command: s.command, args: Array.isArray(s.args) ? s.args.map(String) : [], env: (s.env as Record<string, string>) ?? {} } : {}),
        ...(typeof s.url === 'string' ? { url: s.url, headers: (s.headers as Record<string, string>) ?? {} } : {}),
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
              render: ([name]) => (
                <Button size="s" variant="text" onClick={() => setRemoving(name)}>
                  {t('settings.mcp.remove')}
                </Button>
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
        title={t('settings.mcp.add_title')}
        onClose={() => setAdding(null)}
        busy={busy}
        footer={
          <>
            <Button variant="text" onClick={() => setAdding(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="solid" disabled={!!err} loading={busy} onClick={async () => adding && (await save({ ...list, [adding.name]: draftServer(adding) }, t('settings.mcp.saved'))) && setAdding(null)}>
              {t('settings.mcp.add')}
            </Button>
          </>
        }
      >
        {adding && (
          <div class="stack">
            <label class="stack">
              <span class="label">{t('settings.mcp.name')}</span>
              <TextInput class="mono" value={adding.name} onChange={(v) => setAdding({ ...adding, name: v })} />
            </label>
            <SegmentedControl label={t('settings.mcp.transport')} value={adding.transport} options={[{ value: 'stdio', label: 'stdio' }, { value: 'http', label: 'http' }]} onChange={(v) => setAdding({ ...adding, transport: v })} />
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
                <label class="stack">
                  <span class="label">{t('settings.mcp.env')}</span>
                  <TextArea class="mono" rows={3} value={adding.env} placeholder="KEY=value" onChange={(v) => setAdding({ ...adding, env: v })} />
                </label>
              </>
            ) : (
              <>
                <label class="stack">
                  <span class="label">{t('settings.mcp.url')}</span>
                  <TextInput class="mono" value={adding.url} placeholder="https://" onChange={(v) => setAdding({ ...adding, url: v })} />
                </label>
                <label class="stack">
                  <span class="label">{t('settings.mcp.headers')}</span>
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
