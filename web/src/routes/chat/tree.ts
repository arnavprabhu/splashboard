/**
 * The conversation tree (SPEC §15.2): messages link to their `parent`; regenerations and edits
 * add siblings; the visible thread is the path root → `active_leaf` (docs/ui/07 §8).
 */
import type { ChatMessage } from './types';

export function newId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

function createdKey(m: ChatMessage, index: number): [number, number] {
  const t = m.created_at ? Date.parse(m.created_at) : NaN;
  return [Number.isFinite(t) ? t : 0, index];
}

/** Children of `parentId` (null = roots), ordered by created_at, then by position. */
export function childrenOf(messages: readonly ChatMessage[], parentId: string | null): ChatMessage[] {
  const out: Array<{ m: ChatMessage; k: [number, number] }> = [];
  messages.forEach((m, i) => {
    if ((m.parent ?? null) === parentId) out.push({ m, k: createdKey(m, i) });
  });
  out.sort((a, b) => a.k[0] - b.k[0] || a.k[1] - b.k[1]);
  return out.map((x) => x.m);
}

export function siblingsOf(messages: readonly ChatMessage[], m: ChatMessage): ChatMessage[] {
  return childrenOf(messages, m.parent ?? null);
}

export function byId(messages: readonly ChatMessage[]): Map<string, ChatMessage> {
  return new Map(messages.map((m) => [m.id, m]));
}

/** Root → leaf. Unknown or cyclic links stop the walk. */
export function pathTo(messages: readonly ChatMessage[], leafId: string | null | undefined): ChatMessage[] {
  if (!leafId) return [];
  const map = byId(messages);
  const out: ChatMessage[] = [];
  const seen = new Set<string>();
  let cur = map.get(leafId);
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    out.push(cur);
    cur = cur.parent ? map.get(cur.parent) : undefined;
  }
  return out.reverse();
}

export function isDescendant(messages: readonly ChatMessage[], id: string, ancestorId: string): boolean {
  return pathTo(messages, id).some((m) => m.id === ancestorId);
}

/** Last-active leaf per subtree root, kept in memory (the file persists only active_leaf). */
export type LeafMemory = Map<string, string>;

/** Records `leaf` as the last-active leaf of every node on its path. */
export function rememberLeaf(memory: LeafMemory, messages: readonly ChatMessage[], leaf: string | null | undefined): void {
  if (!leaf) return;
  for (const m of pathTo(messages, leaf)) memory.set(m.id, leaf);
}

/** The leaf to show under `id`: the remembered one, else follow the newest child down. */
export function leafUnder(messages: readonly ChatMessage[], id: string, memory?: LeafMemory): string {
  const remembered = memory?.get(id);
  if (remembered && messages.some((m) => m.id === remembered) && isDescendant(messages, remembered, id)) return remembered;
  let cur = id;
  const seen = new Set<string>();
  for (;;) {
    if (seen.has(cur)) return cur;
    seen.add(cur);
    const kids = childrenOf(messages, cur);
    const next = kids[kids.length - 1];
    if (!next) return cur;
    cur = next.id;
  }
}

/** Default active leaf for a document without one: the newest path from the newest root. */
export function defaultLeaf(messages: readonly ChatMessage[]): string | null {
  const roots = childrenOf(messages, null);
  const root = roots[roots.length - 1];
  return root ? leafUnder(messages, root.id) : null;
}

export interface BranchInfo {
  index: number; // 1-based
  count: number;
  newest: boolean;
}

export function branchInfo(messages: readonly ChatMessage[], m: ChatMessage): BranchInfo {
  const sibs = siblingsOf(messages, m);
  const i = sibs.findIndex((s) => s.id === m.id);
  return { index: i + 1, count: sibs.length, newest: i === sibs.length - 1 };
}

/** The active leaf after moving `m` to its previous (-1) or next (+1) sibling, or null at an edge. */
export function switchBranch(messages: readonly ChatMessage[], m: ChatMessage, dir: -1 | 1, memory?: LeafMemory): string | null {
  const sibs = siblingsOf(messages, m);
  const i = sibs.findIndex((s) => s.id === m.id);
  const target = sibs[i + dir];
  return target ? leafUnder(messages, target.id, memory) : null;
}

export function descendantIds(messages: readonly ChatMessage[], id: string): Set<string> {
  const out = new Set<string>([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const m of messages) {
      if (m.parent && out.has(m.parent) && !out.has(m.id)) {
        out.add(m.id);
        grew = true;
      }
    }
  }
  return out;
}

/** Removes `id` and its subtree. The active leaf moves to the nearest sibling's leaf, else the parent. */
export function removeBranch(
  messages: readonly ChatMessage[],
  activeLeaf: string | null | undefined,
  id: string,
  memory?: LeafMemory,
): { messages: ChatMessage[]; activeLeaf: string | null; removed: number } {
  const target = messages.find((m) => m.id === id);
  if (!target) return { messages: [...messages], activeLeaf: activeLeaf ?? null, removed: 0 };
  const gone = descendantIds(messages, id);
  const rest = messages.filter((m) => !gone.has(m.id));
  let leaf = activeLeaf ?? null;
  if (!leaf || gone.has(leaf)) {
    const sibs = siblingsOf(messages, target);
    const i = sibs.findIndex((s) => s.id === id);
    const near = sibs[i - 1] ?? sibs[i + 1];
    if (near) leaf = leafUnder(rest, near.id, memory);
    else if (target.parent && rest.some((m) => m.id === target.parent)) leaf = target.parent;
    else leaf = defaultLeaf(rest);
  }
  if (memory) for (const g of gone) memory.delete(g);
  return { messages: rest, activeLeaf: leaf, removed: gone.size };
}

/** Number of messages below `id` (for "Delete this message and 4 replies?"). Tool results are folded in. */
export function replyCount(messages: readonly ChatMessage[], id: string): number {
  let n = 0;
  for (const g of descendantIds(messages, id)) {
    if (g === id) continue;
    const m = messages.find((x) => x.id === g);
    if (m && m.role !== 'tool') n += 1;
  }
  return n;
}
