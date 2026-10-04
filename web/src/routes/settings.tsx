import { Link } from 'wouter-preact';
import { PageHeader, Section } from '../components/Section';
import { Empty } from '../components/States';
import { SETTINGS_SECTIONS } from './tabs';

export default function SettingsPage({ params }: { params?: { section?: string } }) {
  const current = SETTINGS_SECTIONS.find((s) => s.slug === params?.section) ?? SETTINGS_SECTIONS[0];
  return (
    <>
      <PageHeader title="Settings." />
      <section class="band">
        <div class="label-row">
          <nav class="label-row-label" aria-label="Settings sections">
            <ul class="stack" style={{ listStyle: 'none', margin: 0, padding: 0, gap: '8px' }}>
              {SETTINGS_SECTIONS.map((s) => (
                <li key={s.slug}>
                  <Link href={`/settings/${s.slug}`} class="navlink nav" aria-current={s.slug === current.slug ? 'page' : undefined}>
                    {s.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <div class="label-row-content">
            <h2 class="heading">{current.label}</h2>
            <Empty title="Not built yet">Specified in SPEC §10.9 and §8.2.</Empty>
          </div>
        </div>
      </section>
      <Section label="Reference" tight>
        <p class="meta">Settings model: SPEC §8</p>
      </Section>
    </>
  );
}
