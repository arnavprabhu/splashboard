import { useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { InspectResult, InstalledModel, ModelCard, ModelDetail, ModelFile, VariantOut } from '../../api/models';
import { Banner } from '../../components/Banner';
import { Button, ExternalLink } from '../../components/Button';
import { CopyButton } from '../../components/CopyButton';
import { Disclosure } from '../../components/Disclosure';
import { KeyValue, type KeyValueItem } from '../../components/KeyValue';
import { Menu } from '../../components/Menu';
import { Sheet } from '../../components/Sheet';
import { Install } from '../../components/Install';
import { Loading, LoadError } from '../../components/States';
import { StatusChip } from '../../components/StatusChip';
import { Tag } from '../../components/Tag';
import { installOf } from '../../lib/engine-install';
import { DASH, formatBytes, formatDate } from '../../lib/format';
import { modelSettingsPath, splitModelId } from '../../lib/model-id';
import { useApi } from '../../lib/use-api';
import { engine, engineState } from '../../store';
import { t } from '../../strings/models';
import * as flows from './actions';
import { getCard, getModel, inspectModel } from './api';
import { CompatTag } from './bits';
import { formatLabel, hfUrl, installedVariants, isClefId, isProjector, pickVariant, repoOf, sha7, shortName } from './logic';
import { Markdown } from './markdown';
import { VariantTable } from './VariantTable';

export interface ModelDrawerProps {
  /** `?model=` value: an installed ID or any repo ID (search result, catalog row). */
  id: string | null;
  installed: readonly InstalledModel[];
  activeId: string | null;
  onClose: () => void;
  onDelete?: (model: InstalledModel) => void;
  onVerify?: (model: InstalledModel) => void;
}

/**
 * The model detail drawer (03 §2, SPEC §9.3): a 720px Sheet (decision M1), URL-addressable
 * through `?model=`. Facts from /models/{id}, /inspect and /card; each part fails on its own.
 */
export function ModelDrawer({ id, installed, activeId, onClose, onDelete, onVerify }: ModelDrawerProps) {
  const open = id !== null;
  const repo = id ? repoOf(id) : '';
  const requestedVariant = id ? splitModelId(id).variant : null;
  const installedHere = id ? installed.filter((m) => repoOf(m.id).toLowerCase() === repo.toLowerCase()) : [];
  const exact = installedHere.find((m) => m.id === id) ?? (requestedVariant ? undefined : installedHere[0]);
  const detail = useApi<ModelDetail | null>((signal) => (exact ? getModel(exact.id, signal) : Promise.resolve(null)), [exact?.id], open && Boolean(exact));
  const inspect = useApi<InspectResult>((signal) => inspectModel(repo, signal), [repo], open);
  const card = useApi<ModelCard>((signal) => getCard(repo, signal), [repo], open);

  if (!open || !id) return null;
  const ins = inspect.data;
  const det = detail.data;
  const model: InstalledModel | undefined = exact;
  const format = det?.format ?? model?.format ?? ins?.format ?? null;
  const isGguf = format === 'gguf' || (ins?.variants?.length ?? 0) > 0;
  const variants: VariantOut[] = (ins?.variants ?? []).filter((v) => !isProjector(v.name));
  const installedSet = installedVariants(repo, installed);
  const rec = pickVariant(ins, requestedVariant);
  const isActive = model ? model.id === activeId : false;
  const install = isActive ? installOf(engine.value) : null;
  const files: ModelFile[] = (det?.files?.length ? det.files : card.data?.files) ?? [];
  const tags = card.data?.tags ?? [];
  const latest = det?.latest_commit ?? null;
  const updateAvailable = det?.update_available || model?.status === 'update_available';

  const download = (variant: string | null) => {
    const target = variant ? `${repo}:${variant}` : repo;
    const size = variant ? (ins?.variants?.find((v) => v.name === variant)?.size_bytes ?? null) : null;
    void flows.startDownload({ id: target, language_only: ins?.badge === 'text_only' }, size).catch(() => undefined);
  };

  const canDownloadMore = isGguf ? variants.some((v) => v.loadable !== false && !installedSet.has(v.name)) : !model;
  // As in the Downloader catalog (03 §3.2 "same as the drawer"): the Variant menu selects, only the button downloads.
  const [picked, setPicked] = useState<string | null>(null);
  const downloadable = (v: VariantOut) => v.loadable !== false && !installedSet.has(v.name);
  const choice = variants.find((v) => v.name === picked) ?? variants.find((v) => v.name === rec && downloadable(v)) ?? variants.find(downloadable) ?? null;
  const choiceSize = choice ? (choice.download_bytes ? formatBytes(choice.download_bytes, { base: 1000 }) : choice.size_bytes ? formatBytes(choice.size_bytes, { base: 1000 }) : null) : null;
  const compatible = ins ? ins.badge !== 'incompatible' : true;

  const facts: KeyValueItem[] = [
    { key: 'family', label: t('models.drawer.family'), value: det?.family ?? model?.family ?? ins?.family ?? null },
    { key: 'format', label: t('models.drawer.format'), value: format ? formatLabel(format, id) : null },
    {
      key: 'vision',
      label: t('models.drawer.vision'),
      value:
        ins?.vision?.available || model?.vision
          ? ins?.vision?.projector
            ? t('models.drawer.vision_yes_proj', { projector: ins.vision.projector })
            : t('models.drawer.vision_yes')
          : ins || model
            ? t('models.drawer.vision_no')
            : null,
      meta: ins?.vision && !ins.vision.available && ins.vision.reason ? ins.vision.reason : undefined,
    },
    { key: 'license', label: t('models.drawer.license'), value: card.data?.license ?? null },
    {
      key: 'commit',
      label: t('models.drawer.commit'),
      value: (det?.commit ?? model?.commit ?? ins?.commit) ? (
        <span class="cluster" style={{ gap: '8px' }}>
          {model ? (
            <span class="mono">{t('models.drawer.commit_installed', { sha: sha7(det?.commit ?? model.commit) ?? DASH })}</span>
          ) : (
            <span class="mono">{sha7(ins?.commit) ?? DASH}</span>
          )}
          {latest && model && <span class="mono">{t('models.drawer.commit_latest', { sha: sha7(latest) ?? DASH })}</span>}
          {updateAvailable && !model?.pinned && <Tag tone="acc">{t('models.status.update')}</Tag>}
          {model?.pinned && <Tag tone="mute">{t('models.status.pinned')}</Tag>}
        </span>
      ) : null,
    },
    {
      key: 'draft',
      label: t('models.drawer.draft'),
      value: det?.draft?.repo_id ?? model?.draft?.repo_id ?? ins?.draft ? (
        <span class="mono">
          {det?.draft?.repo_id ?? model?.draft?.repo_id ?? ins?.draft}
          {(det?.draft?.commit ?? model?.draft?.commit) ? ` @ ${sha7(det?.draft?.commit ?? model?.draft?.commit)}` : ''}
        </span>
      ) : null,
    },
    {
      key: 'template',
      label: t('models.drawer.template'),
      value: det?.chat_template_mode ?? null,
      meta: model && !det?.chat_template_mode ? t('models.drawer.template_unknown') : undefined,
    },
  ];

  return (
    <Sheet
      open
      title={t('models.drawer.title', { short: shortName(repo) })}
      onClose={onClose}
      width="l"
      testId="model-drawer"
      headActions={<CopyButton text={id} what={id} label={t('models.drawer.copy_id')} />}
    >
      <div class="stack drawer">
        <div class="cluster drawer-id">
          <span class="mono">{id}</span>
          {isActive ? <StatusChip state={engineState.value} announce={false} /> : ins ? <CompatTag state={ins.badge} acc={false} /> : inspect.loading ? <CompatTag state="checking" /> : null}
          {model && !isActive && <Tag tone="ink">{t('models.status.installed')}</Tag>}
        </div>
        {install && <Install install={install} />}

        <div class="cluster drawer-actions">
          {compatible && canDownloadMore && isGguf && variants.length > 0 && (
            <span class="cluster">
            <Button variant="accent" disabled={!choice || !downloadable(choice)} onClick={() => choice && download(choice.name)} data-testid="drawer-download">
              {choiceSize ? t('models.action.download_variant_size', { variant: choice?.name ?? '', size: choiceSize }) : t('models.action.download_variant', { variant: choice?.name ?? '' })}
            </Button>
            <Menu
              label={t('models.action.variant')}
              variant="text"
              radio
              testId="drawer-variant"
              items={variants.map((v) => ({
                key: v.name,
                label: <span class="mono">{v.name}</span>,
                text: v.name,
                detail:
                  v.loadable === false
                    ? t('models.variant.unsupported', { reason: v.reason ?? t('models.variant.unsupported_default') })
                    : [formatBytes(v.size_bytes, { base: 1000 }), v.recommended ? t('models.variant.recommended') : null, installedSet.has(v.name) ? t('models.variant.installed') : null]
                        .filter(Boolean)
                        .join(' · '),
                disabled: v.loadable === false || installedSet.has(v.name),
                checked: v.name === choice?.name,
                onSelect: () => setPicked(v.name),
              }))}
            />
            </span>
          )}
          {compatible && canDownloadMore && !isGguf && (
            <Button variant="accent" onClick={() => download(null)} data-testid="drawer-download">
              {t('common.download')}
            </Button>
          )}
          {model && (
            <>
              {isActive ? (
                <Button onClick={() => void flows.unloadModel()}>{t('models.action.unload')}</Button>
              ) : (
                <Button onClick={() => void flows.loadModel(model.id)}>{t('models.action.load')}</Button>
              )}
              <Link href={modelSettingsPath(model.id)} class="btn">
                {t('models.action.settings')}
              </Link>
              {onVerify && <Button onClick={() => onVerify(model)}>{t('models.action.verify')}</Button>}
              {onDelete && <Button onClick={() => onDelete(model)}>{t('common.delete')}</Button>}
              <Button onClick={() => void flows.revealModel(model.id)}>{t('models.action.reveal')}</Button>
            </>
          )}
          <ExternalLink href={hfUrl(id)} class="btn" data-variant="text">
            {t('models.action.hf')}
          </ExternalLink>
        </div>

        {isClefId(id) && (
          <Banner tone="info" title={t('models.clef.title')}>
            {t('models.clef.body')}
          </Banner>
        )}

        <KeyValue items={facts} label={t('models.drawer.facts')} />

        <section class="stack drawer-section" aria-label={t('models.drawer.variants')}>
          <h3 class="label">{t('models.drawer.variants')}</h3>
          {inspect.loading && !ins ? (
            <Loading label={t('models.byid.checking', { id: repo })} />
          ) : inspect.error ? (
            <LoadError thing={t('models.drawer.compat_thing')} error={inspect.error} onRetry={() => void inspect.reload()} />
          ) : ins && ins.badge === 'incompatible' ? (
            <p class="body">
              <span class="mono">{ins.reason ?? DASH}</span>
            </p>
          ) : isGguf ? (
            <VariantTable
              variants={ins?.variants ?? []}
              installed={installedSet}
              onDownload={compatible ? (v) => download(v.name) : undefined}
              caption={t('models.drawer.variants')}
            />
          ) : (
            <p class="body tnum">
              {format === 'legacy'
                ? t('models.drawer.legacy_line')
                : t('models.drawer.mlx_line', {
                    n: files.filter((f) => f.role === 'weights').length || files.filter((f) => /\.safetensors$/.test(f.path)).length,
                    size: formatBytes(det?.size_bytes ?? model?.size_bytes ?? null),
                  })}
            </p>
          )}
        </section>

        <Disclosure summary={t('models.drawer.files', { n: files.length })}>
          {files.length === 0 ? (
            <p class="meta">{card.loading || detail.loading ? t('common.loading') : DASH}</p>
          ) : (
            <ul class="drawer-files">
              {files.map((f) => (
                <li key={`${f.repo_id}/${f.path}`} class="drawer-file">
                  <span class="mono">{f.repo_id !== repo ? `${f.repo_id} ${f.path}` : f.path}</span>
                  <span class="tnum">{formatBytes(f.size_bytes, { base: 1000 })}</span>
                  <span class="meta">{t(`models.file.role.${f.role ?? 'other'}`)}</span>
                </li>
              ))}
            </ul>
          )}
        </Disclosure>
        <Disclosure summary={t('models.drawer.tags', { n: tags.length })}>
          <p class="cluster drawer-tags">
            {tags.length ? tags.map((tag) => <span key={tag} class="mono meta-mono">{tag}</span>) : <span class="meta">{DASH}</span>}
          </p>
        </Disclosure>

        <section class="stack drawer-section" aria-label={t('models.drawer.card')}>
          <h3 class="label">{t('models.drawer.card')}</h3>
          {card.loading && !card.data ? (
            <Loading />
          ) : card.error ? (
            <>
              <Banner tone="warn">{t('models.drawer.hf_unreachable')}</Banner>
              <LoadError thing={t('models.drawer.card_thing')} error={card.error} onRetry={() => void card.reload()} />
            </>
          ) : card.data?.markdown.trim() ? (
            <>
              {card.data.gated && <Banner tone="info">{t('models.drawer.gated')}</Banner>}
              <Markdown source={card.data.markdown} />
            </>
          ) : (
            <p class="body mute">{t('models.drawer.no_card')}</p>
          )}
        </section>
        {model?.last_used_at && <p class="meta">{t('models.drawer.last_used', { when: formatDate(Date.parse(model.last_used_at)) })}</p>}
      </div>
    </Sheet>
  );
}
