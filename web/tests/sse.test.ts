import { describe, expect, it, vi } from 'vitest';
import { backoffDelay, subscribe } from '../src/api/sse';

class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }
  emit(name: string, data: string) {
    const e = { data, lastEventId: '' } as MessageEvent<string>;
    if (name === 'message') this.onmessage?.(e);
    else this.listeners.get(name)?.(e);
  }
  fail(closed: boolean) {
    this.readyState = closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.onerror?.();
  }
}

const Impl = FakeEventSource as unknown as typeof EventSource;

describe('backoffDelay', () => {
  it('doubles up to the cap', () => {
    expect(backoffDelay(1)).toBe(500);
    expect(backoffDelay(2)).toBe(1000);
    expect(backoffDelay(3)).toBe(2000);
    expect(backoffDelay(20)).toBe(15_000);
  });
});

describe('subscribe', () => {
  it('delivers default and named events as parsed JSON', () => {
    FakeEventSource.instances = [];
    const onMessage = vi.fn();
    const sub = subscribe('/api/admin/events', { events: ['engine'], onMessage, EventSourceImpl: Impl });
    const es = FakeEventSource.instances[0]!;
    es.open();
    expect(sub.connected).toBe(true);
    es.emit('engine', '{"state":"ready"}');
    es.emit('message', 'plain text');
    expect(onMessage).toHaveBeenNthCalledWith(1, { event: 'engine', data: { state: 'ready' }, id: '' });
    expect(onMessage).toHaveBeenNthCalledWith(2, { event: 'message', data: 'plain text', id: '' });
    sub.close();
    expect(es.closed).toBe(true);
  });

  it('reconnects with backoff once the browser gives up', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const onError = vi.fn();
    const sub = subscribe('/x', { onMessage: () => undefined, onError, EventSourceImpl: Impl, minDelayMs: 100 });
    FakeEventSource.instances[0]!.fail(true);
    expect(onError).toHaveBeenCalledWith(1);
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.advanceTimersByTime(100);
    expect(FakeEventSource.instances).toHaveLength(2);
    FakeEventSource.instances[1]!.fail(true);
    vi.advanceTimersByTime(199);
    expect(FakeEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(3);
    FakeEventSource.instances[2]!.open();
    sub.close();
    vi.advanceTimersByTime(10_000);
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it('lets the browser retry a transient drop, then takes over', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    subscribe('/x', { onMessage: () => undefined, EventSourceImpl: Impl, minDelayMs: 100 });
    const es = FakeEventSource.instances[0]!;
    es.fail(false);
    expect(es.closed).toBe(false);
    es.fail(false);
    expect(es.closed).toBe(true);
    vi.advanceTimersByTime(200);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it('does not reconnect after close', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const sub = subscribe('/x', { onMessage: () => undefined, EventSourceImpl: Impl, minDelayMs: 100 });
    FakeEventSource.instances[0]!.fail(true);
    sub.close();
    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
