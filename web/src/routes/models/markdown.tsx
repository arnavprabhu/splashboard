/**
 * A tiny, safe Markdown renderer for model cards (03 §2). It builds Preact nodes (never
 * innerHTML), strips HF front matter and raw HTML, never loads images (the admin is offline,
 * SPEC §18.6: images become links), and opens links in a new tab. The full renderer (marked)
 * belongs to the Chat chunk and is not imported here.
 */

import type { ComponentChildren, VNode } from 'preact';

export type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'para'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'code'; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'table'; text: string }
  | { kind: 'rule' };

/** Removes a leading `---` YAML block (Hugging Face front matter). */
export function stripFrontMatter(md: string): string {
  const m = /^﻿?---\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)\s*(?:\r?\n|$)/.exec(md);
  return m ? md.slice(m[0].length) : md;
}

/** Drops HTML tags and comments; keeps their text content. */
export function stripHtml(text: string): string {
  return text.replace(/<!--[\s\S]*?-->/g, '').replace(/<\/?[a-zA-Z][^>]*>/g, '');
}

export function parseBlocks(md: string): Block[] {
  const lines = stripFrontMatter(md).replace(/\r\n?/g, '\n').split('\n');
  const out: Block[] = [];
  let i = 0;
  const isBlank = (l: string) => l.trim() === '';
  while (i < lines.length) {
    const line = lines[i]!;
    if (isBlank(line)) {
      i += 1;
      continue;
    }
    const fence = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i]!.trim().startsWith(fence[1]!)) body.push(lines[i++]!);
      i += 1;
      out.push({ kind: 'code', text: body.join('\n') });
      continue;
    }
    const heading = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      out.push({ kind: 'heading', level: heading[1]!.length, text: heading[2]! });
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push({ kind: 'rule' });
      i += 1;
      continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows: string[] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i]!)) rows.push(lines[i++]!.trim());
      out.push({ kind: 'table', text: rows.join('\n') });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i]!)) body.push(lines[i++]!.replace(/^\s*>\s?/, ''));
      out.push({ kind: 'quote', text: body.join(' ') });
      continue;
    }
    const bullet = /^\s*([-*+]|\d+[.)])\s+/;
    if (bullet.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && !isBlank(lines[i]!)) {
        const l = lines[i]!;
        if (bullet.test(l)) items.push(l.replace(bullet, ''));
        else if (items.length) items[items.length - 1] += ` ${l.trim()}`;
        i += 1;
      }
      out.push({ kind: 'list', ordered, items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && !isBlank(lines[i]!) && !/^\s*(#{1,6}\s|```|~~~|>|\|)/.test(lines[i]!)) para.push(lines[i++]!.trim());
    if (para.length === 0) {
      // A line that only looked like a block start; render it as text.
      para.push(lines[i++]!.trim());
    }
    out.push({ kind: 'para', text: para.join(' ') });
  }
  return out;
}

const SAFE_URL = /^(https?:\/\/|mailto:)/i;

export function safeHref(url: string, base = 'https://huggingface.co'): string | null {
  const u = url.trim();
  if (SAFE_URL.test(u)) return u;
  if (u.startsWith('/')) return `${base}${u}`;
  return null;
}

/** Inline: `code`, **bold**, *em*, [links](url), ![images](url) as links, autolinks. */
export function renderInline(text: string, repoBase?: string): ComponentChildren[] {
  const src = stripHtml(text);
  const out: ComponentChildren[] = [];
  const re = /(`[^`]+`)|(!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\))|(\*\*([^*]+)\*\*|__([^_]+)__)|(\*([^*\s][^*]*)\*|_([^_\s][^_]*)_)|(https?:\/\/[^\s)<]+)/g;
  let last = 0;
  let key = 0;
  for (let m = re.exec(src); m; m = re.exec(src)) {
    if (m.index > last) out.push(src.slice(last, m.index));
    if (m[1]) out.push(<code key={key++} class="mono">{m[1].slice(1, -1)}</code>);
    else if (m[2]) {
      const isImage = m[2].startsWith('!');
      const href = safeHref(m[4]!, repoBase);
      const label = m[3] || m[4]!;
      out.push(
        href ? (
          <a key={key++} href={href} target="_blank" rel="noreferrer noopener" class="md-link">
            {isImage ? `[${label}]` : label}
          </a>
        ) : (
          label
        ),
      );
    } else if (m[5]) out.push(<strong key={key++}>{m[6] ?? m[7]}</strong>);
    else if (m[8]) out.push(<em key={key++}>{m[9] ?? m[10]}</em>);
    else if (m[11]) {
      out.push(
        <a key={key++} href={m[11]} target="_blank" rel="noreferrer noopener" class="md-link">
          {m[11]}
        </a>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push(src.slice(last));
  return out;
}

export function Markdown({ source, repoBase }: { source: string; repoBase?: string }): VNode {
  const blocks = parseBlocks(source);
  return (
    <div class="md">
      {blocks.map((b, i) => {
        switch (b.kind) {
          case 'heading':
            return (
              <p key={i} class={b.level <= 2 ? 'label md-h' : 'body md-h'} role="heading" aria-level={Math.min(6, b.level + 2)}>
                {renderInline(b.text, repoBase)}
              </p>
            );
          case 'para':
            return (
              <p key={i} class="body">
                {renderInline(b.text, repoBase)}
              </p>
            );
          case 'list': {
            const Tag = b.ordered ? 'ol' : 'ul';
            return (
              <Tag key={i} class="body md-list">
                {b.items.map((it, j) => (
                  <li key={j}>{renderInline(it, repoBase)}</li>
                ))}
              </Tag>
            );
          }
          case 'code':
          case 'table':
            return (
              <pre key={i} class="mono md-pre" tabIndex={0}>
                {b.text}
              </pre>
            );
          case 'quote':
            return (
              <blockquote key={i} class="body md-quote">
                {renderInline(b.text, repoBase)}
              </blockquote>
            );
          case 'rule':
            return <hr key={i} class="md-rule" />;
          default:
            return null;
        }
      })}
    </div>
  );
}
