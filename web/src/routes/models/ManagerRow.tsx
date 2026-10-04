import { Link } from 'wouter-preact';
import type { DownloadItem, InstalledModel } from '../../api/models';
import { Banner } from '../../components/Banner';
import { Button, ExternalLink } from '../../components/Button';
import { Checkbox } from '../../components/controls';
import { ProgressBar } from '../../components/ProgressBar';
import { StatusChip } from '../../components/StatusChip';
import { Tag } from '../../components/Tag';
import { formatBytes, formatRelativeTime } from '../../lib/format';
import { modelSettingsPath } from '../../lib/model-id';
import { engineState } from '../../store';
import { t } from '../../strings/models';
import * as flows from './actions';
import { downloadMeta } from './DownloadsPanel';
import { hfUrl, itemProgress, percent, rowLabel, sha7, shortName } from './logic';
import { Row } from './Row';

export interface ManagerRowProps {
  id: string;
  model: InstalledModel | null;
  index: number;
  active: boolean;
  download?: DownloadItem | undefined;
  verifying?: boolean;
  verifyError?: string | null | undefined;
  /** Only one "Update available" tag in the list gets the accent (00 §5.2). */
  accentUpdate?: boolean;
  selecting?: boolean;
  selected?: boolean;
  onToggle?: (checked: boolean) => void;
  onOpen: () => void;
  onUnload: () => void;
  onVerify: () => void;
  onUpdate: () => void;
  onDelete: () => void;
  onCancel: (item: DownloadItem) => void;
}

function StatusTags({ model, download, verifying, accentUpdate }: Pick<ManagerRowProps, 'model' | 'download' | 'verifying' | 'accentUpdate'>) {
  if (download && (download.state === 'running' || download.state === 'queued')) {
    return <Tag tone="ink">{download.state === 'queued' ? t('models.dl.state.queued') : t('models.status.downloading', { pct: percent(itemProgress(download)) })}</Tag>;
  }
  if (download?.state === 'paused') return <Tag tone="ink">{t('models.status.paused')}</Tag>;
  if (verifying || download?.state === 'verifying' || model?.status === 'verifying') {
    return (
      <Tag tone="ink" dots>
        {t('models.status.verifying')}
      </Tag>
    );
  }
  if (!model) return null;
  const tags = [];
  if (model.status === 'loading') {
    tags.push(
      <Tag key="loading" tone="ink" dots>
        {t('models.status.loading')}
      </Tag>,
    );
  }
  if (model.status === 'broken') tags.push(<Tag key="broken" tone="ink">{t('models.status.broken')}</Tag>);
  if (model.status === 'update_available' && !model.pinned) {
    tags.push(
      <Tag key="update" tone={accentUpdate ? 'acc' : 'ink'}>
        {t('models.status.update')}
      </Tag>,
    );
  }
  if (model.status === 'downloading' && typeof model.progress === 'number') {
    tags.push(<Tag key="dl" tone="ink">{t('models.status.downloading', { pct: percent(model.progress) })}</Tag>);
  }
  return tags.length ? <>{tags}</> : null;
}

/** One Manager row (03 §1.3): name, `NN — FORMAT · SIZE`, meta line, status, hover actions. */
export function ManagerRow(props: ManagerRowProps) {
  const { id, model, index, active, download, verifying, verifyError, selecting, selected, onToggle, onOpen, onUnload, onVerify, onUpdate, onDelete, onCancel } = props;
  const short = shortName(id);
  const dlActive = download && (download.state === 'running' || download.state === 'queued' || download.state === 'paused');
  const size = model?.size_bytes ?? download?.bytes_total ?? null;

  const detail = (
    <span class="mrow-meta">
      <span class="mono">{id}</span>
      {model?.family && <span>{model.family}</span>}
      {model?.commit && <span class="mono">{t('models.row.rev', { rev: sha7(model.commit)! })}</span>}
      {model?.pinned && (
        <Tag tone="mute" title={t('models.row.pinned_title', { sha: sha7(model.revision ?? model.commit) ?? '' })}>
          {t('models.status.pinned')}
        </Tag>
      )}
      {model?.language_only && <Tag tone="ink">{t('models.row.language_only')}</Tag>}
      {model && <span>{model.last_used_at ? t('models.row.used', { time: formatRelativeTime(model.last_used_at) }) : t('models.row.never_used')}</span>}
      {model && model.unique_bytes !== model.size_bytes && <span class="tnum">{t('models.row.unique', { size: formatBytes(model.unique_bytes) })}</span>}
      {dlActive && <span class="tnum">{downloadMeta(download)}</span>}
    </span>
  );

  const body = (
    <>
      {dlActive && (
        <ProgressBar
          value={download.state === 'queued' ? 0 : itemProgress(download)}
          label={t('models.dl.item_progress', { model: id })}
          live={download.state === 'running'}
          valueText={`${percent(itemProgress(download))} · ${downloadMeta(download)}`}
        />
      )}
      {verifyError && (
        <Banner
          tone="warn"
          title={t('models.verify.failed_title')}
          actions={
            <Button size="s" onClick={() => void flows.startDownload({ id }, model?.size_bytes ?? null).catch(() => undefined)}>
              {t('models.dl.action.redownload')}
            </Button>
          }
        >
          <span class="mono">{verifyError}</span>
        </Banner>
      )}
    </>
  );

  const actions = dlActive ? (
    <>
      {download.state === 'running' && (
        <Button size="s" onClick={() => void flows.pause(download)} aria-label={t('models.dl.pause_label', { model: id })}>
          {t('models.dl.action.pause')}
        </Button>
      )}
      {download.state === 'paused' && (
        <Button size="s" onClick={() => void flows.resume(download)} aria-label={t('models.dl.resume_label', { model: id })}>
          {t('models.dl.action.resume')}
        </Button>
      )}
      <Button size="s" onClick={() => onCancel(download)} aria-label={t('models.dl.cancel_label', { model: id })}>
        {t('models.dl.action.cancel')}
      </Button>
      <Link href="/models/downloader#downloads" class="btn" data-size="s">
        {t('models.dl.action.details')}
      </Link>
    </>
  ) : model ? (
    <>
      {active ? (
        <Button size="s" onClick={onUnload} aria-label={t('models.action.unload_label', { id })}>
          {t('models.action.unload')}
        </Button>
      ) : (
        <Button size="s" onClick={() => void flows.loadModel(id)} aria-label={t('models.action.load_label', { id })} data-testid="row-load">
          {t('models.action.load')}
        </Button>
      )}
      <Link href={modelSettingsPath(id)} class="btn" data-size="s" aria-label={t('models.action.settings_label', { id })}>
        {t('models.action.settings')}
      </Link>
      <Button size="s" onClick={onVerify} disabled={verifying} aria-label={t('models.action.verify_label', { id })}>
        {t('models.action.verify')}
      </Button>
      {model.status === 'update_available' && !model.pinned && (
        <Button size="s" onClick={onUpdate} aria-label={t('models.action.update_label', { id })}>
          {t('models.action.update')}
        </Button>
      )}
      <Button size="s" onClick={onDelete} aria-label={t('models.action.delete_label', { id })} data-testid="row-delete">
        {t('common.delete')}
      </Button>
      <Button size="s" onClick={() => void flows.revealModel(id)} aria-label={t('models.action.reveal_label', { id })}>
        {t('models.action.reveal')}
      </Button>
      <ExternalLink href={hfUrl(id)} class="btn" data-size="s" data-variant="text">
        {t('models.action.hf')}
      </ExternalLink>
    </>
  ) : null;

  return (
    <Row
      name={short}
      nameClass="display-m"
      onOpen={onOpen}
      openLabel={t('models.row.open_label', { id })}
      modelId={id}
      testId={active ? 'active-row' : 'model-row'}
      pinned={active}
      leading={
        selecting && !active && model ? (
          <Checkbox checked={Boolean(selected)} onChange={(c) => onToggle?.(c)} label={<span class="visually-hidden">{t('models.select.row', { id })}</span>} />
        ) : undefined
      }
      status={active ? <StatusChip state={engineState.value} announce={false} /> : <StatusTags model={model} download={download} verifying={verifying} accentUpdate={props.accentUpdate} />}
      label={rowLabel(index, model?.format ?? null, id, size, model ? 1024 : 1000)}
      detail={detail}
      body={dlActive || verifyError ? body : undefined}
      actions={actions}
      expanded={Boolean(dlActive)}
    />
  );
}
