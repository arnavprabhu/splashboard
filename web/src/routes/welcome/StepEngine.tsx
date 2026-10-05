/**
 * Step 1 — Engine (docs/ui/04 §2): this Mac, Homebrew, Splash (install with live output),
 * Splash version, shell hiders and permissions from the doctor.
 */

import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import type { BrewInfo, DoctorReport, JobEvent } from '../../api/models';
import { Banner, Button, CodeBlock, CopyButton, Disclosure, LoadError, LogPane, ProgressBar, toast, toastError, type LogPaneLine } from '../../components';
import { formatBytes } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { refreshEngine, useEvent } from '../../store';
import { loadVersions, versions } from '../../store/live';
import { t } from '../../strings/welcome';
import { getBrew, getDoctor, installEngine, notBuilt, upgradeEngine } from './api';
import { Glyph, StepLayout, useWizard } from './frame';
import { HOMEBREW_INSTALL_COMMAND, openHomebrewInstaller, openTerminal, SPLASH_INSTALL_COMMAND, SPLASH_UPGRADE_COMMAND } from './host';
import { brewStatus, doctorCheck, engineStepReady, macStatus, shellStatus, splashStatus, type CheckStatus } from './logic';
import { nextStep } from './steps';

export const BREW_POLL_MS = 2000;
/** docs/ui/04 §7: after 15 min without brew the row says "Still waiting". */
export const BREW_PATIENCE_MS = 15 * 60_000;

type InstallState =
  | { phase: 'idle' }
  | { phase: 'running'; kind: 'engine_install' | 'engine_upgrade' }
  | { phase: 'done' }
  | { phase: 'failed'; message: string | null }
  | { phase: 'unavailable'; command: string };

function CheckRow({ label, status, value, children, testId }: { label: string; status: CheckStatus; value: ComponentChildren; children?: ComponentChildren; testId: string }) {
  return (
    <div class="kv-row wz-check" data-status={status} data-testid={testId}>
      <dt class="label">{label}</dt>
      <dd class="wz-check-dd">
        <div class="wz-check-value">
          <span class="wz-check-text">{value}</span>
          <Glyph status={status} />
        </div>
        {children && <div class="wz-check-extra stack">{children}</div>}
      </dd>
    </div>
  );
}

export function StepEngine() {
  const { step, goTo, hosted, system } = useWizard();
  const brew = useApi((s) => getBrew(s), []);
  const doctor = useApi((s) => getDoctor(s), []);
  const [waitingSince, setWaitingSince] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [install, setInstall] = useState<InstallState>({ phase: 'idle' });
  const [lines, setLines] = useState<LogPaneLine[]>([]);
  const lineKey = useRef(0);

  useEffect(() => {
    void loadVersions(true);
  }, []);

  const brewInfo: BrewInfo | null = brew.data;
  const engine = versions.value?.engine ?? null;
  const mac = macStatus(system);
  const brewS = brewStatus(brewInfo);
  const splashS = splashStatus(engine);
  const splashFound = engine?.found === true;

  // Poll for Homebrew while it is missing (docs/ui/04 §2: every 2 s, no reload needed).
  useEffect(() => {
    if (!brewInfo || brewInfo.installed) {
      if (brewInfo?.installed && waitingSince !== null) {
        setWaitingSince(null);
        toast(t('welcome.engine.brew_found'));
      }
      return;
    }
    const timer = setInterval(() => {
      void brew.reload();
      setNow(Date.now());
    }, BREW_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brewInfo?.installed, waitingSince]);

  const onJob = (data: unknown) => {
    const ev = data as Partial<JobEvent> | null;
    if (!ev || install.phase !== 'running' || ev.kind !== install.kind) return;
    if (ev.line) {
      lineKey.current += 1;
      const key = lineKey.current;
      setLines((prev) => [...prev, { key, text: ev.line! }]);
    }
    if (ev.state === 'done') {
      setInstall({ phase: 'done' });
      void loadVersions(true);
      void refreshEngine();
      void doctor.reload();
    } else if (ev.state === 'failed') {
      setInstall({ phase: 'failed', message: ev.message ?? null });
    }
  };
  useEvent('job', onJob);
  useEvent('engine.upgrade', (data) => {
    const ev = data as { phase?: string; line?: string | null; ok?: boolean | null } | null;
    if (!ev || install.phase !== 'running' || install.kind !== 'engine_upgrade') return;
    onJob({ kind: 'engine_upgrade', line: ev.line ?? null, state: ev.phase === 'done' ? 'done' : ev.phase === 'failed' ? 'failed' : 'running', message: null });
  });

  const startInstall = async (kind: 'engine_install' | 'engine_upgrade') => {
    setLines([]);
    setInstall({ phase: 'running', kind });
    try {
      await (kind === 'engine_install' ? installEngine() : upgradeEngine());
    } catch (err) {
      if (notBuilt(err)) setInstall({ phase: 'unavailable', command: kind === 'engine_install' ? SPLASH_INSTALL_COMMAND : SPLASH_UPGRADE_COMMAND });
      else setInstall({ phase: 'failed', message: err instanceof Error ? err.message : String(err) });
    }
  };

  const installHomebrew = async () => {
    if (hosted) {
      try {
        await openHomebrewInstaller();
      } catch (err) {
        toastError(t('welcome.engine.terminal_failed'), err);
        return;
      }
    }
    setWaitingSince(Date.now());
    setNow(Date.now());
  };

  const recheck = () => {
    void brew.reload();
    void loadVersions(true);
    void doctor.reload();
    void refreshEngine();
  };

  const runInTerminal = async (command: string) => {
    try {
      await openTerminal(command);
    } catch (err) {
      toastError(t('welcome.engine.terminal_failed'), err);
    }
  };

  // Accent budget: the first blocking action holds the accent; Continue is outlined until ready.
  const needBrew = brewS === 'fail' && !splashFound;
  const needSplash = !splashFound && engine !== null;
  const ready = engineStepReady({ system, engine });
  const brewAccent = needBrew;
  const splashAccent = !needBrew && needSplash && install.phase !== 'running';
  const waiting = waitingSince !== null && brewS === 'fail';
  const stillWaiting = waiting && now - waitingSince! > BREW_PATIENCE_MS;

  const report: DoctorReport | null | 'unavailable' = doctor.data === null && !doctor.loading && !doctor.error ? 'unavailable' : doctor.data;
  // The manager's id is `path` (system/api.py doctor); `path_shadowing` was its old name in docs/api.md.
  const shadow = doctorCheck(report, 'path') ?? doctorCheck(report, 'path_shadowing');
  const perms = doctorCheck(report, 'permissions');

  const macValue = system
    ? t('welcome.engine.mac_value', { chip: system.chip ?? t('welcome.unknown_chip'), memory: formatBytes(system.memory_bytes, { digits: 0 }), macos: system.macos_version })
    : t('welcome.engine.checking');

  return (
    <StepLayout
      lead={t('welcome.engine.lead')}
      footer={{
        onContinue: () => goTo(nextStep(step)!),
        continueDisabled: !ready,
        continueVariant: ready ? 'accent' : 'outline',
      }}
    >
      <dl class="kv wz-checks" aria-label={t('welcome.engine.checks_label')}>
        <CheckRow label={t('welcome.engine.row.mac')} status={mac} value={macValue} testId="check-mac" />

        <CheckRow
          label={t('welcome.engine.row.brew')}
          status={brewS === 'fail' && splashFound ? 'warn' : brewS}
          testId="check-brew"
          value={
            brew.error ? (
              t('welcome.engine.brew_unknown')
            ) : brewInfo?.installed ? (
              <span class="mono">{[brewInfo.path, brewInfo.version].filter(Boolean).join(' · ')}</span>
            ) : brewInfo ? (
              splashFound ? t('welcome.engine.brew_not_needed') : t('welcome.engine.brew_missing')
            ) : (
              t('welcome.engine.checking')
            )
          }
        >
          {brew.error ? <LoadError thing={t('welcome.engine.row.brew')} error={brew.error} onRetry={() => void brew.reload()} /> : null}
          {brewInfo && !brewInfo.installed && (
            <>
              {waiting ? (
                <div class="stack wz-tight" aria-live="polite">
                  <p class="body">{stillWaiting ? t('welcome.engine.brew_still_waiting') : t('welcome.engine.brew_waiting')}</p>
                  <ProgressBar value={null} label={t('welcome.engine.brew_waiting')} />
                  {!hosted && <p class="body mute">{t('welcome.engine.brew_web_hint')}</p>}
                  {!hosted && <CodeBlock code={HOMEBREW_INSTALL_COMMAND} what={t('welcome.engine.brew_command')} wrap />}
                  <div class="cluster">
                    {hosted && (
                      <Button size="s" onClick={() => void installHomebrew()}>
                        {t('welcome.engine.brew_open_again')}
                      </Button>
                    )}
                    <Button size="s" variant="text" onClick={recheck}>
                      {t('welcome.engine.check_again')}
                    </Button>
                  </div>
                </div>
              ) : (
                <div class="cluster">
                  <Button variant={brewAccent ? 'accent' : 'outline'} onClick={() => void installHomebrew()} data-testid="install-brew">
                    {t('welcome.engine.brew_install')}
                  </Button>
                </div>
              )}
              {!waiting && (
                <Disclosure summary={t('welcome.engine.brew_yourself')}>
                  <p class="body mute">{t('welcome.engine.brew_yourself_body')}</p>
                  <CodeBlock code={HOMEBREW_INSTALL_COMMAND} what={t('welcome.engine.brew_command')} wrap />
                </Disclosure>
              )}
            </>
          )}
        </CheckRow>

        <CheckRow
          label={t('welcome.engine.row.splash')}
          status={install.phase === 'running' ? 'pending' : splashS}
          testId="check-splash"
          value={
            !engine ? (
              t('welcome.engine.checking')
            ) : engine.found ? (
              <span class="mono" data-testid="splash-version">
                {[engine.cli, engine.version].filter(Boolean).join(' · ')}
              </span>
            ) : (
              t('welcome.engine.splash_missing')
            )
          }
        >
          {engine?.found && engine.support === 'untested' && <p class="body">{t('welcome.engine.untested', { version: engine.version ?? '?' })}</p>}
          {engine?.found && engine.support === 'too_old' && (
            <>
              <p class="body">{t('welcome.engine.too_old', { version: engine.version ?? '?' })}</p>
              {install.phase !== 'running' && (
                <div class="cluster">
                  <Button variant="accent" onClick={() => void startInstall('engine_upgrade')}>
                    {t('welcome.engine.upgrade')}
                  </Button>
                </div>
              )}
            </>
          )}
          {needSplash && install.phase !== 'unavailable' && (
            <div class="cluster">
              <Button
                variant={splashAccent ? 'accent' : 'outline'}
                disabled={brewS !== 'ok'}
                loading={install.phase === 'running'}
                onClick={() => void startInstall('engine_install')}
                data-testid="install-splash"
              >
                {install.phase === 'running' ? t('welcome.engine.splash_installing') : t('welcome.engine.splash_install')}
              </Button>
              {brewS !== 'ok' && <p class="meta">{t('welcome.engine.needs_brew')}</p>}
            </div>
          )}
          {(install.phase === 'running' || lines.length > 0) && (
            <LogPane lines={lines} label={t('welcome.engine.splash_log')} height={220} empty={t('welcome.engine.log_waiting')} />
          )}
          {install.phase === 'done' && <p class="body" role="status">{t('welcome.engine.installed', { version: engine?.version ?? '' })}</p>}
          {install.phase === 'failed' && (
            <Banner
              tone="critical"
              title={t('welcome.engine.splash_failed')}
              actions={
                <div class="cluster">
                  {lines.length > 0 && <CopyButton text={lines.map((l) => l.text).join('\n')} label={t('welcome.engine.copy_log')} what={t('welcome.engine.splash_log')} />}
                  <Button size="s" onClick={() => void startInstall('engine_install')}>
                    {t('welcome.engine.retry')}
                  </Button>
                  {hosted && (
                    <Button size="s" onClick={() => void runInTerminal(SPLASH_INSTALL_COMMAND)}>
                      {t('welcome.engine.open_terminal_cmd')}
                    </Button>
                  )}
                </div>
              }
            >
              {install.message && <code class="mono">{install.message}</code>}
            </Banner>
          )}
          {install.phase === 'unavailable' && (
            <div class="stack wz-tight" data-testid="install-unavailable">
              <p class="body">{t('welcome.engine.install_unavailable')}</p>
              <CodeBlock code={install.command} what={t('welcome.engine.splash_command')} />
              <div class="cluster">
                {hosted && (
                  <Button size="s" onClick={() => void runInTerminal(install.command)}>
                    {t('welcome.engine.open_terminal_cmd')}
                  </Button>
                )}
                <Button size="s" onClick={recheck}>
                  {t('welcome.engine.check_again')}
                </Button>
              </div>
            </div>
          )}
        </CheckRow>

        {report === 'unavailable' ? (
          <CheckRow label={t('welcome.engine.row.shell')} status="skip" value={<span class="mute">{t('welcome.engine.shell_unknown')}</span>} testId="check-shell" />
        ) : doctor.error ? (
          <CheckRow label={t('welcome.engine.row.shell')} status="skip" value={t('welcome.engine.shell_error')} testId="check-shell">
            <LoadError thing={t('welcome.engine.doctor')} error={doctor.error} onRetry={() => void doctor.reload()} />
          </CheckRow>
        ) : (
          <>
            <CheckRow
              label={t('welcome.engine.row.shell')}
              status={shellStatus(report, shadow)}
              testId="check-shell"
              value={
                !report ? (
                  t('welcome.engine.checking')
                ) : !shadow ? (
                  <span class="mute">{t('welcome.engine.shell_unknown')}</span>
                ) : shadow.status === 'ok' ? (
                  t('welcome.engine.shell_ok')
                ) : (
                  shadow.message
                )
              }
            >
              {shadow && (shadow.status === 'warn' || shadow.status === 'fail') && (
                <Disclosure summary={t('welcome.engine.shell_what')}>
                  {shadow.fix && <p class="body">{shadow.fix}</p>}
                  <p class="body">{t('welcome.engine.shell_fix')}</p>
                </Disclosure>
              )}
            </CheckRow>
            {perms && (
              <CheckRow
                label={t('welcome.engine.row.permissions')}
                status={perms.status}
                testId="check-permissions"
                value={perms.status === 'ok' ? t('welcome.engine.perms_ok') : perms.message}
              >
                {perms.fix && perms.status !== 'ok' && <CodeBlock code={perms.fix} what={t('welcome.engine.row.permissions')} wrap />}
              </CheckRow>
            )}
          </>
        )}
      </dl>
    </StepLayout>
  );
}
