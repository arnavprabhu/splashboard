import type { ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { t } from '../strings/en';

export interface LogPaneLine {
  key: string | number;
  text: string;
  /** "error" lines get a bold weight and an accent ✕ prefix; "warn" a bold weight. */
  level?: 'debug' | 'info' | 'warn' | 'error' | undefined;
  ts?: string | null | undefined;
  /** Rendered instead of text (e.g. a session divider). */
  node?: ComponentChildren;
}

export interface LogPaneProps {
  lines: readonly LogPaneLine[];
  /** Accessible name ("Engine log"). */
  label: string;
  height?: number | 'auto';
  /** Show the timestamp column. */
  timestamps?: boolean;
  /** Text to highlight (search). */
  highlight?: string;
  empty?: ComponentChildren;
  /** Controlled follow state; when omitted the pane manages it and shows a Follow button. */
  follow?: boolean;
  onFollowChange?: (follow: boolean) => void;
}

function highlightText(text: string, needle: string | undefined): ComponentChildren {
  if (!needle) return text;
  const lower = text.toLowerCase();
  const n = needle.toLowerCase();
  const out: ComponentChildren[] = [];
  let i = 0;
  let j = lower.indexOf(n);
  while (j >= 0) {
    if (j > i) out.push(text.slice(i, j));
    out.push(<mark key={j}>{text.slice(j, j + needle.length)}</mark>);
    i = j + needle.length;
    j = lower.indexOf(n, i);
  }
  if (i < text.length) out.push(text.slice(i));
  return out;
}

/**
 * Monospace log output in a 1px rule box (installer output, engine log tail, trace replay,
 * live tail). Follows the bottom while `follow` is on; scrolling up turns follow off.
 */
export function LogPane({ lines, label, height = 320, timestamps, highlight, empty, follow: followProp, onFollowChange }: LogPaneProps) {
  const box = useRef<HTMLDivElement>(null);
  const [followState, setFollowState] = useState(true);
  const follow = followProp ?? followState;
  const setFollow = (v: boolean) => {
    if (followProp === undefined) setFollowState(v);
    onFollowChange?.(v);
  };
  const programmatic = useRef(false);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !follow) return;
    programmatic.current = true;
    el.scrollTop = el.scrollHeight;
  }, [lines, follow]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onScroll = () => {
      if (programmatic.current) {
        programmatic.current = false;
        return;
      }
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      if (atBottom !== follow) setFollow(atBottom);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  });

  return (
    <div class="logpane-wrap">
      <div
        ref={box}
        class="logpane mono"
        role="log"
        aria-label={label}
        aria-live="off"
        tabIndex={0}
        style={height === 'auto' ? undefined : { height: `${height}px` }}
      >
        {lines.length === 0 ? (
          <p class="meta logpane-empty">{empty ?? '—'}</p>
        ) : (
          lines.map((line) =>
            line.node ? (
              <div key={line.key} class="logline" data-kind="node">
                {line.node}
              </div>
            ) : (
              <div key={line.key} class="logline" data-level={line.level ?? 'info'}>
                {timestamps && line.ts && <span class="logline-ts">{line.ts}</span>}
                <span class="logline-text">{highlightText(line.text, highlight)}</span>
              </div>
            ),
          )
        )}
      </div>
      {followProp === undefined && !follow && lines.length > 0 && (
        <button type="button" class="btn logpane-follow" data-size="s" onClick={() => setFollow(true)}>
          {t('common.follow')} ↓
        </button>
      )}
    </div>
  );
}
