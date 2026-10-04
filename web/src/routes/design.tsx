import { useMemo, useState } from 'preact/hooks';
import type { Alert, EngineState } from '../api/types';
import { ENGINE_STATES } from '../api/types';
import {
  AlertBand,
  Banner,
  Button,
  Chart,
  CopyButton,
  Disclosure,
  Empty,
  ExternalLink,
  Field,
  List,
  ListRow,
  Loading,
  NumberInput,
  NumbersBand,
  PageHeader,
  ProgressBar,
  Section,
  Select,
  Sheet,
  SizeInput,
  Stat,
  StatusChip,
  StickySaveBar,
  Table,
  TagList,
  TextArea,
  TextInput,
  Toggle,
} from '../components';
import { formatBytes, formatIndex, formatTokPerSec } from '../lib/format';

const TOKENS = ['--bg', '--ink', '--mute', '--rule', '--acc', '--on-acc'] as const;

const SAMPLE_ALERTS: Alert[] = [
  {
    id: 'queue_full',
    severity: 'warn',
    message: 'Queue full (32) — raise Queue size',
    count: 3,
    actions: [{ id: 'settings', label: 'Requests settings', method: 'GET', path: '/admin/settings/requests' }],
  },
  {
    id: 'engine_failed',
    severity: 'critical',
    title: 'Engine stopped.',
    message: 'Engine stopped after repeated failures',
    actions: [{ id: 'restart', label: 'Restart engine', method: 'POST', path: '/api/admin/engine/restart' }],
  },
  { id: 'write_behind_refused', severity: 'info', message: 'Persistent cache paused: hourly write cap (128 GiB/h) reached', actions: [] },
];

const SAMPLE_MODELS = [
  { id: 'mlx-community/Qwen3.8-27B-4bit', format: 'MLX', size: 15.6e9 },
  { id: 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL', format: 'GGUF', size: 21.2e9 },
];

function makeSeries(): [number[], number[], number[]] {
  const now = Math.floor(Date.now() / 1000);
  const xs: number[] = [];
  const a: number[] = [];
  const b: number[] = [];
  for (let i = 0; i < 120; i += 1) {
    xs.push(now - (120 - i) * 5);
    a.push(40 + 12 * Math.sin(i / 9) + (i % 7));
    b.push(28 + 6 * Math.cos(i / 13));
  }
  return [xs, a, b];
}

function Gallery({ theme }: { theme: 'light' | 'dark' }) {
  const [on, setOn] = useState(true);
  const [text, setText] = useState('127.0.0.1');
  const [num, setNum] = useState<number | null>(0.6);
  const [kv, setKv] = useState('int8');
  const [size, setSize] = useState('32G');
  const [tags, setTags] = useState(['qwen27']);
  const [json, setJson] = useState('{\n  "enable_thinking": false\n}');
  const [sheet, setSheet] = useState(false);
  const [changes, setChanges] = useState(2);
  const data = useMemo(makeSeries, []);

  return (
    <div data-theme={theme} aria-label={`${theme} theme`}>
      <PageHeader title={`${theme}.`} meta="Every component, DESIGN.md tokens" size="m" />

      <Section label="Tokens">
        <div class="swatches">
          {TOKENS.map((t) => (
            <div class="swatch" key={t}>
              <div class="swatch-color" style={{ background: `var(${t})` }} />
              <code class="meta flag">{t}</code>
            </div>
          ))}
        </div>
      </Section>

      <Section label="Type">
        <div class="stack">
          <p class="display-xl">Ready.</p>
          <p class="display-l">Models.</p>
          <p class="display-m">Qwen3.8</p>
          <p class="display-number tnum">42.7</p>
          <p class="heading">Heading</p>
          <p class="lead">One local engine, every Splash feature.</p>
          <p class="body">Body copy at 16–20px. Settings explain themselves and show the flag they map to.</p>
          <p class="label">01 — Label</p>
          <p class="nav">Nav link</p>
          <p class="meta">Meta · fine print</p>
        </div>
      </Section>

      <AlertBand alerts={SAMPLE_ALERTS} onDismiss={() => undefined} />

      <Section label="Status chip" meta="Accent only for live states">
        <div class="cluster">
          {ENGINE_STATES.map((s: EngineState) => (
            <StatusChip key={s} state={s} />
          ))}
          <StatusChip state={null} />
        </div>
      </Section>

      <Section label="Buttons">
        <div class="cluster">
          <Button>Outline</Button>
          <Button variant="solid">Solid ink</Button>
          <Button variant="accent">Accent</Button>
          <Button variant="text">Text</Button>
          <Button disabled>Disabled</Button>
          <Button size="s">Small</Button>
          <CopyButton text="http://127.0.0.1:8000/v1" />
          <ExternalLink href="https://github.com/incoai/splash" class="nav">
            Splash repo
          </ExternalLink>
        </div>
      </Section>

      <NumbersBand label="Numbers band" actions={<Button size="s" variant="text">Clear</Button>}>
        <Stat label="Decode" value={formatTokPerSec(42.71, { unit: false })} unit="tok/s" sub="p50 TTFT 312 ms" />
        <Stat label="Cache efficiency" value="87.5" unit="%" />
        <Stat label="Requests" value="1,204" sub="3 failed" />
        <Stat label="KV copy failures" value="2" accent title="Accent marks a value that needs attention" />
      </NumbersBand>

      <section class="band" aria-label="List rows">
        <h2 class="label" style={{ marginBottom: '16px' }}>
          List rows
        </h2>
        <List label="Installed models">
          {SAMPLE_MODELS.map((m, i) => (
            <ListRow
              key={m.id}
              name={m.id.split('/')[1] ?? m.id}
              label={`${formatIndex(i + 1)} — ${m.format} · ${formatBytes(m.size, { base: 1000 })}`}
              onSelect={() => undefined}
              detail={i === 0 ? <StatusChip state="ready" /> : undefined}
              nameClass="heading"
              actions={
                <>
                  <Button size="s" variant="text">
                    Load
                  </Button>
                  <Button size="s" variant="text">
                    Settings
                  </Button>
                  <Button size="s" variant="text">
                    Delete
                  </Button>
                </>
              }
            />
          ))}
        </List>
      </section>

      <Section label="Fields">
        <Field label="Host" help="Address the public port listens on." flag="--host" source="global" restart>
          {({ id, describedBy }) => <TextInput id={id} aria-describedby={describedBy} value={text} onChange={setText} />}
        </Field>
        <Field
          label="Temperature"
          help="Sampling temperature, 0–2."
          source="model"
          error={num !== null && (num < 0 || num > 2) ? 'Must be between 0 and 2' : null}
          more={<p>Injected by the proxy only when a request omits it (SPEC §7.5).</p>}
        >
          {({ id, describedBy }) => (
            <NumberInput id={id} aria-describedby={describedBy} value={num} onChange={setNum} min={0} max={2} step={0.05} />
          )}
        </Field>
        <Field label="KV format" flag="--kv-format" source="default" restart>
          {({ id }) => (
            <Select
              id={id}
              value={kv}
              onChange={setKv}
              options={[
                { value: 'int8', label: 'int8' },
                { value: 'fp16', label: 'fp16' },
              ]}
            />
          )}
        </Field>
        <Field label="SSD cache size" flag="--max-cache-disk" help="0 disables the disk tier." restart>
          {({ id }) => <SizeInput id={id} kind="max-cache-disk" value={size} onChange={setSize} />}
        </Field>
        <Field label="Served names" help="Extra names this model answers to.">
          {({ id }) => <TagList id={id} label="Add a served name" values={tags} onChange={setTags} placeholder="Add name, press Enter" />}
        </Field>
        <Field label="Persistent cache" flag="--persistent-cache" source="global">
          {({ id }) => <Toggle id={id} checked={on} onChange={setOn} />}
        </Field>
        <Field label="Template kwargs" help="JSON object passed as chat_template_kwargs.">
          {({ id }) => <TextArea id={id} value={json} onChange={setJson} spellcheck={false} />}
        </Field>
      </Section>

      <Section label="Banners">
        <div class="stack">
          <Banner title="Note">Local model scores, not calibrated confidence.</Banner>
          <Banner tone="critical" title="Clef" actions={<Button size="s">Details</Button>}>
            Splash scores with the backbone's output layer, not Clef's joint schema head.
          </Banner>
        </div>
      </Section>

      <Section label="Table">
        <Table
          caption="Requests"
          rowKey={(r) => r.id}
          columns={[
            { key: 'id', label: 'Request' },
            { key: 'endpoint', label: 'Endpoint' },
            { key: 'tokens', label: 'Tokens', align: 'right' },
            { key: 'ms', label: 'TTFT', align: 'right', render: (r) => `${r.ms} ms` },
          ]}
          rows={[
            { id: 'req_01', endpoint: '/v1/chat/completions', tokens: '12,288', ms: 312 },
            { id: 'req_02', endpoint: '/v1/messages', tokens: '2,048', ms: 98 },
          ]}
        />
      </Section>

      <Section label="Progress">
        <div class="stack">
          <ProgressBar value={0.42} label="Download" live valueText="42%" />
          <ProgressBar value={0.8} label="Disk used" />
          <ProgressBar value={null} label="Preparing" />
        </div>
      </Section>

      <Section label="Chart">
        <Chart
          title="Decode tok/s"
          summary="42.7 now"
          data={data}
          series={[{ label: 'Decode', primary: true }, { label: 'Prefill' }]}
          yFormat={(v) => v.toFixed(0)}
        />
      </Section>

      <Section label="Disclosure">
        <Disclosure summary="What this changes">
          <pre class="mono">ANTHROPIC_BASE_URL=http://127.0.0.1:8000</pre>
        </Disclosure>
      </Section>

      <Section label="Sheet">
        <Button onClick={() => setSheet(true)}>Open sheet</Button>
        <Sheet open={sheet} title="Model details" onClose={() => setSheet(false)} footer={<Button variant="solid" onClick={() => setSheet(false)}>Done</Button>}>
          <p class="body">Full-height sheet, 2px left rule, page dimmed with --bg at 80%. Esc closes.</p>
          <Field label="Revision" help="Pin a commit or branch.">
            {({ id }) => <TextInput id={id} value="main" onChange={() => undefined} />}
          </Field>
        </Sheet>
      </Section>

      <Section label="Empty and loading">
        <Empty title="No models yet" action={<Button variant="solid">Browse models</Button>}>
          Download a supported model to get started.
        </Empty>
        <Loading />
      </Section>

      <div style={{ position: 'relative' }}>
        <StickySaveBar changes={changes} restart onSave={() => setChanges(0)} onDiscard={() => setChanges(0)} />
      </div>
    </div>
  );
}

/** Hidden review route: /admin/_design. Every component in both themes. */
export default function DesignPage() {
  return (
    <div class="gallery-themes">
      <Gallery theme="light" />
      <Gallery theme="dark" />
    </div>
  );
}
