/**
 * Settings → About → Remove Splash GUI data… (SPEC §19, D72; docs/ui/05; PKG-12).
 *
 * The sheet loads `POST /uninstall/plan`, lists the steps and the data folder's size, and asks
 * about models and cache separately (both unchecked; either one needs a typed DELETE). Remove calls
 * `POST /uninstall` with `stop: true`: the manager restores the integrations, removes the PATH
 * block, the shim and the data, then stops, and this page goes offline. When the menu bar app is
 * connected it must run the removal itself (it alone can unregister its login item and agent, and
 * it would start the manager again), so the sheet points there instead.
 */
import { useState } from 'preact/hooks';
import type { UninstallPlan, UninstallResult } from '../../api/models';
import { Button, Checkbox, LoadError, Loading, Sheet, TextInput, toastError } from '../../components';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/settings';
import { settingsApi } from './api';

export function RemoveData() {
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState<UninstallPlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [models, setModels] = useState(false);
  const [cache, setCache] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<UninstallResult | null>(null);

  async function load() {
    setError(null);
    setPlan(null);
    try {
      setPlan(await settingsApi.uninstallPlan());
    } catch (err) {
      setError(err);
    }
  }
  function show() {
    setModels(false);
    setCache(false);
    setTyped('');
    setDone(null);
    setOpen(true);
    void load();
  }
  async function remove() {
    setBusy(true);
    try {
      setDone(await settingsApi.uninstall({ delete_data: true, delete_models: models, delete_cache: cache, stop: true }));
    } catch (err) {
      toastError(t('settings.remove.failed'), err);
    } finally {
      setBusy(false);
    }
  }

  const needsTyped = models || cache;
  const blocked = !plan || plan.app_connected || (needsTyped && typed !== 'DELETE');
  const outside = plan?.items.filter((i) => !i.deletable) ?? [];

  return (
    <>
      <Button size="s" variant="text" onClick={show} data-testid="remove-data">
        {t('settings.remove.button')}
      </Button>
      <Sheet
        open={open}
        title={t('settings.remove.title')}
        onClose={() => setOpen(false)}
        busy={busy}
        role="alertdialog"
        testId="remove-data-sheet"
        footer={
          done ? (
            <Button onClick={() => setOpen(false)}>{t('common.close')}</Button>
          ) : (
            <>
              <Button variant="text" onClick={() => setOpen(false)}>
                {t('common.cancel')}
              </Button>
              <Button variant="accent" loading={busy} disabled={blocked} onClick={() => void remove()}>
                {t('settings.remove.confirm')}
              </Button>
            </>
          )
        }
      >
        {done ? (
          <div class="stack">
            <p>{t('settings.remove.done', { freed: formatBytes(done.freed_bytes) })}</p>
            <p class="meta">{t('settings.remove.done_next')}</p>
          </div>
        ) : error ? (
          <LoadError thing={t('settings.remove.thing')} error={error} onRetry={() => void load()} />
        ) : !plan ? (
          <Loading />
        ) : (
          <div class="stack">
            <ol class="steps">
              {plan.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            <p>{t('settings.remove.data', { size: formatBytes(plan.data_bytes), home: plan.home })}</p>
            <Checkbox
              checked={models}
              onChange={setModels}
              label={t('settings.remove.models', { size: formatBytes(plan.models_bytes) })}
            />
            <Checkbox
              checked={cache}
              onChange={setCache}
              label={t('settings.remove.cache', { size: formatBytes(plan.cache_bytes) })}
            />
            {outside.map((item) => (
              <p class="meta" key={item.path}>
                {t('settings.remove.outside', { path: item.path })}
              </p>
            ))}
            {needsTyped && (
              <label class="stack">
                <span>{t('settings.remove.type_delete')}</span>
                <TextInput value={typed} onChange={setTyped} aria-label={t('settings.remove.type_delete')} />
              </label>
            )}
            {plan.app_connected && <p class="meta">{t('settings.remove.app_connected')}</p>}
          </div>
        )}
      </Sheet>
    </>
  );
}
