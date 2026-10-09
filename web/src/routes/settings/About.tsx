/**
 * Settings → About (docs/ui/05 §3.17): versions and update checks, this Mac, links and
 * licences, and setup actions (re-run the wizard, run the doctor).
 */
import { useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { DoctorReport, UpdateInfo } from '../../api/models';
import { Button, Disclosure, ExternalLink, KeyValue, LabelRow, LoadError, Loading, toast, toastError } from '../../components';
import { DASH, formatBytes, formatRelativeTime } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { t } from '../../strings/settings';
import { settingsApi } from './api';
import { RemoveData } from './RemoveData';

export const LINKS = {
  /** Splashboard itself (D40; public since D103). */
  guiRepo: 'https://github.com/arnavprabhu/splashboard',
  guiIssues: 'https://github.com/arnavprabhu/splashboard/issues',
  /** The engine. */
  repo: 'https://github.com/incoai/splash',
  issue: 'https://github.com/incoai/splash/issues/new',
} as const;

const GLYPH: Record<DoctorReport['checks'][number]['status'], string> = { ok: '✓', warn: '!', fail: '✕', skip: '–' };

export function updateLine(u: UpdateInfo | null | undefined): string {
  if (!u) return t('settings.about.not_checked');
  if (u.available && u.version) return t('settings.about.available', { version: u.version });
  if (u.checked_at) return t('settings.about.up_to_date', { when: formatRelativeTime(u.checked_at) });
  return t('settings.about.not_checked');
}

export function About() {
  const versions = useApi(settingsApi.versions);
  const system = useApi(settingsApi.system);
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [appHint, setAppHint] = useState(false);
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [doctorError, setDoctorError] = useState<unknown>(null);
  const [doctorBusy, setDoctorBusy] = useState(false);
  const [upgrading, setUpgrading] = useState(false);

  const v = versions.data;
  const engineUpdate = update ?? v?.engine_update ?? null;
  const e = v?.engine;
  const s = system.data;

  async function checkEngine() {
    setChecking(true);
    try {
      setUpdate(await settingsApi.checkEngineUpdate());
    } catch (err) {
      toastError(t('settings.about.check_failed'), err);
    } finally {
      setChecking(false);
    }
  }
  async function checkApp() {
    try {
      await settingsApi.checkAppUpdate();
      toast(t('settings.about.app_checking'));
    } catch {
      setAppHint(true);
    }
  }
  async function runDoctor() {
    setDoctorBusy(true);
    setDoctorError(null);
    try {
      setDoctor(await settingsApi.doctor());
    } catch (err) {
      setDoctorError(err);
    } finally {
      setDoctorBusy(false);
    }
  }
  async function upgrade() {
    setUpgrading(true);
    try {
      await settingsApi.upgradeEngine();
      toast(t('settings.about.upgrade_started'));
    } catch (err) {
      toastError(t('settings.about.upgrade_failed'), err);
    } finally {
      setUpgrading(false);
    }
  }

  return (
    <div class="about stack">
      <LabelRow label={t('settings.about.app')}>
        {versions.error ? (
          <LoadError thing={t('settings.about.thing_versions')} error={versions.error} onRetry={versions.reload} />
        ) : !v ? (
          <Loading />
        ) : (
          <div class="stack">
            <p class="tnum">{t('settings.about.app_versions', { gui: v.gui, manager: v.manager, python: v.python })}</p>
            <div class="cluster">
              <Button size="s" onClick={() => void checkApp()}>
                {t('settings.about.check')}
              </Button>
              {appHint && <span class="meta">{t('settings.about.app_check_hint')}</span>}
            </div>
          </div>
        )}
      </LabelRow>
      <LabelRow label={t('settings.about.engine')}>
        {!v ? null : (
          <div class="stack">
            <p class="tnum">
              {e?.found
                ? t('settings.about.engine_line', {
                    version: e.version ?? DASH,
                    source: e.cli ?? e.source ?? DASH,
                    schema: v.status_schema_version ?? DASH,
                  })
                : t('settings.about.engine_missing')}
            </p>
            <div class="cluster">
              <span class="meta" role="status">
                {updateLine(engineUpdate)}
              </span>
              <Button size="s" loading={checking} onClick={() => void checkEngine()}>
                {t('settings.about.engine_check')}
              </Button>
              {engineUpdate?.available && (
                <Button size="s" variant="solid" loading={upgrading} onClick={() => void upgrade()}>
                  {t('settings.about.upgrade')}
                </Button>
              )}
            </div>
            {engineUpdate?.available && engineUpdate.release_notes_md && (
              <Disclosure summary={t('settings.about.release_notes')}>
                <pre class="mono about-notes">{engineUpdate.release_notes_md}</pre>
              </Disclosure>
            )}
          </div>
        )}
      </LabelRow>
      <LabelRow label={t('settings.about.mac')}>
        {system.error ? (
          <LoadError thing={t('settings.about.thing_system')} error={system.error} onRetry={system.reload} />
        ) : !s ? (
          <Loading />
        ) : (
          <p class="tnum">
            {t('settings.about.mac_line', {
              chip: s.chip ?? DASH,
              cpu: s.cpu_cores ?? DASH,
              gpu: s.gpu_cores ?? DASH,
              ram: formatBytes(s.memory_bytes),
              macos: s.macos_version,
            })}
          </p>
        )}
      </LabelRow>
      <LabelRow label={t('settings.about.links')}>
        <div class="stack">
          <p class="cluster" data-testid="about-gui-links">
            <span class="meta">{t('settings.about.app')}</span>
            <ExternalLink href={LINKS.guiRepo}>{t('settings.about.gui_repo')}</ExternalLink>
            <ExternalLink href={LINKS.guiIssues}>{t('settings.about.gui_issues')}</ExternalLink>
          </p>
          <p class="cluster">
            <span class="meta">{t('settings.about.engine')}</span>
            <ExternalLink href={LINKS.repo}>{t('settings.about.repo')}</ExternalLink>
            <ExternalLink href={LINKS.issue}>{t('settings.about.issue')}</ExternalLink>
          </p>
          <div id="licenses">
          <Disclosure summary={t('settings.about.licenses')} open={location.hash === '#licenses'}>
            <ul class="about-licenses">
              <li>{t('settings.about.license_app')}</li>
              <li>{t('settings.about.license_engine')}</li>
              <li>{t('settings.about.license_font')}</li>
              <li>{t('settings.about.license_libs')}</li>
            </ul>
          </Disclosure>
          </div>
        </div>
      </LabelRow>
      <LabelRow label={t('settings.about.setup')}>
        <div class="stack">
          <div class="cluster">
            <Link href="/welcome?step=1" class="btn" data-variant="outline" data-size="s">
              {t('settings.about.rerun_wizard')}
            </Link>
            <Button size="s" loading={doctorBusy} onClick={() => void runDoctor()}>
              {t('settings.about.run_doctor')}
            </Button>
            <Link href="/logs/diagnostics" class="btn" data-variant="text" data-size="s">
              {t('settings.about.diagnostics')}
            </Link>
            <RemoveData />
          </div>
          {!!doctorError && <LoadError thing={t('settings.about.doctor_thing')} error={doctorError} onRetry={() => void runDoctor()} />}
          {doctor && (
            <KeyValue
              label={t('settings.about.run_doctor')}
              items={doctor.checks.map((c) => ({
                key: c.id,
                label: c.label,
                value: `${GLYPH[c.status]} ${c.message}`,
                meta: c.fix ?? undefined,
                accent: c.status === 'fail',
              }))}
            />
          )}
        </div>
      </LabelRow>
    </div>
  );
}
