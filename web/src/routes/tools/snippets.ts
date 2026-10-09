/**
 * "Copy as" generators: complete, runnable snippets built from the
 * current endpoint, body and auth. Keys are always environment variable references, never
 * values, and snippets always target the manager's public port, never the engine's internal
 * one.
 */

import type { Endpoint } from './endpoints';
import { wantsStream } from './endpoints';

export interface SnippetInput {
  /** The manager origin, e.g. http://127.0.0.1:8000. */
  origin: string;
  endpoint: Endpoint;
  /** Concrete path (`{id}` resolved). */
  path: string;
  /** Parsed body, or null for GET/DELETE. */
  body: unknown;
  /** Sent raw to the engine in the Playground. */
  raw?: boolean;
  /** `security.api_key_required`: reference $SPLASH_API_KEY. */
  auth?: boolean;
}

export type SnippetKind = 'curl' | 'python_openai' | 'python_anthropic' | 'js_fetch';

export const RAW_NOTE = 'Sent raw to the engine in the Playground; this snippet goes through the manager.';

/** Single-quotes a string for POSIX shells. */
export function shellQuote(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

/** A JSON value as a Python literal (True/False/None), pretty-printed with 4 spaces. */
export function toPython(value: unknown, indent = 4, base = 0): string {
  const json = JSON.stringify(value, null, indent);
  if (json === undefined) return 'None';
  const text = json.replace(/"(?:[^"\\]|\\.)*"|\btrue\b|\bfalse\b|\bnull\b/g, (m) =>
    m === 'true' ? 'True' : m === 'false' ? 'False' : m === 'null' ? 'None' : m,
  );
  if (!base) return text;
  const pad = ' '.repeat(base);
  return text
    .split('\n')
    .map((l, i) => (i === 0 ? l : pad + l))
    .join('\n');
}

const pyStr = (s: string) => JSON.stringify(s);

/** Whether the snippet kind applies to this endpoint (anthropic only for /v1/messages*). */
export function snippetAvailable(kind: SnippetKind, ep: Endpoint): boolean {
  if (kind === 'python_anthropic') return ep.id === 'messages' || ep.id === 'count_tokens';
  return true;
}

export function curlSnippet(input: SnippetInput): string {
  const { origin, endpoint, path, body, raw, auth } = input;
  const lines: string[] = [];
  if (raw) lines.push(`# ${RAW_NOTE}`);
  const parts: string[] = [];
  parts.push(endpoint.body ? `curl ${origin}${path}` : `curl -X ${endpoint.method} ${origin}${path}`);
  if (endpoint.body) parts.push(`-H 'Content-Type: application/json'`);
  if (auth) parts.push(`-H "Authorization: Bearer $SPLASH_API_KEY"`);
  if (endpoint.body && wantsStream(body)) parts.push('-N');
  if (endpoint.body) parts.push(`-d ${shellQuote(JSON.stringify(body ?? {}))}`);
  lines.push(parts.join(' \\\n  '));
  return lines.join('\n');
}

export function pythonOpenAiSnippet(input: SnippetInput): string {
  const { origin, endpoint, path, body, raw, auth } = input;
  const out: string[] = [];
  if (raw) out.push(`# ${RAW_NOTE}`);
  const viaHttpx = !path.startsWith('/v1/');
  if (viaHttpx) {
    out.push('# The OpenAI SDK has no method for this route; httpx is used instead.');
    if (auth) out.push('import os', '');
    out.push('import httpx', '');
    const headers = auth ? ', headers={"Authorization": f"Bearer {os.environ[\'SPLASH_API_KEY\']}"}' : '';
    if (endpoint.body) {
      out.push(`body = ${toPython(body ?? {})}`, '');
      out.push(`response = httpx.${endpoint.method.toLowerCase()}(${pyStr(origin + path)}, json=body${headers}, timeout=None)`);
    } else {
      out.push(`response = httpx.${endpoint.method.toLowerCase()}(${pyStr(origin + path)}${headers})`);
    }
    out.push(endpoint.id === 'metrics' ? 'print(response.text)' : 'print(response.json())');
    return out.join('\n');
  }
  if (endpoint.id === 'systemone' && body && typeof body === 'object') {
    const typesafe = typesafeSnippet({ origin, auth: !!auth, body: body as Record<string, unknown> });
    return raw ? `# ${RAW_NOTE}\n${typesafe}` : typesafe;
  }
  if (auth) out.push('import os', '');
  out.push('from openai import OpenAI', '');
  out.push(`client = OpenAI(base_url=${pyStr(`${origin}/v1`)}, api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'})`, '');
  const id = path.slice(path.lastIndexOf('/') + 1);
  const stream = wantsStream(body);
  const call = (expr: string) => {
    if (stream) out.push(`stream = ${expr}`, 'for event in stream:', '    print(event)');
    else out.push(`result = ${expr}`, 'print(result)');
  };
  switch (endpoint.id) {
    case 'chat':
      out.push(`body = ${toPython(body ?? {})}`, '');
      call('client.chat.completions.create(**body)');
      break;
    case 'completions':
      out.push(`body = ${toPython(body ?? {})}`, '');
      call('client.completions.create(**body)');
      break;
    case 'responses':
      out.push(`body = ${toPython(body ?? {})}`, '');
      call('client.responses.create(**body)');
      break;
    case 'responses_get':
      out.push(`print(client.responses.retrieve(${pyStr(decodeURIComponent(id))}))`);
      break;
    case 'responses_delete':
      out.push(`print(client.responses.delete(${pyStr(decodeURIComponent(id))}))`);
      break;
    case 'models':
      out.push('print(client.models.list())');
      break;
    case 'model_get':
      out.push(`print(client.models.retrieve(${pyStr(decodeURIComponent(path.slice('/v1/models/'.length)))}))`);
      break;
    default: {
      out.push('# The OpenAI SDK has no method for this route; client.post sends it as is.');
      const rel = path.slice('/v1'.length);
      if (endpoint.body) {
        out.push(`body = ${toPython(body ?? {})}`, '');
        out.push(`result = client.post(${pyStr(rel)}, body=body, cast_to=object)`);
      } else {
        out.push(`result = client.${endpoint.method === 'DELETE' ? 'delete' : 'get'}(${pyStr(rel)}, cast_to=object)`);
      }
      out.push('print(result)');
    }
  }
  return out.join('\n');
}

export function pythonAnthropicSnippet(input: SnippetInput): string | null {
  const { origin, endpoint, body, raw, auth } = input;
  if (!snippetAvailable('python_anthropic', endpoint)) return null;
  const out: string[] = [];
  if (raw) out.push(`# ${RAW_NOTE}`);
  if (auth) out.push('import os', '');
  out.push('import anthropic', '');
  out.push(`client = anthropic.Anthropic(base_url=${pyStr(origin)}, api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'})`, '');
  out.push(`body = ${toPython(body ?? {})}`, '');
  if (endpoint.id === 'count_tokens') {
    out.push('result = client.messages.count_tokens(**body)', 'print(result.input_tokens)');
  } else if (wantsStream(body)) {
    out.push('stream = client.messages.create(**body)', 'for event in stream:', '    print(event)');
  } else {
    out.push('message = client.messages.create(**body)', 'print(message)');
  }
  return out.join('\n');
}

export function jsFetchSnippet(input: SnippetInput): string {
  const { origin, endpoint, path, body, raw, auth } = input;
  const out: string[] = [];
  if (raw) out.push(`// ${RAW_NOTE}`);
  const headers: string[] = [];
  if (endpoint.body) headers.push(`'Content-Type': 'application/json'`);
  if (auth) headers.push('Authorization: `Bearer ${process.env.SPLASH_API_KEY}`');
  const init: string[] = [`  method: '${endpoint.method}',`];
  if (headers.length) init.push(`  headers: { ${headers.join(', ')} },`);
  if (endpoint.body) {
    const json = JSON.stringify(body ?? {}, null, 2)
      .split('\n')
      .map((l, i) => (i === 0 ? l : `  ${l}`))
      .join('\n');
    init.push(`  body: JSON.stringify(${json}),`);
  }
  out.push(`const res = await fetch('${origin}${path}', {`, ...init, '});');
  if (endpoint.body && wantsStream(body)) {
    out.push(
      'const reader = res.body.getReader();',
      'const decoder = new TextDecoder();',
      'for (;;) {',
      '  const { value, done } = await reader.read();',
      '  if (done) break;',
      '  console.log(decoder.decode(value, { stream: true }));',
      '}',
    );
  } else if (endpoint.id === 'metrics') {
    out.push('console.log(await res.text());');
  } else {
    out.push('console.log(res.status, await res.json());');
  }
  return out.join('\n');
}

/**
 * "Copy as typesafe-sdk Python" from a System One request body, matching
 * DEVELOPMENT.md "Judgment contracts" (typesafe-sdk 0.7.0): `Noul`, `Choice` (criteria map,
 * `None` descriptions) and `Score` (ordered level list).
 */
export function typesafeSnippet({ origin, auth, body }: { origin: string; auth: boolean; body: Record<string, unknown> }): string {
  const questions = (typeof body.questions === 'object' && body.questions !== null ? body.questions : {}) as Record<string, Record<string, unknown>>;
  const used = new Set<string>();
  const entries: string[] = [];
  for (const [key, q] of Object.entries(questions)) {
    const kind = q?.type === 'choice' ? 'Choice' : q?.type === 'score' ? 'Score' : 'Noul';
    used.add(kind);
    const args: string[] = [];
    if (q?.instructions !== undefined && q.instructions !== null) {
      args.push(`instructions=${typeof q.instructions === 'string' ? pyStr(q.instructions) : toPython(q.instructions, 4, 12)}`);
    }
    if (q?.criteria !== undefined && q.criteria !== null) args.push(`criteria=${toPython(q.criteria, 4, 12)}`);
    entries.push(`            ${pyStr(key)}: ${kind}(${args.join(', ')}),`);
  }
  const imports = [...used].sort().concat('TypeSafeClient').join(', ');
  const state = typeof body.state === 'string' ? pyStr(body.state) : toPython(body.state ?? '', 4, 8);
  const model = typeof body.model === 'string' ? body.model : '';
  const out: string[] = [];
  if (auth) out.push('import os', '');
  out.push(
    `from typesafe_sdk import ${imports}`,
    '',
    'with TypeSafeClient(',
    `    base_url=${pyStr(origin)},`,
    `    api_key=${auth ? 'os.environ["SPLASH_API_KEY"]' : '"local"'},`,
    `    model=${pyStr(model)},`,
    ') as client:',
    '    result = client.system_one(',
    `        state=${state},`,
    '        questions={',
    ...entries,
    '        },',
    '    )',
    '    print(result)',
  );
  return out.join('\n');
}

export function snippet(kind: SnippetKind, input: SnippetInput): string | null {
  switch (kind) {
    case 'curl':
      return curlSnippet(input);
    case 'python_openai':
      return pythonOpenAiSnippet(input);
    case 'python_anthropic':
      return pythonAnthropicSnippet(input);
    case 'js_fetch':
      return jsFetchSnippet(input);
  }
}
