/**
 * The streamed compatibility check (D59). Its own module so the SSE reader stays out of the
 * initial chunk: the Status route imports ./api for the installed list (SPEC §18.6).
 */

import { ADMIN, ApiError, parseErrorBody, request } from '../../api/client';
import type { InspectResult } from '../../api/models';
import { readSse } from '../../api/stream';
import { readInspect } from './api';

export interface StreamInspectOptions {
  signal?: AbortSignal;
  refresh?: boolean;
  /** Each partial result (D59): unchecked variants have `loadable: null` and are in `pending`. */
  onProgress?: (partial: InspectResult) => void;
}

/**
 * GET /inspect/stream (D59): the likely recommended variant's verdict first, then the others as
 * Splash checks them. Resolves with the complete result (what GET /inspect returns). No client
 * timeout: the manager ends the stream with `inspect.error` when its own check times out.
 */
export async function streamInspect(id: string, options: StreamInspectOptions = {}): Promise<InspectResult> {
  const { signal, refresh = false, onProgress } = options;
  const res = await request<Response>(`${ADMIN}/inspect/stream`, {
    query: refresh ? { id, refresh: true } : { id },
    headers: { Accept: 'text/event-stream' },
    raw: true,
    ...(signal ? { signal } : {}),
  });
  for await (const evt of readSse(res, signal)) {
    let data: unknown;
    try {
      data = JSON.parse(evt.data);
    } catch {
      continue;
    }
    if (evt.event === 'inspect.error') {
      throw new ApiError(503, parseErrorBody(data, 503) ?? { message: 'The compatibility check failed', type: 'server_error', code: null }, data);
    }
    const result = readInspect(data);
    if (!result) continue;
    if (evt.event === 'inspect.result') return result;
    if (evt.event === 'inspect.progress') onProgress?.(result);
  }
  if (signal?.aborted) throw new DOMException('aborted', 'AbortError');
  throw new ApiError(502, { message: 'The compatibility check ended without a result', type: 'server_error', code: 'bad_response' });
}
