/**
 * Model IDs are full Hugging Face IDs, OWNER/REPO[:VARIANT]. Validation
 * mirrors splash/install/models.py (REPO_ID, VARIANT, validate_repo_id,
 * split_model_id), including its error messages. Admin URLs put the ID in the path as
 * two segments: /admin/models/OWNER/REPO[:VARIANT]/settings.
 */

const REPO_ID = /^[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?\/[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9_])?$/;
const VARIANT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export const REPO_ID_ERROR = 'model must be a full Hugging Face repository ID (owner/repo)';
export const VARIANT_ERROR = 'model variant must be a short name such as UD-Q4_K_M (owner/repo:VARIANT)';

export function repoIdError(value: string): string | null {
  return REPO_ID.test(value) && !value.includes('--') && !value.includes('..') && !value.endsWith('.git')
    ? null
    : REPO_ID_ERROR;
}

/** Splash's message for an invalid OWNER/REPO[:VARIANT], or null when it is valid. */
export function modelIdError(value: string): string | null {
  const sep = value.indexOf(':');
  const repo = sep < 0 ? value : value.slice(0, sep);
  const repoError = repoIdError(repo);
  if (repoError) return repoError;
  if (sep < 0) return null;
  const variant = value.slice(sep + 1);
  return VARIANT.test(variant) && !variant.includes('..') ? null : VARIANT_ERROR;
}

export function isModelId(value: string): boolean {
  return modelIdError(value) === null;
}

export function splitModelId(value: string): { repo: string; variant: string | null } {
  const sep = value.indexOf(':');
  return sep < 0 ? { repo: value, variant: null } : { repo: value.slice(0, sep), variant: value.slice(sep + 1) };
}

function safeDecode(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

export function modelSettingsPath(id: string): string {
  const slash = id.indexOf('/');
  const owner = slash < 0 ? id : id.slice(0, slash);
  const repo = slash < 0 ? '' : id.slice(slash + 1);
  return `/models/${encodeURIComponent(owner)}/${encodeURIComponent(repo).replace(/%3A/gi, ':')}/settings`;
}

/** Accepts either {owner, repo} segments or a single encoded {id}. */
export function modelIdFromParams(params: Record<string, string | undefined>): string {
  if (params.owner && params.repo) return `${safeDecode(params.owner)}/${safeDecode(params.repo)}`;
  return safeDecode(params.id ?? '');
}
