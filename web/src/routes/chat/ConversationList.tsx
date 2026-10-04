/** The conversation list (docs/ui/07 §3): search, date groups, inline rename, export and delete. */
import { useEffect, useRef, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import { Button } from '../../components/Button';
import { Menu } from '../../components/Menu';
import { SearchInput } from '../../components/controls';
import { LoadError, Loading } from '../../components/States';
import { t } from '../../strings/chat';
import { groupChats, listTime } from './logic';
import type { ChatSummary } from './types';

export interface ConversationListProps {
  chats: ChatSummary[] | null;
  error: unknown;
  onRetry: () => void;
  query: string;
  onQuery: (q: string) => void;
  activeId: string | null;
  streamingId: string | null;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (c: ChatSummary) => void;
  onOpen?: () => void;
}

function Row({ c, active, streaming, onRename, onDelete, onOpen, snippet }: { c: ChatSummary; active: boolean; streaming: boolean; onRename: (title: string) => void; onDelete: () => void; onOpen?: () => void; snippet: boolean }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(c.title);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (editing) input.current?.select();
  }, [editing]);
  const commit = () => {
    setEditing(false);
    const v = title.trim();
    if (v && v !== c.title) onRename(v);
    else setTitle(c.title);
  };
  return (
    <li class="chat-row" data-active={active ? 'true' : undefined}>
      {editing ? (
        <input
          ref={input}
          class="input chat-rename"
          value={title}
          aria-label={t('chat.list.rename_label', { title: c.title })}
          onInput={(e) => setTitle(e.currentTarget.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit();
            else if (e.key === 'Escape') {
              e.stopPropagation();
              setTitle(c.title);
              setEditing(false);
            }
          }}
        />
      ) : (
        <Link
          href={`/chat/${encodeURIComponent(c.id)}`}
          class="chat-row-link"
          aria-current={active ? 'page' : undefined}
          aria-label={t('chat.list.open', { title: c.title })}
          onClick={onOpen}
        >
          <span class="chat-row-title">
            {c.title}
            {streaming && <span class="loading-dots" aria-label={t('chat.list.streaming')} />}
          </span>
          <span class="meta tnum chat-row-time">{listTime(c.updated_at)}</span>
          {snippet && c.snippet && <span class="meta chat-row-snippet">{c.snippet}</span>}
        </Link>
      )}
      <span class="chat-row-actions cluster" role="group" aria-label={t('chat.list.actions', { title: c.title })}>
        <Button size="s" variant="text" onClick={() => setEditing(true)}>
          {t('chat.list.rename')}
        </Button>
        <Menu
          label={t('chat.list.export')}
          variant="text"
          size="s"
          items={[
            { key: 'json', label: t('chat.list.export_json'), onSelect: () => location.assign(`/api/admin/chats/${encodeURIComponent(c.id)}/export?format=json`) },
            { key: 'md', label: t('chat.list.export_md'), onSelect: () => location.assign(`/api/admin/chats/${encodeURIComponent(c.id)}/export?format=md`) },
          ]}
        />
        <Button size="s" variant="text" onClick={onDelete}>
          {t('chat.list.delete')}
        </Button>
      </span>
    </li>
  );
}

export function ConversationList(p: ConversationListProps) {
  const searching = p.query.trim().length > 0;
  return (
    <nav class="chat-list stack" aria-label={t('chat.list.label')}>
      <h2 class="label">{t('chat.list.label')}</h2>
      <SearchInput value={p.query} onChange={p.onQuery} label={t('chat.list.search')} placeholder={t('chat.list.search_placeholder')} primary />
      <Button size="s" variant="text" onClick={p.onNew} class="chat-new">
        {t('chat.list.new')}
      </Button>
      {p.error ? (
        <LoadError thing={t('chat.load_chats')} error={p.error} onRetry={p.onRetry} />
      ) : p.chats === null ? (
        <Loading />
      ) : p.chats.length === 0 ? (
        <p class="meta">{searching ? t('chat.list.no_match', { q: p.query.trim() }) : t('chat.list.empty')}</p>
      ) : searching ? (
        <div>
          <h3 class="label mute">{t('chat.list.results', { n: p.chats.length })}</h3>
          <ul class="chat-rows">
            {p.chats.map((c) => (
              <Row key={c.id} c={c} snippet active={c.id === p.activeId} streaming={c.id === p.streamingId} onRename={(v) => p.onRename(c.id, v)} onDelete={() => p.onDelete(c)} onOpen={p.onOpen} />
            ))}
          </ul>
        </div>
      ) : (
        groupChats(p.chats).map((g) => (
          <div key={g.group}>
            <h3 class="label mute">{t(`chat.list.group.${g.group}`)}</h3>
            <ul class="chat-rows">
              {g.chats.map((c) => (
                <Row key={c.id} c={c} snippet={false} active={c.id === p.activeId} streaming={c.id === p.streamingId} onRename={(v) => p.onRename(c.id, v)} onDelete={() => p.onDelete(c)} onOpen={p.onOpen} />
              ))}
            </ul>
          </div>
        ))
      )}
    </nav>
  );
}
