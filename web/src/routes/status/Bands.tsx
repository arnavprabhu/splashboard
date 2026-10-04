/**
 * The lower Status bands (docs/ui/02 §8–9): Memory, Cache, Scheduler & admission, Latency and
 * Claude Code. Field names are Splash's `/status` schema 6 (runtime/engine/Status.cpp,
 * server/latency.py). Loaded lazily: they sit below the fold (SPEC §18.6 budget).
 */
import type { ComponentChildren } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import { Banner } from '../../components/Banner';
import { CodeBlock } from '../../components/CodeBlock';
import { copyText } from '../../components/CopyButton';
import { toast, toastError } from '../../components/Toast';
import { Disclosure } from '../../components/Disclosure';
import { Select } from '../../components/inputs';
import { KeyValue, MeterBar, type KeyValueItem } from '../../components/KeyValue';
import { Section } from '../../components/Section';
import { Table } from '../../components/Table';
import { DASH, formatBytes, formatCount, formatMs, formatPercent } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { engine, settings } from '../../store';
import { loadSystem, systemInfo } from '../../store/live';
import { t } from '../../strings/bands';
import { fetchApiKey, modelProfiles } from './api';
import {
  FAILURE_FIELDS,
  STAGES,
  checkpointEntries,
  claudeEnv,
  launchCommand,
  rawGet,
  rawNum,
  stageBuckets,
  stageMeanMs,
  stagePercentileMs,
  type Stage,
} from './logic';

const bytes = (v: number | null) => (v === null ? DASH : formatBytes(v));
const count = (v: number | null) => (v === null ? DASH : formatCount(v));

export interface BandsProps {
  raw: Record<string, unknown> | null;
  /** The engine is not running: bands collapse to their labels (docs/ui/02 §10). */
  stopped: boolean;
}

function Stopped({ label }: { label: string }) {
  return (
    <Section label={label}>
      <p class="meta">{t('bands.bands.stopped')}</p>
    </Section>
  );
}

function groupMissing(raw: unknown, group: string): boolean {
  return raw !== null && rawGet(raw, group) === undefined;
}

// ---------- §8.1 Memory ----------

export function MemoryBand({ raw, stopped }: BandsProps) {
  useEffect(() => {
    void loadSystem();
  }, []);
  if (stopped) return <Stopped label={t('bands.memory.label')} />;
  const charged = rawNum(raw, 'memory_governor.charged_bytes');
  const limit = rawNum(raw, 'memory_governor.limit_bytes');
  const pressure = rawGet(raw, 'memory_governor.system_pressure');
  const growth = rawGet(raw, 'memory_governor.growth_allowed');
  const healthy = rawGet(raw, 'metal.healthy');
  const ram = systemInfo.value?.memory_bytes ?? null;
  const items: KeyValueItem[] = [
    {
      key: 'charged',
      label: t('bands.memory.charged'),
      value:
        charged !== null && limit !== null
          ? `${t('bands.memory.charged_v', { c: formatBytes(charged), l: formatBytes(limit) })} · ${formatPercent(limit > 0 ? charged / limit : null)}`
          : null,
    },
    {
      key: 'current',
      label: t('bands.memory.current'),
      value:
        rawNum(raw, 'memory_actual.current_bytes') === null
          ? null
          : `${bytes(rawNum(raw, 'memory_actual.current_bytes'))} · ${bytes(rawNum(raw, 'memory_actual.peak_bytes'))}`,
      meta: t('bands.memory.allocated', { v: bytes(rawNum(raw, 'memory_actual.allocated_bytes')) }),
    },
    { key: 'headroom', label: t('bands.memory.headroom'), value: rawNum(raw, 'memory_governor.headroom_bytes') === null ? null : bytes(rawNum(raw, 'memory_governor.headroom_bytes')) },
    {
      key: 'host',
      label: t('bands.memory.host'),
      value:
        rawNum(raw, 'memory_governor.host_available_bytes') === null
          ? null
          : `${bytes(rawNum(raw, 'memory_governor.host_available_bytes'))} · ${bytes(rawNum(raw, 'memory_governor.host_reserve_bytes'))}`,
      meta: t('bands.memory.host_of', { ram: ram === null ? DASH : formatBytes(ram), v: bytes(rawNum(raw, 'memory_governor.host_headroom_bytes')) }),
    },
    {
      key: 'pressure',
      label: t('bands.memory.pressure'),
      value:
        typeof pressure === 'string'
          ? t(`bands.memory.pressure.${pressure as 'normal'}`) || pressure
          : null,
      accent: pressure === 'critical',
      meta: typeof rawGet(raw, 'memory_pressure') === 'string' ? t('bands.memory.top_pressure', { v: String(rawGet(raw, 'memory_pressure')) }) : undefined,
    },
    {
      key: 'growth',
      label: t('bands.memory.growth'),
      value: typeof growth === 'boolean' ? t(growth ? 'bands.memory.growth.allowed' : 'bands.memory.growth.denied') : null,
      meta: rawNum(raw, 'memory_governor.denied_reservations') !== null ? t('bands.memory.denied', { n: rawNum(raw, 'memory_governor.denied_reservations') ?? 0 }) : undefined,
    },
    {
      key: 'metal',
      label: t('bands.memory.metal'),
      value:
        typeof healthy === 'boolean'
          ? healthy
            ? t('bands.memory.metal_ok')
            : t('bands.memory.metal_failed', { reason: String(rawGet(raw, 'metal.failure_reason') ?? '') })
          : null,
      accent: healthy === false,
    },
  ];
  return (
    <Section label={t('bands.memory.label')} id="memory">
      <div class="stack">
        {groupMissing(raw, 'memory_governor') ? (
          <p class="meta">{t('bands.bands.not_reported')}</p>
        ) : (
          <MeterBar
            label={t('bands.memory.meter')}
            segments={[
              { label: t('bands.memory.charged'), value: charged },
              { label: t('bands.memory.headroom'), value: limit !== null && charged !== null ? Math.max(0, limit - charged) : null },
            ]}
            total={limit}
            valueText={charged !== null && limit !== null ? t('bands.memory.charged_v', { c: formatBytes(charged), l: formatBytes(limit) }) : undefined}
          />
        )}
        <KeyValue items={items} label={t('bands.memory.label')} />
        <p class="meta">{t('bands.memory.explainer')}</p>
      </div>
    </Section>
  );
}

// ---------- §8.2 Cache ----------

export function CacheBand({ raw, stopped }: BandsProps) {
  if (stopped) return <Stopped label={t('bands.cache.label')} />;
  const serve = (settings.value?.settings.global?.serve ?? {}) as { max_cache_disk?: string | number; persistent_cache?: boolean };
  const storage = (settings.value?.settings.global?.storage ?? {}) as { cache_dir?: string | null };
  const capacity = rawNum(raw, 'disk.capacity_bytes');
  const diskOn = (capacity !== null && capacity > 0) || (serve.max_cache_disk !== undefined && String(serve.max_cache_disk) !== '0');
  const persistent = rawGet(raw, 'disk.persistent') === true;
  const checkpoints = checkpointEntries(rawGet(raw, 'state'))
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${k.endsWith('bytes') ? formatBytes(v) : formatCount(v)}`)
    .join(' · ');
  const failures = FAILURE_FIELDS.map(([name, path]) => ({ name, value: rawNum(raw, path) }));
  const anyFailure = failures.some((f) => (f.value ?? 0) > 0);
  const settingsLink = (
    <Link href="/settings/cache" class="mono-link">
      {t('bands.cache.settings')} ↗
    </Link>
  );
  const ramLines: ComponentChildren[] = [
    t('bands.cache.pages', {
      a: count(rawNum(raw, 'kv.pages_active')),
      c: count(rawNum(raw, 'kv.pages_cache')),
      f: count(rawNum(raw, 'kv.pages_free')),
      t: count(rawNum(raw, 'kv.pages_allocated')),
    }),
    t('bands.cache.kv_bytes', { b: bytes(rawNum(raw, 'kv.allocated_bytes')), r: bytes(rawNum(raw, 'kv.reclaimable_bytes')) }),
    t('bands.cache.states', { n: count(rawNum(raw, 'state.entries')), u: count(rawNum(raw, 'state.in_use')), b: bytes(rawNum(raw, 'state.bytes')) }),
    checkpoints ? t('bands.cache.checkpoints', { list: checkpoints }) : null,
    t('bands.cache.evictions', { n: count(rawNum(raw, 'state.in_use_evictions')) }),
  ];
  const used = rawNum(raw, 'disk.used_bytes');
  return (
    <Section label={t('bands.cache.label')} id="cache">
      <KeyValue
        label={t('bands.cache.label')}
        items={[
          {
            key: 'ram',
            label: t('bands.cache.ram'),
            value: (
              <span class="band-lines">
                {ramLines.filter(Boolean).map((l, i) => (
                  <span key={i}>{l}</span>
                ))}
              </span>
            ),
          },
          {
            key: 'ssd',
            label: t('bands.cache.ssd'),
            value: diskOn ? (
              <span class="band-lines">
                <MeterBar
                  label={t('bands.cache.ssd_meter')}
                  segments={[{ label: t('bands.cache.ssd'), value: used }]}
                  total={capacity}
                  valueText={t('bands.cache.ssd_used', { u: bytes(used), c: bytes(capacity) })}
                />
                <span>
                  {t('bands.cache.ssd_used', { u: bytes(used), c: bytes(capacity) })} · {t('bands.cache.on_disk', { v: bytes(rawNum(raw, 'disk.file_bytes')) })}
                </span>
                <span>{t('bands.cache.kv_blocks', { n: count(rawNum(raw, 'disk.kv_blocks')), b: bytes(rawNum(raw, 'disk.kv_bytes')) })}</span>
                <span>
                  {t('bands.cache.demotions', {
                    n: count(rawNum(raw, 'disk.kv_demotions')),
                    r: count(rawNum(raw, 'disk.kv_demotions_refused')),
                    p: count(rawNum(raw, 'disk.kv_pending_pages')),
                  })}{' '}
                  · {t('bands.cache.restores', { n: count(rawNum(raw, 'disk.kv_restores')) })}
                </span>
                <span>
                  {t('bands.cache.disk_hits', {
                    h: count(rawNum(raw, 'state.disk_hits')),
                    p: count(rawNum(raw, 'state.disk_promotions')),
                    o: count(rawNum(raw, 'state.offloads')),
                  })}
                </span>
                <span>{t('bands.cache.wear', { r: bytes(rawNum(raw, 'disk.read_bytes')), w: bytes(rawNum(raw, 'disk.written_bytes')) })}</span>
              </span>
            ) : (
              <span>
                {t('bands.cache.off')} · {settingsLink}
              </span>
            ),
          },
          {
            key: 'persistent',
            label: t('bands.cache.persistent'),
            value: persistent ? (
              <span class="band-lines">
                <span class="mono">{t('bands.cache.persistent_on', { dir: storage.cache_dir ?? '~/.splash/cache' })}</span>
                <span>
                  {t('bands.cache.taken_back', {
                    s: count(rawNum(raw, 'disk.taken_back.states')),
                    k: count(rawNum(raw, 'disk.taken_back.kv_blocks')),
                    b: bytes(rawNum(raw, 'disk.taken_back.bytes')),
                    l: count(rawNum(raw, 'disk.taken_back.left_behind')),
                  })}
                </span>
                <span>
                  {t('bands.cache.write_behind', {
                    w: count(rawNum(raw, 'disk.write_behind.waiting')),
                    d: count(rawNum(raw, 'disk.write_behind.durable')),
                    u: count(rawNum(raw, 'disk.write_behind.unneeded')),
                    r: count(rawNum(raw, 'disk.write_behind.refused')),
                  })}
                </span>
              </span>
            ) : (
              <span>
                {t('bands.cache.persistent_off')} · {settingsLink}
              </span>
            ),
          },
          {
            key: 'failures',
            label: t('bands.cache.failures'),
            value: (
              <span class={anyFailure ? undefined : 'mute'} data-testid="cache-failures">
                {failures.map((f, i) => (
                  <span key={f.name}>
                    {i > 0 && ' · '}
                    {t(`bands.cache.f.${f.name as 'copies'}`)}{' '}
                    <span class={(f.value ?? 0) > 0 ? 'acc' : undefined} data-accent={(f.value ?? 0) > 0 ? 'true' : undefined}>
                      {count(f.value)}
                    </span>
                  </span>
                ))}
              </span>
            ),
          },
        ]}
      />
    </Section>
  );
}

// ---------- §8.3 Scheduler and admission ----------

export function SchedulerBand({ raw, stopped }: BandsProps) {
  if (stopped) return <Stopped label={t('bands.sched.label')} />;
  const n = (p: string) => count(rawNum(raw, p));
  const oldest = rawNum(raw, 'admission.oldest_wait_ms');
  const widths = (['b1', 'b2', 'b3', 'b4'] as const).map((w) => [w, rawNum(raw, `scheduler.decode_batches_by_width.${w}`)] as const);
  const maxWidth = Math.max(1, ...widths.map(([, v]) => v ?? 0));
  const list = widths.map(([w, v]) => `${w} ${count(v)}`).join(' · ');
  return (
    <Section label={t('bands.sched.label')} id="scheduler">
      <KeyValue
        label={t('bands.sched.label')}
        items={[
          {
            key: 'waiting',
            label: t('bands.sched.waiting'),
            value: t('bands.sched.waiting_v', {
              t: n('admission.waiting'),
              m: n('admission.waiting_memory'),
              c: n('admission.waiting_concurrency'),
              p: n('scheduler.waiting_prefix'),
              g: n('scheduler.waiting_mask'),
            }),
          },
          {
            key: 'held',
            label: t('bands.sched.held'),
            value: t('bands.sched.held_v', {
              h: n('admission.held_behind_refusal'),
              r: n('admission.restoring'),
              s: n('admission.suspended'),
              d: rawGet(raw, 'admission.draining') === true ? t('common.yes') : rawGet(raw, 'admission.draining') === false ? t('common.no') : DASH,
            }),
          },
          { key: 'oldest', label: t('bands.sched.oldest'), value: oldest === null ? null : formatMs(oldest), accent: oldest !== null && oldest > 10_000 },
          {
            key: 'suspensions',
            label: t('bands.sched.suspensions'),
            value: t('bands.sched.suspensions_v', {
              r: n('cache.resource_suspensions'),
              p: n('cache.priority_suspensions'),
              m: n('cache.resource_resumptions'),
            }),
          },
          {
            key: 'lanes',
            label: t('bands.sched.lanes'),
            value: t('bands.sched.lanes_v', {
              p: n('scheduler.prefilling'),
              d: n('scheduler.decoding'),
              q: n('scheduler.queued'),
              w: n('scheduler.waiting_resources'),
            }),
          },
          {
            key: 'batches',
            label: t('bands.sched.batches'),
            value: (
              <span class="cluster batches">
                <span class="tnum">{list}</span>
                <span class="histogram" role="img" aria-label={t('bands.sched.batches_aria', { list })}>
                  {widths.map(([w, v]) => (
                    <span key={w} class="histogram-bar" style={{ height: `${Math.round(((v ?? 0) / maxWidth) * 100)}%` }} />
                  ))}
                </span>
              </span>
            ),
          },
          { key: 'loop', label: t('bands.sched.loop'), value: rawNum(raw, 'loop.max_tick_ms') === null ? null : t('bands.sched.loop_v', { v: formatMs(rawNum(raw, 'loop.max_tick_ms')) }) },
        ]}
      />
    </Section>
  );
}

// ---------- §8.4 Latency ----------

interface StageRow {
  stage: Stage;
  p50: number | null;
  p95: number | null;
  mean: number | null;
  count: number;
}

export function LatencyBand({ raw, stopped }: BandsProps) {
  const [rawStage, setRawStage] = useState<Stage>('http_ttft');
  if (stopped) return <Stopped label={t('bands.latency.label')} />;
  const latency = rawGet(raw, 'latency');
  if (raw !== null && (latency === undefined || latency === null))
    return (
      <Section label={t('bands.latency.label')} id="latency">
        <p class="body">{t('bands.latency.missing')}</p>
      </Section>
    );
  const rows: StageRow[] = STAGES.map((stage) => {
    const h = rawGet(raw, `latency.${stage}`);
    const c = rawNum(raw, `latency.${stage}.count`) ?? 0;
    return { stage, p50: stagePercentileMs(h, 0.5), p95: stagePercentileMs(h, 0.95), mean: stageMeanMs(h), count: c };
  });
  const approx = (v: number | null, c: number) => (c === 0 || v === null ? DASH : t('bands.latency.approx', { v: formatMs(v) }));
  const buckets = stageBuckets(rawGet(raw, `latency.${rawStage}`));
  return (
    <Section label={t('bands.latency.label')} id="latency" meta={t('bands.latency.note')}>
      <div class="stack">
        <Table
          caption={t('bands.latency.caption')}
          rowKey={(r) => r.stage}
          rows={rows}
          columns={[
            {
              key: 'stage',
              label: t('bands.latency.stage'),
              render: (r) => <span class={r.stage === 'http_ttft' ? 'strong' : undefined}>{t(`bands.latency.s.${r.stage}`)}</span>,
            },
            { key: 'p50', label: t('bands.latency.p50'), align: 'right', render: (r) => approx(r.p50, r.count) },
            { key: 'p95', label: t('bands.latency.p95'), align: 'right', render: (r) => approx(r.p95, r.count) },
            { key: 'mean', label: t('bands.latency.mean'), align: 'right', render: (r) => (r.count === 0 ? DASH : formatMs(r.mean)) },
            { key: 'count', label: t('bands.latency.count'), align: 'right', render: (r) => (r.count === 0 ? DASH : formatCount(r.count)) },
          ]}
        />
        <Disclosure summary={t('bands.latency.raw')}>
          <div class="stack">
            <label class="cluster">
              <span class="label">{t('bands.latency.raw_stage')}</span>
              <Select value={rawStage} options={STAGES.map((s) => ({ value: s, label: t(`bands.latency.s.${s}`) }))} onChange={(v) => setRawStage(v as Stage)} />
            </label>
            <Table
              caption={t('bands.latency.raw')}
              rowKey={(r) => r[0]}
              rows={buckets}
              columns={[
                { key: 'bound', label: t('bands.latency.bound'), render: (r) => <span class="mono">{r[0]}</span> },
                { key: 'cum', label: t('bands.latency.cumulative'), align: 'right', render: (r) => formatCount(r[1]) },
              ]}
            />
          </div>
        </Disclosure>
      </div>
    </Section>
  );
}

// ---------- §9 Claude Code ----------

/** A revealed key re-masks after 30 s, as in Settings → Security (docs/ui/05 decision G5). */
export const CLAUDE_KEY_REMASK_MS = 30_000;

export function ClaudeBand({ models }: { models: readonly string[] }) {
  const e = engine.value;
  const active = e?.model ?? null;
  const g = settings.value?.settings.global;
  const keyRequired = (g?.security as { api_key_required?: boolean } | undefined)?.api_key_required === true;
  const aliases = ((settings.value?.settings.models?.[active ?? ''] as { serve?: { served_model_names?: string[] } } | undefined)?.serve?.served_model_names ?? []) as string[];
  const profiles = useApi((s) => modelProfiles(active!, s), [active], !!active);
  const [pick, setPick] = useState<string>('');
  const [key, setKey] = useState<string | null>(null);
  useEffect(() => {
    if (key === null) return;
    const id = setTimeout(() => setKey(null), CLAUDE_KEY_REMASK_MS);
    return () => clearTimeout(id);
  }, [key]);
  const options = useMemo(() => {
    const out: { value: string; label: string }[] = [];
    if (active) {
      out.push({ value: active, label: active });
      for (const a of aliases) out.push({ value: a, label: t('bands.claude.alias', { id: a }) });
      for (const p of profiles.data?.profiles ?? []) if (p.name !== 'default') out.push({ value: `${active}:${p.name}`, label: `${active}:${p.name}` });
    } else {
      for (const m of models) out.push({ value: m, label: m });
    }
    return out;
  }, [active, aliases.join('|'), profiles.data, models.join('|')]);
  const model = pick || active || options[0]?.value || '';
  const command = launchCommand(model || null, active);
  const env = claudeEnv({
    baseUrl: location.origin,
    model: model || '<model>',
    key: keyRequired ? (key ?? '••••') : null,
    context: e?.maximum_context_tokens ?? null,
  });
  return (
    <Section label={t('bands.claude.label')} id="claude">
      <div class="stack">
        {!active && <Banner tone="info">{t('bands.claude.stopped')}</Banner>}
        {options.length > 0 ? (
          <label class="cluster">
            <span class="label">{t('bands.claude.model')}</span>
            <Select value={model} options={options} onChange={(v) => setPick(v === active ? '' : v)} />
          </label>
        ) : (
          <p class="meta">{t('bands.claude.no_model')}</p>
        )}
        <CodeBlock code={command} label={t('bands.claude.command')} what={t('bands.claude.command')} />
        <Disclosure summary={t('bands.claude.env')} open>
          <CodeBlock code={env} what={t('bands.claude.env')} copy={!keyRequired || key !== null} />
          {keyRequired && (
            <span class="cluster">
              <button
                type="button"
                class="btn"
                data-variant="text"
                data-size="s"
                onClick={async () => {
                  if (key !== null) return setKey(null);
                  try {
                    setKey(await fetchApiKey());
                  } catch (err) {
                    toastError(t('bands.claude.key_failed'), err);
                  }
                }}
              >
                {key === null ? t('bands.claude.reveal') : t('bands.claude.hide')}
              </button>
              {key === null && (
                <button
                  type="button"
                  class="btn"
                  data-variant="text"
                  data-size="s"
                  onClick={async () => {
                    try {
                      const real = await fetchApiKey();
                      const text = claudeEnv({ baseUrl: location.origin, model: model || '<model>', key: real, context: e?.maximum_context_tokens ?? null });
                      if (await copyText(text)) toast(t('bands.claude.copied'));
                    } catch (err) {
                      toastError(t('bands.claude.key_failed'), err);
                    }
                  }}
                >
                  {t('bands.claude.copy_env')}
                </button>
              )}
            </span>
          )}
        </Disclosure>
        <p class="meta">
          {t('bands.claude.footer')} <Link href="/integrations">{t('bands.claude.integrations')} ↗</Link> {t('bands.claude.integrations_more')}
        </p>
      </div>
    </Section>
  );
}

/** Everything below the charts, as one lazily loaded chunk. */
export default function LowerBands({ raw, stopped, models }: BandsProps & { models: readonly string[] }) {
  return (
    <>
      <MemoryBand raw={raw} stopped={stopped} />
      <CacheBand raw={raw} stopped={stopped} />
      <SchedulerBand raw={raw} stopped={stopped} />
      <LatencyBand raw={raw} stopped={stopped} />
      <ClaudeBand models={models} />
    </>
  );
}
