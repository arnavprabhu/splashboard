import { useEffect, useRef, useState } from 'preact/hooks';

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall back below; clipboard API needs a secure context */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

export interface CopyButtonProps {
  text: string;
  label?: string;
  /** What is being copied, for screen readers. */
  what?: string;
}

/** Text button: COPY → COPIED. */
export function CopyButton({ text, label = 'Copy', what }: CopyButtonProps) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const onClick = async () => {
    const ok = await copyText(text);
    setState(ok ? 'copied' : 'failed');
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState('idle'), 1600);
  };
  const shown = state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label;
  return (
    <button
      type="button"
      class="btn"
      data-variant="text"
      data-size="s"
      onClick={onClick}
      aria-label={what ? `${shown} ${what}` : undefined}
      aria-live="polite"
    >
      {shown}
    </button>
  );
}
