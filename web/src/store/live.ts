/**
 * Shared, ref-counted subscription to GET /api/admin/metrics/live and cached
 * one-shot lookups (system, versions). Status, the nav tok/s and Benchmark share
 * one EventSource; it closes when the last user unsubscribes.
 */

import { signal } from '@preact/signals';
import { useEffect, useRef } from 'preact/hooks';
import { api } from '../api/client';
import type { LiveMetrics, SystemInfo, Versions } from '../api/models';
import { subscribe, type Subscription } from '../api/sse';

export const liveSample = signal<LiveMetrics | null>(null);
/** Wall-clock ms when the last sample arrived (stale detection: > 5 s). */
export const liveSampleAt = signal<number | null>(null);

type SampleListener = (sample: LiveMetrics) => void;
const sampleListeners = new Set<SampleListener>();
let users = 0;
let sub: Subscription | null = null;

function isSample(v: unknown): v is LiveMetrics {
  return typeof v === 'object' && v !== null && typeof (v as { t?: unknown }).t === 'number';
}

function open(): void {
  sub = subscribe<unknown>('/api/admin/metrics/live', {
    events: ['snapshot'],
    onMessage: ({ data }) => {
      if (!isSample(data)) return;
      liveSample.value = data;
      liveSampleAt.value = Date.now();
      for (const fn of sampleListeners) fn(data);
    },
  });
}

/** Starts (or joins) the live stream; returns a release function. */
export function retainLiveMetrics(): () => void {
  users += 1;
  if (users === 1) open();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    users -= 1;
    if (users === 0) {
      sub?.close();
      sub = null;
    }
  };
}

/** Subscribes to every live sample while mounted (and keeps the stream open). */
export function useLiveMetrics(onSample?: SampleListener, enabled = true): void {
  const ref = useRef(onSample);
  ref.current = onSample;
  useEffect(() => {
    if (!enabled) return;
    const release = retainLiveMetrics();
    const fn: SampleListener = (s) => ref.current?.(s);
    sampleListeners.add(fn);
    return () => {
      sampleListeners.delete(fn);
      release();
    };
  }, [enabled]);
}

// ---------- cached lookups ----------

function cached<T>(path: string, method: 'GET' | 'POST' = 'GET'): { value: ReturnType<typeof signal<T | null>>; load: (force?: boolean) => Promise<T | null> } {
  const value = signal<T | null>(null);
  let pending: Promise<T | null> | null = null;
  const load = (force = false) => {
    if (value.value && !force) return Promise.resolve(value.value);
    pending ??= (method === 'POST' ? api.read<T>(path) : api.get<T>(path))
      .then((v) => (value.value = v))
      .catch(() => value.value)
      .finally(() => {
        pending = null;
      });
    return pending;
  };
  return { value, load };
}

const systemCache = cached<SystemInfo>('/system', 'POST');
const versionsCache = cached<Versions>('/versions');

/** POST /system (was GET; chip, RAM, disks, power), fetched once per page load. */
export const systemInfo = systemCache.value;
export const loadSystem = systemCache.load;
/** GET /versions (gui, manager, engine discovery, engine update). */
export const versions = versionsCache.value;
export const loadVersions = versionsCache.load;
