/**
 * Hand-written MCP value types. Kept out of the generated models.ts so `pnpm gen:api` can't drop them.
 */
import type { McpServer, Schemas } from './models';

/** A Keychain-held MCP `env`/`headers` value as the manager returns it. */
export type MaskedSecret = Schemas['McpSecretRef'];
/** Send a `MaskedSecret` back unchanged to keep a value, or a plain string to set a new one. */
export type McpValue = string | MaskedSecret;
/** `McpServer` with its `env`/`headers` narrowed to Keychain-backed values. */
export type McpServerView = Omit<McpServer, 'env' | 'headers'> & { env?: Record<string, McpValue>; headers?: Record<string, McpValue> };
