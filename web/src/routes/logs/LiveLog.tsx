import type { ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { CopyButton } from '../../components/CopyButton';
import { Tag } from '../../components/Tag';
import { t } from '../../strings/logs';
import { fullTimeOf, splitMatches, TRUNCATE_AT, type Row } from './model';

export interface LiveLogProps {
  rows: readonly Row[];
  label: string;
  timestamps: boolean;
  wrap: boolean;
  query: string;
  /** Index into `rows` of the current search match, or -1. */
  current: number;
  /** Indices of every matching row. */
  matches: ReadonlySet<number>;
  follow: boolean;
  onFollowChange: (follow: boolean) => void;
  /** New lines since Follow turned off. */
  unseen: number;
  empty?: ComponentChildren;
  loading?: boolean;
}

/** Scrolling up by more than this turns Follow off (docs/ui/06 §2). */
const UNFOLLOW_PX = 40;

function Highlighted({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  return (
    <>
      {splitMatches(text, query).map((part, i) => (part.match ? <mark key={i}>{part.text}</mark> : part.text))}
    </>
  );
}

function DividerRow({ row, query }: { row: Row; query: string }) {
  const d = row.divider!;
  if (d.kind === 'stop') {
    const parts = [d.reason, d.exit !== null ? t('logs.divider.exit', { code: d.exit }) : null].filter(Boolean).join(' · ');
    return (
      <div class="logs-divider" data-kind="stop">
        <span class="label">
          {row.clock && <span class="tnum logs-divider-time">{row.clock} </span>}
          {t('logs.divider.stop')}
          {parts && <span class="mono"> · {parts}</span>}
        </span>
      </div>
    );
  }
  return (
    <div class="logs-divider" data-kind="start">
      <div class="logs-divider-head">
        <span class="label">
          {row.clock && <span class="tnum logs-divider-time">{row.clock} </span>}
          {t('logs.divider.start')}
          {d.model && <span class="mono"> · {d.model}</span>}
        </span>
        {d.command && <CopyButton text={d.command} what={t('logs.divider.copy_what')} />}
      </div>
      {d.command && (
        <code class="mono logs-divider-cmd">
          <Highlighted text={d.command} query={query} />
        </code>
      )}
    </div>
  );
}

function LineText({ row, query }: { row: Row; query: string }) {
  const [all, setAll] = useState(false);
  const long = row.text.length > TRUNCATE_AT && !all;
  const text = long ? row.text.slice(0, TRUNCATE_AT) : row.text;
  return (
    <span class="logs-text">
      {row.tag && (
        <>
          <Tag tone="mute">{t(`logs.tag.${row.tag}`)}</Tag>{' '}
        </>
      )}
      <Highlighted text={text} query={query} />
      {long && (
        <>
          {'… '}
          <button type="button" class="btn" data-variant="text" data-size="s" onClick={() => setAll(true)}>
            {t('logs.show_all')}
          </button>
        </>
      )}
    </span>
  );
}

/**
 * The live tail pane (docs/ui/06 §1, §3): one inner scroller with `role="log"`, rows of
 * time · level · text, session dividers between 2px rules, search highlights, Follow
 * (pinned to the bottom; scrolling up > 40px turns it off; End re-follows; Home goes to the
 * top; Space toggles while the pane has focus) and a `▼ N NEW LINES` button.
 */
export function LiveLog({ rows, label, timestamps, wrap, query, current, matches, follow, onFollowChange, unseen, empty, loading }: LiveLogProps) {
  const box = useRef<HTMLDivElement>(null);
  const programmatic = useRef(false);
  const followRef = useRef(follow);
  followRef.current = follow;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !follow) return;
    programmatic.current = true;
    el.scrollTop = el.scrollHeight;
  }, [rows, follow]);

  useEffect(() => {
    if (current < 0) return;
    const el = box.current?.querySelector<HTMLElement>(`[data-index="${current}"]`);
    if (!el) return;
    el.scrollIntoView({ block: 'center' });
  }, [current]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onScroll = () => {
      if (programmatic.current) {
        programmatic.current = false;
        return;
      }
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (followRef.current && distance > UNFOLLOW_PX) onFollowChange(false);
      else if (!followRef.current && distance <= 2) onFollowChange(true);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [onFollowChange]);

  const onKeyDown = (e: KeyboardEvent) => {
    const el = box.current;
    if (!el || e.target !== el) return;
    if (e.key === 'End') {
      e.preventDefault();
      onFollowChange(true);
    } else if (e.key === 'Home') {
      e.preventDefault();
      onFollowChange(false);
      programmatic.current = true;
      el.scrollTop = 0;
    } else if (e.key === ' ') {
      e.preventDefault();
      onFollowChange(!follow);
    }
  };

  // A polite summary every 10 s instead of announcing each line (docs/ui/06 §3).
  const [announce, setAnnounce] = useState('');
  const announced = useRef(rows.length);
  useEffect(() => {
    const id = setInterval(() => {
      const n = rows.length - announced.current;
      announced.current = rows.length;
      setAnnounce(n > 0 ? t('logs.new_lines', { n }) : '');
    }, 10_000);
    return () => clearInterval(id);
  }, [rows.length]);

  return (
    <div class="logs-pane-wrap">
      <div
        ref={box}
        class="logs-pane mono"
        role="log"
        aria-label={label}
        aria-live="off"
        aria-busy={loading ? 'true' : undefined}
        tabIndex={0}
        data-wrap={String(wrap)}
        data-timestamps={String(timestamps)}
        data-testid="log-pane"
        onKeyDown={onKeyDown}
      >
        {rows.length === 0 ? (
          <div class="logs-empty">{empty}</div>
        ) : (
          rows.map((row, i) => {
            if (row.meta) {
              return (
                <div key={`m${row.key}-${i}`} class="logs-meta meta" data-index={i}>
                  {t(`logs.meta.${row.meta}`)}
                </div>
              );
            }
            if (row.divider) {
              return (
                <div key={row.key} data-index={i} data-match={matches.has(i) ? 'true' : undefined} data-current={i === current ? 'true' : undefined}>
                  <DividerRow row={row} query={query} />
                </div>
              );
            }
            return (
              <div
                key={row.key}
                class="logs-row"
               
                data-index={i}
                data-level={row.level}
                data-match={matches.has(i) ? 'true' : undefined}
                data-current={i === current ? 'true' : undefined}
              >
                {timestamps && (
                  <span class="logs-time tnum" title={fullTimeOf(row.ts) ?? undefined}>
                    {row.clock ?? ''}
                  </span>
                )}
                <span class="logs-level label">{t(`logs.level.${row.level}`)}</span>
                <LineText row={row} query={query} />
              </div>
            );
          })
        )}
      </div>
      {!follow && unseen > 0 && (
        <button type="button" class="btn logs-unseen" data-variant="solid" data-size="s" onClick={() => onFollowChange(true)}>
          ▼ {t('logs.new_lines', { n: unseen })}
        </button>
      )}
      <span class="visually-hidden" aria-live="polite">
        {announce}
      </span>
    </div>
  );
}
