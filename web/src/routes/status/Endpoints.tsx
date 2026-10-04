import { useEffect, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import { CopyButton, copyText } from '../../components/CopyButton';
import { KeyValue } from '../../components/KeyValue';
import { Section } from '../../components/Section';
import { toast, toastError } from '../../components/Toast';
import { settings } from '../../store';
import { loadSystem, systemInfo } from '../../store/live';
import { t } from '../../strings/status';
import { fetchApiKey } from './api';
import { endpoints, lanUrl } from './logic';

/** Copies the API key without showing it (docs/ui/02 §6). */
export function CopyKeyButton() {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      class="btn"
      data-variant="text"
      data-size="s"
      disabled={busy}
      aria-busy={busy ? 'true' : undefined}
      onClick={async () => {
        setBusy(true);
        try {
          const key = await fetchApiKey();
          if (!key) toast(t('status.endpoints.key_missing'));
          else if (await copyText(key)) toast(t('status.endpoints.key_copied'));
        } catch (err) {
          toastError(t('status.endpoints.key_failed'), err);
        } finally {
          setBusy(false);
        }
      }}
    >
      {t('status.endpoints.copy_key')}
    </button>
  );
}

/** Endpoints band (docs/ui/02 §6): base URLs with Copy, API key state, LAN address. */
export function EndpointsBand() {
  const g = settings.value?.settings.global;
  const server = (g?.server ?? {}) as { host?: string; port?: number; allowed_hosts?: string[] };
  const keyRequired = (g?.security as { api_key_required?: boolean } | undefined)?.api_key_required === true;
  useEffect(() => {
    void loadSystem();
  }, []);
  const urls = endpoints(location.origin);
  const lan = lanUrl(server.host, systemInfo.value?.hostname, server.port);
  const items = [
    {
      key: 'openai',
      label: t('status.endpoints.openai'),
      value: (
        <span class="status-endpoint">
          <code class="mono" data-testid="endpoint-openai">
            {urls.openai}
          </code>
          <CopyButton text={urls.openai} what={t('status.endpoints.openai')} />
        </span>
      ),
    },
    {
      key: 'anthropic',
      label: t('status.endpoints.anthropic'),
      value: (
        <span class="status-endpoint">
          <code class="mono">{urls.anthropic}</code>
          <CopyButton text={urls.anthropic} what={t('status.endpoints.anthropic')} />
        </span>
      ),
    },
    {
      key: 'key',
      label: t('status.endpoints.key'),
      value: keyRequired ? (
        <span class="status-endpoint">
          <span>{t('status.endpoints.key_on')}</span>
          <CopyKeyButton />
          <Link href="/settings/security" class="btn" data-variant="text" data-size="s">
            {t('status.endpoints.security')}
          </Link>
        </span>
      ) : (
        <span class="status-endpoint">
          <span>{t('status.endpoints.key_off')}</span>
          <Link href="/settings/security" class="btn" data-variant="text" data-size="s">
            {t('status.endpoints.security')}
          </Link>
        </span>
      ),
    },
    ...(lan
      ? [
          {
            key: 'lan',
            label: t('status.endpoints.lan'),
            value: (
              <span class="status-endpoint">
                <span>
                  {t('status.endpoints.lan_value', { url: '' })}
                  <code class="mono">{lan}</code>
                </span>
                <CopyButton text={lan} what={t('status.endpoints.lan')} />
              </span>
            ),
            meta: t('status.endpoints.allowed_hosts', { n: server.allowed_hosts?.length ?? 0 }),
          },
        ]
      : []),
  ];
  return (
    <Section label={t('status.endpoints.label')} id="status-endpoints" tight>
      <KeyValue items={items} label={t('status.endpoints.label')} />
    </Section>
  );
}
