/**
 * Every Splash route the Playground can call and the request
 * templates per endpoint. Routes were checked against splash/server/server.py
 * (do_GET / do_POST / do_DELETE); request fields against server/frontend.py and api_shapes.py.
 */

export type Method = 'GET' | 'POST' | 'DELETE';
export type EndpointGroup = 'openai' | 'anthropic' | 'tokenizer' | 'scoring' | 'health';
/** Which response shape a stream has (decides FIRST TOKEN and the PARSED assembly). */
export type Shape = 'chat' | 'completions' | 'responses' | 'messages' | 'other';

export type EndpointId =
  | 'chat'
  | 'completions'
  | 'responses'
  | 'responses_get'
  | 'responses_delete'
  | 'models'
  | 'model_get'
  | 'messages'
  | 'count_tokens'
  | 'tokenize'
  | 'apply_template'
  | 'judgments'
  | 'systemone'
  | 'status'
  | 'metrics'
  | 'health'
  | 'ready';

export interface Endpoint {
  id: EndpointId;
  method: Method;
  /** Path with `{id}` for the routes that take one. */
  path: string;
  group: EndpointGroup;
  /** GET/DELETE have no body; they may have a path parameter instead. */
  body: boolean;
  param?: 'response_id' | 'model_id';
  shape: Shape;
}

export const ENDPOINTS: readonly Endpoint[] = [
  { id: 'chat', method: 'POST', path: '/v1/chat/completions', group: 'openai', body: true, shape: 'chat' },
  { id: 'completions', method: 'POST', path: '/v1/completions', group: 'openai', body: true, shape: 'completions' },
  { id: 'responses', method: 'POST', path: '/v1/responses', group: 'openai', body: true, shape: 'responses' },
  { id: 'responses_get', method: 'GET', path: '/v1/responses/{id}', group: 'openai', body: false, param: 'response_id', shape: 'other' },
  { id: 'responses_delete', method: 'DELETE', path: '/v1/responses/{id}', group: 'openai', body: false, param: 'response_id', shape: 'other' },
  { id: 'models', method: 'GET', path: '/v1/models', group: 'openai', body: false, shape: 'other' },
  { id: 'model_get', method: 'GET', path: '/v1/models/{id}', group: 'openai', body: false, param: 'model_id', shape: 'other' },
  { id: 'messages', method: 'POST', path: '/v1/messages', group: 'anthropic', body: true, shape: 'messages' },
  { id: 'count_tokens', method: 'POST', path: '/v1/messages/count_tokens', group: 'anthropic', body: true, shape: 'other' },
  { id: 'tokenize', method: 'POST', path: '/tokenize', group: 'tokenizer', body: true, shape: 'other' },
  { id: 'apply_template', method: 'POST', path: '/apply-template', group: 'tokenizer', body: true, shape: 'other' },
  { id: 'judgments', method: 'POST', path: '/v1/judgments', group: 'scoring', body: true, shape: 'other' },
  { id: 'systemone', method: 'POST', path: '/v1/systemone', group: 'scoring', body: true, shape: 'other' },
  { id: 'status', method: 'GET', path: '/status', group: 'health', body: false, shape: 'other' },
  { id: 'metrics', method: 'GET', path: '/metrics', group: 'health', body: false, shape: 'other' },
  { id: 'health', method: 'GET', path: '/health', group: 'health', body: false, shape: 'other' },
  { id: 'ready', method: 'GET', path: '/ready', group: 'health', body: false, shape: 'other' },
];

export const GROUP_ORDER: readonly EndpointGroup[] = ['openai', 'anthropic', 'tokenizer', 'scoring', 'health'];

export function endpointById(id: string): Endpoint {
  return ENDPOINTS.find((e) => e.id === id) ?? ENDPOINTS[0]!;
}

export function endpointLabel(ep: Endpoint): string {
  return `${ep.method} ${ep.path}`;
}

/** Finds the endpoint for a method + concrete path (history rows). */
export function endpointFor(method: string, path: string): { endpoint: Endpoint; param: string } | null {
  for (const ep of ENDPOINTS) {
    if (ep.method !== method) continue;
    if (!ep.param) {
      if (ep.path === path) return { endpoint: ep, param: '' };
      continue;
    }
    const prefix = ep.path.slice(0, ep.path.indexOf('{id}'));
    if (path.startsWith(prefix) && path.length > prefix.length) return { endpoint: ep, param: path.slice(prefix.length) };
  }
  return null;
}

/** Splash's response IDs (`resp_` + [A-Za-z0-9_]+, server.py do_GET). */
export const RESPONSE_ID = /^resp_[A-Za-z0-9_]+$/;

/** The concrete path: `{id}` filled in. Model IDs keep their slashes (they are literal in paths). */
export function resolvePath(ep: Endpoint, param: string): string {
  if (!ep.param) return ep.path;
  const value = param.trim();
  const encoded = ep.param === 'model_id' ? value.split('/').map(encodeURIComponent).join('/').replace(/%3A/gi, ':') : encodeURIComponent(value);
  return ep.path.replace('{id}', encoded);
}

/** A problem with the path parameter, or null. */
export function paramError(ep: Endpoint, param: string): 'missing' | 'response_id' | null {
  if (!ep.param) return null;
  const value = param.trim();
  if (!value) return 'missing';
  if (ep.param === 'response_id' && !RESPONSE_ID.test(value)) return 'response_id';
  return null;
}

/** Whether a body asks for a stream (`stream: true`). */
export function wantsStream(body: unknown): boolean {
  return typeof body === 'object' && body !== null && (body as { stream?: unknown }).stream === true;
}

// ---------- templates ----------

export interface Template {
  id: string;
  /** String key under tools.template.* */
  label: string;
  build: (model: string) => Record<string, unknown>;
}

/** A 1×1 PNG, so the image template is valid without a real attachment. */
export const PNG_1X1 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
/** A placeholder PDF data URL; replace the base64 with a real document. */
export const PDF_PLACEHOLDER = 'JVBERi0xLjQKJcfsj6IKJSVFT0YK';

const TOOL = {
  type: 'function',
  function: {
    name: 'get_weather',
    description: 'Current weather for a city.',
    parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
  },
};

const ANTHROPIC_TOOL = {
  name: 'get_weather',
  description: 'Current weather for a city.',
  input_schema: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
};

const user = (content: unknown) => ({ role: 'user', content });

/** The SemIf example from DEVELOPMENT.md "Judgment contracts". */
export const SEMIF_EXAMPLE = {
  id: 'approval',
  state: 'The proposal is awaiting approval.',
  question: 'What is the current approval status?',
  options: [
    { id: 'approved', description: 'Approval was explicitly given.' },
    { id: 'pending', description: 'Approval has not been given.' },
  ],
};

/** The System One example from DEVELOPMENT.md "Judgment contracts" (typesafe-sdk 0.7.0). */
export const SYSTEMONE_EXAMPLE = {
  state: { message: 'I was charged twice. Please fix this today.' },
  questions: {
    billing: { type: 'noul', instructions: 'Is this about billing?' },
    department: {
      type: 'choice',
      instructions: 'Which team should handle this?',
      criteria: { billing: null, technical: null, sales: null },
    },
    urgency: { type: 'score', instructions: 'How urgent is the request?', criteria: ['No urgency', 'This week', 'Today'] },
  },
};

const TEMPLATES: Partial<Record<EndpointId, readonly Template[]>> = {
  chat: [
    { id: 'chat', label: 'tools.template.chat', build: (model) => ({ model, messages: [user('Write a haiku about Swiss posters.')], stream: true }) },
    {
      id: 'chat_tools',
      label: 'tools.template.chat_tools',
      build: (model) => ({ model, messages: [user('What is the weather in Zurich?')], tools: [TOOL], stream: true }),
    },
    {
      id: 'chat_image',
      label: 'tools.template.chat_image',
      build: (model) => ({
        model,
        messages: [
          user([
            { type: 'text', text: 'Describe this image.' },
            { type: 'image_url', image_url: { url: `data:image/png;base64,${PNG_1X1}` } },
          ]),
        ],
        stream: true,
      }),
    },
    {
      id: 'chat_pdf',
      label: 'tools.template.chat_pdf',
      build: (model) => ({
        model,
        messages: [
          user([
            { type: 'text', text: 'Summarise this document.' },
            { type: 'file', file: { file_data: `data:application/pdf;base64,${PDF_PLACEHOLDER}` } },
          ]),
        ],
        stream: true,
      }),
    },
    {
      id: 'json_schema',
      label: 'tools.template.json_schema',
      build: (model) => ({
        model,
        messages: [user('Give me a city and its country.')],
        response_format: {
          type: 'json_schema',
          json_schema: {
            name: 'place',
            schema: { type: 'object', properties: { city: { type: 'string' }, country: { type: 'string' } }, required: ['city', 'country'] },
          },
        },
      }),
    },
    {
      id: 'progress',
      label: 'tools.template.progress',
      build: (model) => ({
        model,
        messages: [user('Explain prefix caching in two sentences.')],
        stream: true,
        stream_options: { include_usage: true },
        return_progress: true,
      }),
    },
  ],
  completions: [
    { id: 'string', label: 'tools.template.string_prompt', build: (model) => ({ model, prompt: 'The capital of Switzerland is', max_tokens: 16 }) },
    { id: 'token_ids', label: 'tools.template.token_ids', build: (model) => ({ model, prompt: [785, 6722, 315], max_tokens: 16 }) },
  ],
  responses: [
    { id: 'basic', label: 'tools.template.responses_basic', build: (model) => ({ model, input: 'Say hello in three languages.', store: true }) },
    {
      id: 'continue',
      label: 'tools.template.responses_continue',
      build: (model) => ({ model, input: 'And in two more?', previous_response_id: 'resp_…', store: true }),
    },
    {
      id: 'reasoning',
      label: 'tools.template.responses_reasoning',
      build: (model) => ({ model, input: 'Is 1,001 prime?', reasoning: { effort: 'low' }, stream: true }),
    },
  ],
  messages: [
    { id: 'basic', label: 'tools.template.messages_basic', build: (model) => ({ model, max_tokens: 256, messages: [user('Hello!')] }) },
    {
      id: 'thinking',
      label: 'tools.template.messages_thinking',
      build: (model) => ({ model, max_tokens: 1024, thinking: { type: 'enabled', budget_tokens: 512 }, messages: [user('Is 1,001 prime?')], stream: true }),
    },
    {
      id: 'document',
      label: 'tools.template.messages_document',
      build: (model) => ({
        model,
        max_tokens: 512,
        messages: [
          user([
            { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: PDF_PLACEHOLDER } },
            { type: 'text', text: 'Summarise this document.' },
          ]),
        ],
      }),
    },
    {
      id: 'tools',
      label: 'tools.template.messages_tools',
      build: (model) => ({ model, max_tokens: 256, tools: [ANTHROPIC_TOOL], messages: [user('What is the weather in Zurich?')] }),
    },
  ],
  count_tokens: [
    { id: 'text', label: 'tools.template.count_text', build: (model) => ({ model, messages: [user('How many tokens is this?')] }) },
    {
      id: 'image',
      label: 'tools.template.count_image',
      build: (model) => ({
        model,
        messages: [
          user([
            { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG_1X1 } },
            { type: 'text', text: 'How many tokens is this?' },
          ]),
        ],
      }),
    },
  ],
  tokenize: [{ id: 'tokenize', label: 'tools.template.tokenize', build: (model) => ({ model, content: 'hello', add_special: false }) }],
  apply_template: [
    {
      id: 'apply_template',
      label: 'tools.template.apply_template',
      build: (model) => ({ model, messages: [{ role: 'system', content: 'You are terse.' }, user('Hello!')], add_generation_prompt: true }),
    },
  ],
  judgments: [{ id: 'semif', label: 'tools.template.semif', build: (model) => ({ model, ...SEMIF_EXAMPLE }) }],
  systemone: [{ id: 'systemone', label: 'tools.template.systemone', build: (model) => ({ model, ...SYSTEMONE_EXAMPLE }) }],
};

export function templatesFor(id: EndpointId): readonly Template[] {
  return TEMPLATES[id] ?? [];
}

/** The default body for an endpoint, pretty-printed; empty for routes without a body. */
export function defaultBody(id: EndpointId, model: string): string {
  const first = templatesFor(id)[0];
  return first ? JSON.stringify(first.build(model), null, 2) : '';
}

/** Rewrites only the `model` key of a JSON body; returns the text unchanged if it doesn't parse. */
export function withModel(text: string, model: string): string {
  try {
    const value = JSON.parse(text) as unknown;
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return text;
    return JSON.stringify({ ...(value as Record<string, unknown>), model }, null, 2);
  } catch {
    return text;
  }
}
