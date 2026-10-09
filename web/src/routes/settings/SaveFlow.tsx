/**
 * The sticky save bar and what saving does: plain save, save & restart, the
 * "requests running" choice, and the rebind move to a new address.
 */
import { useState } from 'preact/hooks';
import { Banner, Button, ConfirmSheet, Sheet, StickySaveBar, toast, toastError } from '../../components';
import { isCancelled, withInstallConfirm } from '../../lib/engine-install';
import { engine } from '../../store';
import { t } from '../../strings/settings';
import { settingsApi, probeOrigin } from './api';
import { engineRunning, rebindUrl } from './form';
import { fieldsByKey, type SettingsForm } from './state';

export interface SaveFlowProps {
  form: SettingsForm;
  /** Where the admin reopens after a rebind. */
  rebindPath?: string;
  /** Per-model pages: "Save & reload model". */
  saveText?: string;
}

export function SettingsSave({ form, rebindPath = '/admin/settings/server', saveText }: SaveFlowProps) {
  const [sheet, setSheet] = useState<'busy' | 'rebind' | null>(null);
  const [pending, setPending] = useState(false);
  const [waiting, setWaiting] = useState<string | null>(null);
  const [timedOut, setTimedOut] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const plan = form.plan.value;
  const running = engineRunning(engine.value?.state);
  const restart = plan.restart && running;
  const inFlight = engine.value?.requests_in_flight ?? 0;
  const restartCount = form.dirty.value.filter((e) => fieldsByKey.value.get(e.ref.key)?.applies === 'restart').length;

  async function save(restartNow: boolean) {
    const rebind = plan.rebind;
    setFailure(null);
    try {
      await form.save();
      setSheet(null);
      if (restartNow && restart) {
        // Kept installing (No in the install sheet): saved, with the restart still pending.
        const restarted = await withInstallConfirm((force) => settingsApi.restartEngine(inFlight > 0 || force)).then(
          () => true,
          (err: unknown) => {
            if (isCancelled(err)) return false;
            throw err;
          },
        );
        toast(t(restarted ? 'settings.saved_restarting' : 'settings.saved'));
        if (!restarted) setPending(true);
      } else {
        toast(t('settings.saved'));
        if (restart) setPending(true);
      }
      if (rebind) {
        const url = rebindUrl(rebind, location, rebindPath);
        const origin = new URL(url).origin;
        setWaiting(url);
        const deadline = Date.now() + 20_000;
        while (Date.now() < deadline) {
          if (await probeOrigin(origin, 500)) {
            location.replace(url);
            return;
          }
          await new Promise((r) => setTimeout(r, 500));
        }
        setWaiting(null);
        setTimedOut(url);
      }
    } catch (err) {
      setFailure(err instanceof Error ? err.message : String(err));
      toastError(t('settings.save_failed'), err);
    }
  }

  function onSave() {
    if (plan.rebind) return setSheet('rebind');
    if (restart && inFlight > 0) return setSheet('busy');
    void save(true);
  }

  const note = restart
    ? restartCount > 0
      ? t('settings.needs_restart', { n: restartCount })
      : undefined
    : plan.restart && !running
      ? t('settings.applies_on_start')
      : undefined;

  const rebindTarget = plan.rebind ? rebindUrl(plan.rebind, location, rebindPath) : '';

  return (
    <>
      {pending && (
        <Banner
          tone="warn"
          title={t('settings.pending_restart')}
          actions={
            <Button
              size="s"
              onClick={() =>
                void withInstallConfirm((force) => settingsApi.restartEngine(force))
                  .then(() => setPending(false))
                  .catch((e) => isCancelled(e) || toastError(t('settings.save_failed'), e))
              }
            >
              {t('settings.restart_now')}
            </Button>
          }
        >
          {' '}
        </Banner>
      )}
      {waiting && <Banner tone="info">{t('settings.rebind_waiting', { url: waiting })}</Banner>}
      {timedOut && (
        <Banner tone="critical">
          {t('settings.rebind_timeout')} <a href={timedOut}>{timedOut}</a>
        </Banner>
      )}
      <StickySaveBar
        changes={form.dirty.value.length}
        restart={restart}
        saving={form.saving.value}
        invalid={form.invalid.value}
        onSave={onSave}
        onDiscard={() => {
          form.discard();
          toast(t('settings.discarded'));
        }}
        saveText={saveText}
        note={note}
      />
      <Sheet
        open={sheet === 'busy'}
        title={t('settings.busy_title')}
        role="alertdialog"
        busy={form.saving.value}
        onClose={() => setSheet(null)}
        footer={
          <>
            <Button variant="solid" disabled={form.saving.value} onClick={() => void save(false)}>
              {t('settings.busy_later')}
            </Button>
            <Button variant="accent" loading={form.saving.value} onClick={() => void save(true)}>
              {t('settings.busy_now')}
            </Button>
          </>
        }
      >
        <p class="body">{t('settings.busy_body', { n: inFlight })}</p>
        {failure && <p class="field-error">{failure}</p>}
      </Sheet>
      <ConfirmSheet
        open={sheet === 'rebind'}
        title={t('settings.rebind_title')}
        confirmLabel={t('settings.rebind_confirm')}
        busy={form.saving.value}
        error={failure}
        onClose={() => setSheet(null)}
        onConfirm={() => save(true)}
      >
        <p class="body">{t('settings.rebind_body', { url: rebindTarget })}</p>
      </ConfirmSheet>
    </>
  );
}
