/**
 * The composer: auto-growing textarea, attachment chips with page counts and
 * the 64 pages / 64 MiB limits, drag-and-drop and paste, SEND/STOP and the key rules.
 */
import { useEffect, useRef, useState } from 'preact/hooks';
import { Button } from '../../components/Button';
import { Tooltip } from '../../components/Tooltip';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/chat';
import { DEFAULT_MAX_IMAGE_PIXELS, attachmentError, countPdfPages } from './logic';
import { newId } from './tree';
import type { DraftAttachment } from './types';

const ACCEPT = 'image/png,image/jpeg,image/webp,image/gif,application/pdf';

function readDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

function imagePixels(url: string): Promise<number | null> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img.naturalWidth * img.naturalHeight);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

export async function toDraft(file: File, maxPixels = DEFAULT_MAX_IMAGE_PIXELS): Promise<DraftAttachment> {
  const kind = file.type === 'application/pdf' ? 'pdf' : 'image';
  const dataUrl = await readDataUrl(file);
  let pages: number | null = null;
  let resize = false;
  if (kind === 'pdf') pages = countPdfPages(new Uint8Array(await file.arrayBuffer()));
  else {
    const px = await imagePixels(dataUrl);
    resize = px !== null && px > maxPixels;
  }
  return { id: newId(), kind, name: file.name, mime: file.type, bytes: file.size, dataUrl, pages, resize };
}

export interface ComposerProps {
  value: string;
  onChange: (v: string) => void;
  attachments: DraftAttachment[];
  onAttachments: (a: DraftAttachment[]) => void;
  placeholder: string;
  streaming: boolean;
  /** Reason SEND is blocked (panel errors, no model…), or null. */
  blocked: string | null;
  /** Vision state of the selected model: false = language-only. */
  vision: boolean | null;
  editing: boolean;
  disabled?: boolean;
  onSend: () => void;
  onStop: () => void;
  onCancelEdit: () => void;
  onEditLast: () => void;
  onNotice: (text: string) => void;
}

export function Composer(p: ComposerProps) {
  const area = useRef<HTMLTextAreaElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [touch] = useState(() => typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches);
  const limit = attachmentError(p.attachments);
  const hasContent = p.value.trim().length > 0 || p.attachments.length > 0;
  const canSend = hasContent && !p.blocked && !limit && !p.disabled;
  const used = p.attachments.reduce((n, a) => n + a.bytes, 0);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    const line = 24;
    el.style.height = `${Math.min(el.scrollHeight, line * 10 + 20)}px`;
  }, [p.value]);

  async function add(files: File[]) {
    const out: DraftAttachment[] = [];
    for (const f of files) {
      if (!ACCEPT.split(',').includes(f.type)) {
        p.onNotice(t('chat.composer.unsupported_file', { name: f.name }));
        continue;
      }
      if (f.type !== 'application/pdf' && p.vision === false) {
        p.onNotice(t('chat.composer.paste_ignored'));
        continue;
      }
      try {
        out.push(await toDraft(f));
      } catch {
        p.onNotice(t('chat.composer.read_failed', { name: f.name }));
      }
    }
    if (out.length) p.onAttachments([...p.attachments, ...out]);
  }

  return (
    <div
      class="chat-composer stack"
      data-drag={drag ? 'true' : undefined}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        void add(Array.from(e.dataTransfer?.files ?? []));
      }}
    >
      {drag && <p class="label chat-drop">{p.vision === false ? t('chat.composer.drop_text_only') : t('chat.composer.drop')}</p>}
      {p.editing && (
        <p class="meta cluster">
          {t('chat.composer.editing')}
          <Button size="s" variant="text" onClick={p.onCancelEdit}>
            {t('chat.composer.edit_cancel')}
          </Button>
        </p>
      )}
      {p.attachments.length > 0 && (
        <div class="chat-chips-row">
          <ul class="chat-chips">
            {p.attachments.map((a) => (
              <li key={a.id} class="tag chat-chip">
                ▪ <span class="chat-chip-name">{a.name}</span>
                {a.kind === 'pdf' && ` · ${a.pages === null ? t('chat.att.pages_unknown') : a.pages === 1 ? t('chat.att.page') : t('chat.att.pages', { n: a.pages })}`}
                {` · ${formatBytes(a.bytes)}`}
                {a.resize && ` · ${t('chat.att.resize')}`}
                <button
                  type="button"
                  class="chat-chip-x"
                  aria-label={t('chat.composer.remove_attachment', { name: a.name })}
                  onClick={() => p.onAttachments(p.attachments.filter((x) => x.id !== a.id))}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          <span class="meta tnum">{t('chat.composer.counter', { n: p.attachments.length, used: (used / 1024 / 1024).toFixed(1) })}</span>
        </div>
      )}
      {limit && <p class="field-error">{limit}</p>}
      <div class="chat-composer-bar">
        <input
          ref={file}
          type="file"
          multiple
          accept={ACCEPT}
          class="visually-hidden"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => {
            void add(Array.from(e.currentTarget.files ?? []));
            e.currentTarget.value = '';
          }}
        />
        {p.vision === false ? (
          <Tooltip text={t('chat.composer.vision_off')}>
            <Button size="s" variant="text" class="chat-attach" aria-disabled="true" aria-label={`${t('chat.composer.attach')} · ${t('chat.composer.vision_off')}`}>
              {t('chat.composer.attach')}
            </Button>
          </Tooltip>
        ) : (
          <Button size="s" variant="text" class="chat-attach" onClick={() => file.current?.click()}>
            {t('chat.composer.attach')}
          </Button>
        )}
        <textarea
          ref={area}
          class="chat-input"
          rows={1}
          value={p.value}
          aria-label={t('chat.composer.label')}
          placeholder={touch ? p.placeholder : `${p.placeholder} ${t('chat.composer.hint')}`}
          disabled={p.disabled}
          onInput={(e) => p.onChange(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && p.streaming) {
              e.preventDefault();
              p.onStop();
            } else if (e.key === 'ArrowUp' && !p.value && !p.editing) {
              e.preventDefault();
              p.onEditLast();
            } else if (e.key === 'Enter' && !e.isComposing && ((e.metaKey || e.ctrlKey) || (!e.shiftKey && !touch))) {
              e.preventDefault();
              if (canSend && !p.streaming) p.onSend();
            }
          }}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData?.files ?? []);
            if (files.length) {
              e.preventDefault();
              void add(files);
            }
          }}
        />
        {p.streaming ? (
          <Button variant="solid" class="chat-send" onClick={p.onStop}>
            {t('chat.composer.stop')}
          </Button>
        ) : (
          <Button variant={canSend ? 'accent' : 'outline'} class="chat-send" disabled={!canSend} title={p.blocked ?? undefined} onClick={p.onSend}>
            {p.editing ? t('chat.msg.resend') : t('chat.composer.send')}
          </Button>
        )}
      </div>
    </div>
  );
}
