/** The model selector (docs/ui/07 §4): model and profile in one Menu, plus the switch notice. */
import { Button } from '../../components/Button';
import { Menu, type MenuGroup } from '../../components/Menu';
import { StatusChip } from '../../components/StatusChip';
import { formatTokens } from '../../lib/format';
import type { EngineState } from '../../api/types';
import { t } from '../../strings/chat';
import { requestModel, shortModel, type ModelRow } from './logic';

export interface ModelSelectorProps {
  rows: ModelRow[];
  /** Profiles per model (from `/v1/models` for the active one, `/models/{id}/profiles` for the pick). */
  profilesFor: (id: string) => string[];
  model: string | null;
  profile: string;
  onPick: (model: string, profile: string) => void;
  active: string | null;
  engineState: EngineState | null;
  autoLoad: boolean;
  onLoadNow: () => void;
  loading: boolean;
}

export function ModelSelector(p: ModelSelectorProps) {
  const stopped = !p.active || p.engineState === 'stopped';
  const groups: MenuGroup[] = [];
  const toItems = (rows: ModelRow[]) =>
    rows.flatMap((r) => {
      const detail = [
        r.context ? (r.estimated ? t('chat.model.ctx_est', { ctx: formatTokens(r.context) }) : t('chat.model.ctx', { ctx: formatTokens(r.context) })) : null,
        r.vision === null ? null : r.vision ? t('chat.model.vision') : t('chat.model.text_only'),
      ]
        .filter(Boolean)
        .join(' · ');
      const profiles = p.profilesFor(r.id).filter((x) => x !== 'default');
      return [
        {
          key: `${r.id}`,
          label: <span class="mono">{r.id}</span>,
          text: r.id,
          detail: detail || undefined,
          checked: p.model === r.id && p.profile === 'default',
          onSelect: () => p.onPick(r.id, 'default'),
        },
        ...profiles.map((prof) => ({
          key: `${r.id}:${prof}`,
          label: <span class="mono chat-model-profile">{t('chat.model.profile_item', { profile: prof })}</span>,
          text: `${r.id}:${prof}`,
          checked: p.model === r.id && p.profile === prof,
          onSelect: () => p.onPick(r.id, prof),
        })),
      ];
    });
  const activeRows = p.rows.filter((r) => r.active);
  const others = p.rows.filter((r) => !r.active);
  if (activeRows.length) groups.push({ label: t('chat.model.group.active'), items: toItems(activeRows) });
  if (others.length) groups.push({ label: stopped && !activeRows.length ? t('chat.model.group.stopped') : t('chat.model.group.installed'), items: toItems(others) });
  const value = p.model ? requestModel(p.model, p.profile).replace(/:([^/:]+)$/, ' : $1') : t('chat.model.choose');
  const row = p.rows.find((r) => r.id === p.model);
  const isActive = !!p.model && p.model === p.active;
  return (
    <div class="chat-model cluster">
      <span class="label">{t('chat.model.label')}</span>
      <Menu
        label={<span class="mono chat-model-value">{value}</span>}
        ariaLabel={t('chat.model.menu_label', { value })}
        groups={groups.length ? groups : [{ items: [{ key: 'none', label: t('chat.model.none'), disabled: true }] }]}
        radio
        variant="text"
        testId="chat-model"
      />
      <span class="meta cluster chat-model-meta">
        {isActive && <StatusChip state={p.engineState} announce={false} />}
        {row?.context ? <span class="tnum">{row.estimated ? t('chat.model.ctx_est', { ctx: formatTokens(row.context) }) : t('chat.model.ctx', { ctx: formatTokens(row.context) })}</span> : null}
        {row && row.vision !== null && <span>{row.vision ? t('chat.model.vision') : t('chat.model.text_only')}</span>}
        {p.model && !isActive && (stopped ? (
          p.autoLoad ? (
            <span>{t('chat.model.stopped')}</span>
          ) : (
            <span class="cluster">
              {t('chat.model.not_loaded')} ·
              <Button size="s" variant="text" loading={p.loading} onClick={p.onLoadNow}>
                {t('chat.model.load_now')}
              </Button>
            </span>
          )
        ) : p.autoLoad ? (
          <span class="acc" data-accent="true">
            {t('chat.model.will_switch', { active: shortModel(p.active), s: 2 })}
          </span>
        ) : (
          <span class="cluster">
            {t('chat.model.not_loaded')} ·
            <Button size="s" variant="text" loading={p.loading} onClick={p.onLoadNow}>
              {t('chat.model.load_now')}
            </Button>
          </span>
        ))}
      </span>
    </div>
  );
}
