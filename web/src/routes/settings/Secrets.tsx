/**
 * Secrets (docs/ui/05 §6, SPEC §17.2): the API key and the Hugging Face token override. They
 * save immediately through their own routes and never enter the save bar (decision G4).
 */
import { useEffect, useState } from 'preact/hooks';
import type { HfTokenTestOut } from '../../api/models';
import { Button, ConfirmSheet, Field, Tag, TextInput, toast, toastError } from '../../components';
import { useApi } from '../../lib/use-api';
import { t } from '../../strings/settings';
import { settingsApi } from './api';
import type { SettingsForm } from './state';

/** Revealed secrets re-mask after 30 s (decision G5). */
export const REMASK_S = 30;

export function ApiKeyField({ form }: { form: SettingsForm }) {
  const set = !!form.envelope.value?.secrets.api_key_set;
  const meta = useApi((s) => settingsApi.secretMeta('api_key', s), [set, form.envelope.value]);
  const [shown, setShown] = useState<string | null>(null);
  const [once, setOnce] = useState(false);
  const [left, setLeft] = useState(0);
  const [sheet, setSheet] = useState<'rotate' | 'delete' | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!shown || once) return;
    setLeft(REMASK_S);
    const id = setInterval(() => {
      setLeft((s) => {
        if (s <= 1) {
          setShown(null);
          return 0;
        }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [shown, once]);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toastError(t('settings.secret.failed'), e);
    } finally {
      setBusy(false);
    }
  }
  const reveal = () =>
    run(async () => {
      const { key } = await settingsApi.revealApiKey();
      setOnce(false);
      setShown(key);
    });
  const copy = () =>
    run(async () => {
      const { key } = await settingsApi.revealApiKey();
      if (key) await navigator.clipboard?.writeText(key);
      toast(t('settings.secret.copied'));
    });
  const generate = () =>
    run(async () => {
      const { key } = await settingsApi.generateApiKey();
      setOnce(true);
      setShown(key);
      setSheet(null);
      await form.load(true);
      toast(t('settings.secret.rotated'));
    });
  const remove = () =>
    run(async () => {
      await settingsApi.deleteApiKey();
      setShown(null);
      setSheet(null);
      await form.load(true);
      toast(t('settings.secret.removed'));
    });

  return (
    <div class="field secret-field" data-key="security.api_key">
      <div class="field-head">
        <span class="label">{t('settings.secret.api_key')}</span>
        <span class="meta flag mono">Keychain</span>
        {set && <Tag tone="mute">{t('settings.secret.keychain')}</Tag>}
      </div>
      <div class="field-body stack">
        {shown ? (
          <div class="cluster">
            <TextInput class="mono secret-value" value={shown} onChange={() => undefined} readOnly aria-label={t('settings.secret.api_key')} />
            {once ? (
              <span class="meta">{t('settings.secret.shown_once')}</span>
            ) : (
              <span class="meta tnum">{t('settings.secret.remask', { s: left })}</span>
            )}
          </div>
        ) : (
          <p class="mono" data-testid="api-key-masked">{set ? (meta.data?.masked ?? '••••') : t('settings.secret.api_key_none')}</p>
        )}
        <div class="cluster">
          {set ? (
            <>
              <Button size="s" disabled={busy} onClick={() => (shown ? setShown(null) : void reveal())}>
                {shown ? t('settings.secret.hide') : t('settings.secret.reveal')}
              </Button>
              <Button size="s" disabled={busy} onClick={() => void copy()}>
                {t('settings.secret.copy')}
              </Button>
              <Button size="s" disabled={busy} onClick={() => setSheet('rotate')}>
                {t('settings.secret.rotate')}
              </Button>
              <Button size="s" variant="text" disabled={busy} onClick={() => setSheet('delete')}>
                {t('settings.secret.remove')}
              </Button>
            </>
          ) : (
            <Button size="s" variant="solid" loading={busy} onClick={() => void generate()}>
              {t('settings.secret.generate')}
            </Button>
          )}
        </div>
      </div>
      <ConfirmSheet
        open={sheet === 'rotate'}
        title={t('settings.secret.rotate_title')}
        confirmLabel={t('settings.secret.rotate')}
        busy={busy}
        onClose={() => setSheet(null)}
        onConfirm={generate}
      >
        <p>{t('settings.secret.rotate_body')}</p>
      </ConfirmSheet>
      <ConfirmSheet
        open={sheet === 'delete'}
        title={t('settings.secret.delete_title')}
        confirmLabel={t('settings.secret.remove')}
        busy={busy}
        onClose={() => setSheet(null)}
        onConfirm={remove}
      >
        <p>{t('settings.secret.delete_body')}</p>
      </ConfirmSheet>
    </div>
  );
}

export function HfTokenField({ form }: { form: SettingsForm }) {
  const secrets = form.envelope.value?.secrets;
  const meta = useApi((s) => settingsApi.secretMeta('hf_token', s), [secrets?.hf_token_override_set], !!secrets?.hf_token_override_set);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<HfTokenTestOut | null>(null);
  const source = secrets?.hf_token_override_set ? 'override' : secrets?.hf_login_token_present ? 'login' : 'none';
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toastError(t('settings.secret.failed'), e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Field label={t('settings.secret.hf')} flag="env HF_TOKEN" help={t('settings.secret.hf_help')}>
      {({ id, describedBy }) => (
        <div class="stack">
          <p class="meta">
            {t(`settings.secret.hf_source.${source}` as 'settings.secret.hf_source.none')}
            {result?.ok && result.user ? ` (${result.user})` : ''}
            {source === 'override' && meta.data?.masked ? <span class="mono"> · {meta.data.masked}</span> : null}
          </p>
          <div class="cluster">
            <TextInput
              id={id}
              type="password"
              autocomplete="off"
              aria-describedby={describedBy}
              placeholder={t('settings.secret.hf_enter')}
              value={token}
              onChange={setToken}
            />
            <Button
              size="s"
              variant="solid"
              disabled={!token || busy}
              onClick={() =>
                void run(async () => {
                  await settingsApi.setHfToken(token);
                  setToken('');
                  await form.load(true);
                  toast(t('settings.secret.hf_saved'));
                })
              }
            >
              {t('settings.secret.hf_save')}
            </Button>
            <Button size="s" disabled={busy} onClick={() => void run(async () => setResult(await settingsApi.testHfToken(token || undefined)))}>
              {t('settings.secret.hf_test')}
            </Button>
            {secrets?.hf_token_override_set && (
              <Button
                size="s"
                variant="text"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await settingsApi.deleteHfToken();
                    await form.load(true);
                    toast(t('settings.secret.hf_removed'));
                  })
                }
              >
                {t('settings.secret.remove')}
              </Button>
            )}
          </div>
          {result && (
            <p role="status" class={result.ok ? 'body' : 'field-error'}>
              {result.ok
                ? t('settings.secret.hf_ok', { user: result.user ?? '—' })
                : t('settings.secret.hf_bad', { error: result.error ?? '—' })}
            </p>
          )}
        </div>
      )}
    </Field>
  );
}
