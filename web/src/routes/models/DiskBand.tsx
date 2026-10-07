import { useEffect } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { DiskUsage } from '../../api/models';
import { MeterBar } from '../../components/KeyValue';
import { Section } from '../../components/Section';
import { LoadError } from '../../components/States';
import { formatBytes } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { loadSystem, systemInfo } from '../../store/live';
import { t } from '../../strings/models';
import { getStorage } from './api';

/**
 * Disk usage band (03 §1.6, SPEC §9.5): models and cache on the models volume. Uses
 * `GET /models` → `disk` when it answers, else `POST /system` volumes and `GET /storage`.
 */
export function DiskBand({ disk, loading }: { disk: DiskUsage | null; loading?: boolean }) {
  const storage = useApi((signal) => getStorage(signal), []);
  useEffect(() => {
    void loadSystem();
  }, []);
  const sys = systemInfo.value;
  const st = storage.data;
  const modelsVol = sys?.disk.models;
  const cacheVol = sys?.disk.cache;
  const modelsBytes = disk?.models_bytes ?? st?.models_bytes ?? null;
  const cacheBytes = disk?.cache_bytes ?? st?.cache_bytes ?? null;
  const free = disk?.free_bytes ?? modelsVol?.free_bytes ?? st?.free_bytes ?? null;
  const total = disk?.total_bytes ?? modelsVol?.total_bytes ?? null;
  const modelsDir = disk?.models_dir ?? st?.models_dir ?? modelsVol?.path ?? null;
  const cacheDir = disk?.cache_dir ?? st?.cache_dir ?? cacheVol?.path ?? null;
  const splashBytes = st?.splash_data_bytes ?? null;
  const separateCache = Boolean(modelsVol && cacheVol && (modelsVol.total_bytes !== cacheVol.total_bytes || modelsVol.free_bytes !== cacheVol.free_bytes));
  const nothing = !disk && !st && !sys;
  const used = typeof total === 'number' && typeof free === 'number' ? total - free : null;

  return (
    <Section label={t('models.disk.label')} id="models-disk">
      <div class="stack disk-band" data-loading={loading || (storage.loading && !st) ? 'true' : undefined} data-testid="disk-band">
        {nothing && storage.error ? (
          <LoadError thing={t('models.disk.thing')} error={storage.error} onRetry={() => void storage.reload()} />
        ) : (
          <>
            <p class="label tnum disk-line">
              <span>{t('models.disk.models', { size: formatBytes(modelsBytes) })}</span>
              <span>{t('models.disk.cache', { size: formatBytes(cacheBytes) })}</span>
              {splashBytes !== null && <span>{t('models.disk.splash', { size: formatBytes(splashBytes) })}</span>}
              <span>{t('models.disk.free', { free: formatBytes(free), total: formatBytes(total) })}</span>
            </p>
            <MeterBar
              label={t('models.disk.meter', { volume: separateCache ? t('models.disk.models_volume') : t('models.disk.volume') })}
              valueText={t('models.disk.used', { used: formatBytes(used), total: formatBytes(total) })}
              total={total}
              segments={separateCache ? [{ label: t('models.disk.seg_models'), value: modelsBytes }] : [
                { label: t('models.disk.seg_models'), value: modelsBytes },
                { label: t('models.disk.seg_cache'), value: cacheBytes },
              ]}
            />
            {separateCache && cacheVol && (
              <>
                <p class="meta tnum">{t('models.disk.cache_volume', { free: formatBytes(cacheVol.free_bytes), total: formatBytes(cacheVol.total_bytes) })}</p>
                <MeterBar
                  label={t('models.disk.meter', { volume: t('models.disk.cache_volume_name') })}
                  total={cacheVol.total_bytes}
                  segments={[{ label: t('models.disk.seg_cache'), value: cacheBytes }]}
                />
              </>
            )}
            <p class="meta disk-paths">
              {modelsDir && <span class="mono">{modelsDir}</span>}
              {cacheDir && <span class="mono">{cacheDir}</span>}
              {splashBytes !== null && <span>{t('models.disk.splash_note')}</span>}
              <Link href="/settings/storage" class="disk-link">
                {t('models.disk.storage_settings')}
              </Link>
            </p>
          </>
        )}
      </div>
    </Section>
  );
}
