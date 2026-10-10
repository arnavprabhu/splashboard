import { act, fireEvent, render, waitFor, within } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  status: null as Record<string, unknown> | null,
  release: null as null | (() => void),
  started: false,
  signal: null as AbortSignal | null,
}));

vi.mock('../src/lib/use-api', () => ({
  useApi: () => ({ data: { chats: [], data: [], profiles: [], ...(state.status ?? {}) }, error: null, loading: false, reload: vi.fn(async () => undefined), setData: vi.fn() }),
}));

// A reply that waits for the test to release it before streaming its only chunk.
vi.mock('../src/api/stream', () => ({
  postStream: async function* (_path: string, _body: unknown, opts: { signal?: AbortSignal }) {
    state.started = true;
    state.signal = opts.signal ?? null;
    await new Promise<void>((r) => {
      state.release = r;
    });
    yield { data: { choices: [{ delta: { content: 'Late reply from A' }, finish_reason: 'stop', index: 0 }] } };
  },
}));

import { api } from '../src/api/client';
import ChatPage from '../src/routes/chat';
import type { Chat } from '../src/routes/chat/types';

const chat = (id: string) =>
  ({
    id,
    title: `Chat ${id}`,
    model: 'o/model',
    profile: 'default',
    created_at: '2026-10-09T00:00:00Z',
    updated_at: '2026-10-09T00:00:00Z',
    system: '',
    tools: [],
    sampling: {},
    messages: [
      { id: `u-${id}`, role: 'user', parent: null, content: `Question ${id}` },
      { id: `a-${id}`, parent: `u-${id}`, role: 'assistant', content: `Answer ${id}` },
    ],
    active_leaf: `a-${id}`,
  }) as unknown as Chat;

const summary = (id: string) => ({ id, title: `Chat ${id}`, updated_at: '2026-10-09T00:00:00Z', created_at: '2026-10-09T00:00:00Z', message_count: 2, model: 'o/model' });

const pause = (ms: number) => act(async () => void (await new Promise((r) => setTimeout(r, ms))));
const contentOf = (doc: unknown) => JSON.stringify((doc as Chat).messages);

beforeEach(() => {
  state.status = null;
  state.release = null;
  state.started = false;
  state.signal = null;
  Object.defineProperty(window, 'innerWidth', { value: 1400, configurable: true });
  localStorage.setItem('chat.panel.open', 'true');
});

describe('chat route', () => {
  it('renders a multi-template status and picks the tool template once tools are defined', async () => {
    state.status = { chat_template: { later_system: { default: 'native', tool_use: 'patched' } } };
    const screen = render(<ChatPage />);
    fireEvent.click(screen.getByRole('tab', { name: 'System' }));
    expect(screen.queryByText(/through Splash’s template patch/)).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }));
    fireEvent.input(screen.getByLabelText('Tool definitions'), {
      target: { value: JSON.stringify([{ type: 'function', function: { name: 'get_time', parameters: { type: 'object', properties: {} } } }]) },
    });
    fireEvent.click(screen.getByRole('tab', { name: 'System' }));
    await waitFor(() => expect(screen.getByText(/through Splash’s template patch/)).toBeTruthy());
  });

  it('keeps a reply for the previous conversation out of the newly opened one and saves it to its own', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    const screen = render(<ChatPage params={{ cid: 'A' }} />);
    await waitFor(() => expect(screen.getByText('Answer A')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(state.started).toBe(true));
    screen.rerender(<ChatPage params={{ cid: 'B' }} />);
    await waitFor(() => expect(screen.getByText('Answer B')).toBeTruthy());
    // Opening B stops the reply still streaming into A.
    expect(state.signal?.aborted).toBe(true);
    await act(async () => state.release!());
    await waitFor(() => expect(put.mock.calls.some(([path, doc]) => path === '/chats/A' && contentOf(doc).includes('Late reply from A'))).toBe(true));
    expect(screen.getByText('Answer B')).toBeTruthy();
    expect(screen.queryByText('Late reply from A')).toBeNull();
    expect(put.mock.calls.some(([path, doc]) => path === '/chats/B' && contentOf(doc).includes('Late reply from A'))).toBe(false);
  });

  it('saves an edit made just before opening another conversation', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    const screen = render(<ChatPage params={{ cid: 'A' }} />);
    await waitFor(() => expect(screen.getByText('Answer A')).toBeTruthy());
    await pause(100);
    fireEvent.click(screen.getByRole('tab', { name: 'System' }));
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'Unsaved A system' } });
    screen.rerender(<ChatPage params={{ cid: 'B' }} />);
    await waitFor(() => expect(screen.getByText('Answer B')).toBeTruthy());
    await pause(100);
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'Saved B system' } });
    await waitFor(() => expect(put.mock.calls.some(([path, doc]) => path === '/chats/B' && (doc as Chat).system === 'Saved B system')).toBe(true), { timeout: 2000 });
    await pause(1500);
    expect(put.mock.calls.some(([path, doc]) => path === '/chats/A' && (doc as Chat).system === 'Unsaved A system')).toBe(true);
    expect(put.mock.calls.some(([path, doc]) => path === '/chats/B' && (doc as Chat).system === 'Unsaved A system')).toBe(false);
  });

  it('keeps one conversation’s queued save while another’s is in flight', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    let finish: (() => void) | null = null;
    const put = vi.spyOn(api, 'put').mockImplementation(async (path: string) => {
      if (path === '/chats/B' && !finish) await new Promise<void>((r) => (finish = r));
      return {} as never;
    });
    const screen = render(<ChatPage params={{ cid: 'B' }} />);
    await waitFor(() => expect(screen.getByText('Answer B')).toBeTruthy());
    await pause(100);
    fireEvent.click(screen.getByRole('tab', { name: 'System' }));
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'B in flight' } });
    await waitFor(() => expect(finish).not.toBeNull(), { timeout: 2000 });
    screen.rerender(<ChatPage params={{ cid: 'A' }} />);
    await waitFor(() => expect(screen.getByText('Answer A')).toBeTruthy());
    await pause(100);
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'A edit' } });
    screen.rerender(<ChatPage params={{ cid: 'B' }} />);
    await waitFor(() => expect(screen.getByText('Answer B')).toBeTruthy());
    await pause(100);
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'B queued' } });
    await act(async () => finish!());
    await waitFor(() => expect(put.mock.calls.some(([path, doc]) => path === '/chats/B' && (doc as Chat).system === 'B queued')).toBe(true), { timeout: 2000 });
    expect(put.mock.calls.some(([path, doc]) => path === '/chats/A' && (doc as Chat).system === 'A edit')).toBe(true);
  });
  it('keeps a message sent just before opening another conversation in its own, without starting a reply', async () => {
    state.status = { data: [{ id: 'o/model', loaded: true }] };
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    let create: (() => void) | null = null;
    vi.spyOn(api, 'post').mockImplementation(async (path: string) => {
      if (path !== '/chats') return {} as never;
      await new Promise<void>((r) => (create = r));
      return { id: 'N', created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z' } as never;
    });
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    const screen = render(<ChatPage />);
    fireEvent.input(screen.getByLabelText('Message'), { target: { value: 'Question for N' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(create).not.toBeNull());
    // The new conversation is still being saved when the user opens B.
    screen.rerender(<ChatPage params={{ cid: 'B' }} />);
    await waitFor(() => expect(screen.getByText('Answer B')).toBeTruthy());
    await act(async () => create!());
    await waitFor(() => expect(put.mock.calls.some(([path, doc]) => path === '/chats/N' && contentOf(doc).includes('Question for N'))).toBe(true), { timeout: 2000 });
    expect(state.started).toBe(false);
    expect(screen.getByText('Answer B')).toBeTruthy();
    expect(screen.queryByText('Question for N')).toBeNull();
    expect(put.mock.calls.some(([path, doc]) => path === '/chats/B' && contentOf(doc).includes('Question for N'))).toBe(false);
  });
  it('stops the reply of a conversation being deleted and never saves it again', async () => {
    state.status = { chats: [summary('A'), summary('B')] };
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    const calls: string[] = [];
    vi.spyOn(api, 'put').mockImplementation(async (path: string) => {
      calls.push(`PUT ${path}`);
      return {} as never;
    });
    vi.spyOn(api, 'del').mockImplementation(async (path: string) => {
      calls.push(`DELETE ${path}`);
      return undefined as never;
    });
    const screen = render(<ChatPage params={{ cid: 'A' }} />);
    await waitFor(() => expect(screen.getByText('Answer A')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(state.started).toBe(true));
    const list = within(screen.getByRole('navigation', { name: 'Conversations' }));
    fireEvent.click(list.getAllByRole('button', { name: 'Delete' })[0]!);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete chat' }));
    await waitFor(() => expect(calls).toContain('DELETE /chats/A'));
    expect(state.signal?.aborted).toBe(true);
    // The stopped reply finishes after the delete: its final save must not bring A back.
    await act(async () => state.release!());
    await pause(300);
    expect(calls.slice(calls.indexOf('DELETE /chats/A'))).not.toContain('PUT /chats/A');
  });

  it('waits for a save already on its way before deleting, and drops the edits that follow', async () => {
    state.status = { chats: [summary('A'), summary('B')] };
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => chat(path.endsWith('/B') ? 'B' : 'A') as never);
    const calls: string[] = [];
    let finish: (() => void) | null = null;
    vi.spyOn(api, 'put').mockImplementation(async (path: string) => {
      calls.push(`PUT ${path}`);
      if (!finish) await new Promise<void>((r) => (finish = r));
      calls.push(`PUT done ${path}`);
      return {} as never;
    });
    vi.spyOn(api, 'del').mockImplementation(async (path: string) => {
      calls.push(`DELETE ${path}`);
      return undefined as never;
    });
    const screen = render(<ChatPage params={{ cid: 'A' }} />);
    await waitFor(() => expect(screen.getByText('Answer A')).toBeTruthy());
    await pause(100);
    fireEvent.click(screen.getByRole('tab', { name: 'System' }));
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'first' } });
    await waitFor(() => expect(finish).not.toBeNull(), { timeout: 2000 });
    fireEvent.input(screen.getByLabelText('System prompt'), { target: { value: 'second' } });
    const list = within(screen.getByRole('navigation', { name: 'Conversations' }));
    fireEvent.click(list.getAllByRole('button', { name: 'Delete' })[0]!);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete chat' }));
    await pause(100);
    expect(calls).not.toContain('DELETE /chats/A');
    await act(async () => finish!());
    await waitFor(() => expect(calls).toContain('DELETE /chats/A'));
    await pause(1500);
    expect(calls.filter((c) => c === 'PUT /chats/A')).toHaveLength(1);
    expect(calls.indexOf('PUT done /chats/A')).toBeLessThan(calls.indexOf('DELETE /chats/A'));
  });
});
