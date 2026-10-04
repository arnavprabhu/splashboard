import { CopyButton } from '../components/CopyButton';
import { NumbersBand, Stat } from '../components/NumbersBand';
import { Section } from '../components/Section';
import { StatusChip } from '../components/StatusChip';
import { SubNav } from '../components/SubNav';
import { DASH, formatDuration } from '../lib/format';
import { engine } from '../store';
import { Placeholder } from './Placeholder';
import { STATUS_TABS } from './tabs';

export function endpoints(origin: string): Array<{ label: string; url: string }> {
  return [
    { label: 'OpenAI base', url: `${origin}/v1` },
    { label: 'Anthropic base', url: origin },
  ];
}

export default function StatusPage() {
  const e = engine.value;
  return (
    <>
      <SubNav items={STATUS_TABS} label="Status" exact />
      <Placeholder
        title={e?.model ? e.model.split('/').pop() ?? e.model : 'No model.'}
        spec="§10.3"
        meta={
          <span class="cluster" style={{ gap: '10px', alignItems: 'center' }}>
            <StatusChip state={e?.state ?? null} />
            <span>Uptime {e?.uptime_s != null ? formatDuration(e.uptime_s) : DASH}</span>
            <span>Splash {e?.engine_version ?? DASH}</span>
          </span>
        }
      >
        <NumbersBand label="Since engine start">
          <Stat label="Tokens processed" value={DASH} />
          <Stat label="Cached tokens" value={DASH} />
          <Stat label="Decode" value={DASH} unit="tok/s" />
          <Stat label="Prefill" value={DASH} unit="tok/s" />
        </NumbersBand>
        <Section label="Endpoints">
          <ul class="list" style={{ borderBottom: 0 }}>
            {endpoints(location.origin).map((ep) => (
              <li key={ep.label} class="cluster rule-minor" style={{ padding: '10px 0', justifyContent: 'space-between' }}>
                <span class="label">{ep.label}</span>
                <code class="mono" style={{ flex: '1 1 240px' }}>
                  {ep.url}
                </code>
                <CopyButton text={ep.url} what={ep.label} />
              </li>
            ))}
          </ul>
        </Section>
      </Placeholder>
    </>
  );
}
