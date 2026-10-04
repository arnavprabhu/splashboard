/**
 * Judgments builder logic (docs/ui/08 §3): System One questions ⇄ request body, validation and
 * result shaping, and the SemIf row. Shapes verified in splash/server/judgments.py
 * (`validate_systemone`, `systemone_answer`, `validate_row`, `judgment_response`).
 */
import { t } from '../../strings/tools';

export type QType = 'noul' | 'choice' | 'score';

export interface Question {
  uid: string;
  type: QType;
  key: string;
  instructions: string;
  /** Choice labels with optional descriptions. */
  labels: Array<{ label: string; description: string }>;
  /** Score levels, low → high. */
  levels: string[];
  /** Noul criteria (optional). */
  noulTrue: string;
  noulFalse: string;
}

export const MAX_QUESTIONS = 64;
export const MAX_DOMAIN = 255;
export const MIN_OPTIONS = 2;
export const MAX_OPTIONS = 16;

let seq = 0;
export const uid = () => `q${++seq}`;

export function newQuestion(type: QType, taken: readonly string[]): Question {
  let n = taken.length + 1;
  let key = `${type}_${n}`;
  while (taken.includes(key)) key = `${type}_${++n}`;
  return {
    uid: uid(),
    type,
    key,
    instructions: '',
    labels: type === 'choice' ? [{ label: 'yes', description: '' }, { label: 'no', description: '' }] : [],
    levels: type === 'score' ? ['low', 'medium', 'high'] : [],
    noulTrue: '',
    noulFalse: '',
  };
}

export function questionsFromBody(body: unknown): Question[] | null {
  if (!body || typeof body !== 'object') return null;
  const qs = (body as { questions?: unknown }).questions;
  if (!qs || typeof qs !== 'object' || Array.isArray(qs)) return null;
  const out: Question[] = [];
  for (const [key, raw] of Object.entries(qs as Record<string, unknown>)) {
    if (!raw || typeof raw !== 'object') return null;
    const q = raw as Record<string, unknown>;
    const type = q.type;
    if (type !== 'noul' && type !== 'choice' && type !== 'score') return null;
    if (q.instructions !== undefined && typeof q.instructions !== 'string') return null;
    const base: Question = { uid: uid(), type, key, instructions: (q.instructions as string | undefined) ?? '', labels: [], levels: [], noulTrue: '', noulFalse: '' };
    const c = q.criteria;
    if (type === 'choice') {
      if (!c || typeof c !== 'object' || Array.isArray(c)) return null;
      for (const [label, d] of Object.entries(c as Record<string, unknown>)) {
        if (d !== null && typeof d !== 'string') return null;
        base.labels.push({ label, description: (d as string | null) ?? '' });
      }
    } else if (type === 'score') {
      if (!Array.isArray(c) || !c.every((x) => typeof x === 'string')) return null;
      base.levels = [...(c as string[])];
    } else if (c !== undefined && c !== null) {
      if (typeof c !== 'object' || Array.isArray(c)) return null;
      const cc = c as Record<string, unknown>;
      if ((cc.true !== undefined && typeof cc.true !== 'string') || (cc.false !== undefined && typeof cc.false !== 'string')) return null;
      base.noulTrue = (cc.true as string | undefined) ?? '';
      base.noulFalse = (cc.false as string | undefined) ?? '';
    }
    out.push(base);
  }
  return out;
}

export function questionBody(q: Question): Record<string, unknown> {
  const out: Record<string, unknown> = { type: q.type };
  if (q.instructions.trim()) out.instructions = q.instructions;
  if (q.type === 'choice') out.criteria = Object.fromEntries(q.labels.map((l) => [l.label, l.description.trim() ? l.description : null]));
  if (q.type === 'score') out.criteria = [...q.levels];
  if (q.type === 'noul' && (q.noulTrue.trim() || q.noulFalse.trim())) out.criteria = { true: q.noulTrue, false: q.noulFalse };
  return out;
}

export function systemOneBody(model: string, state: unknown, qs: readonly Question[]): Record<string, unknown> {
  return { model, state, questions: Object.fromEntries(qs.map((q) => [q.key, questionBody(q)])) };
}

/** Per-question problems keyed by uid (key, domain sizes, duplicate labels). */
export function questionErrors(qs: readonly Question[]): Record<string, string> {
  const out: Record<string, string> = {};
  const seen = new Map<string, number>();
  for (const q of qs) seen.set(q.key.trim(), (seen.get(q.key.trim()) ?? 0) + 1);
  for (const q of qs) {
    if (!q.key.trim()) out[q.uid] = t('tools.jd.key_empty');
    else if ((seen.get(q.key.trim()) ?? 0) > 1) out[q.uid] = t('tools.jd.key_dup');
    else if (q.type === 'choice') {
      const labels = q.labels.map((l) => l.label.trim());
      if (labels.length < 1 || labels.length > MAX_DOMAIN) out[q.uid] = t('tools.jd.domain');
      else if (labels.some((l) => !l)) out[q.uid] = t('tools.jd.label_empty');
      else if (new Set(labels).size !== labels.length) out[q.uid] = t('tools.jd.label_dup');
    } else if (q.type === 'score') {
      if (q.levels.length < 1 || q.levels.length > MAX_DOMAIN) out[q.uid] = t('tools.jd.domain');
      else if (q.levels.some((l) => !l.trim())) out[q.uid] = t('tools.jd.label_empty');
    }
  }
  return out;
}

/** Maps a 422 `detail[].loc` (["body","questions","department","criteria"]) onto a question key. */
export function detailKey(loc: unknown): string | null {
  if (!Array.isArray(loc)) return null;
  const i = loc.indexOf('questions');
  return i >= 0 && typeof loc[i + 1] === 'string' ? (loc[i + 1] as string) : null;
}

export interface Bar {
  label: string;
  p: number;
  winner: boolean;
}

export interface AnswerView {
  head: string;
  confidence: number | null;
  bars: Bar[];
  noInference: boolean;
}

function winnerBars(entries: Array<[string, number]>): Bar[] {
  const max = Math.max(...entries.map((e) => e[1]));
  let marked = false;
  return entries.map(([label, p]) => {
    const winner = !marked && p === max;
    if (winner) marked = true;
    return { label, p, winner };
  });
}

/** One answer from `/v1/systemone` (`answers[key]`) as the header line and bars (D-08-4, D-08-5). */
export function answerView(q: Question | undefined, a: Record<string, unknown>): AnswerView {
  const conf = typeof a.confidence === 'number' ? a.confidence : null;
  if (a.type === 'noul') {
    const pTrue = typeof a.noul === 'number' ? a.noul : 0;
    return {
      head: t('tools.jd.answer_v', { v: pTrue >= 0.5 ? 'true' : 'false' }),
      confidence: null,
      bars: winnerBars([
        ['true', pTrue],
        ['false', 1 - pTrue],
      ]),
      noInference: false,
    };
  }
  const probs = (a.probabilities ?? {}) as Record<string, number>;
  if (a.type === 'choice') {
    const entries = Object.entries(probs);
    return {
      head: t('tools.jd.answer_v', { v: String(a.choice ?? '') }),
      confidence: conf,
      bars: winnerBars(entries),
      noInference: entries.length === 1,
    };
  }
  const legend = Array.isArray(a.legend) ? (a.legend as string[]) : (q?.levels ?? []);
  const entries = Object.entries(probs)
    .sort((x, y) => Number(x[0]) - Number(y[0]))
    .map(([i, p]) => [`${Number(i) + 1} ${legend[Number(i)] ?? ''}`.trim(), p] as [string, number]);
  const score = typeof a.score === 'number' ? a.score + 1 : null;
  return {
    head: t('tools.jd.score_v', { v: score === null ? '—' : score.toFixed(2), k: entries.length }),
    confidence: conf,
    bars: winnerBars(entries),
    noInference: entries.length === 1,
  };
}

// ---------- SemIf ----------

export const LETTERS = 'ABCDEFGHIJKLMNOP';

export interface SemifRow {
  id: string;
  state: string;
  question: string;
  options: Array<{ id: string; description: string }>;
}

export function semifErrors(r: SemifRow): string | null {
  if (r.options.length < MIN_OPTIONS) return t('tools.jd.min_options');
  const ids = r.options.map((o) => o.id.trim());
  if (ids.some((x) => !x) || new Set(ids).size !== ids.length) return t('tools.jd.option_dup');
  return null;
}

export function semifBody(model: string, r: SemifRow): Record<string, unknown> {
  return { model, id: r.id, state: r.state, question: r.question, options: r.options.map((o) => ({ id: o.id, description: o.description })) };
}

/** First 4 and last 2 hex digits of a hash. */
export function shortHash(h: string): string {
  return h.length > 8 ? `${h.slice(0, 4)}…${h.slice(-2)}` : h;
}
