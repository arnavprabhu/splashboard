/**
 * Settings form state. One form for the global page lives at module
 * level so unsaved changes survive section changes and leaving the page; the per-model page
 * gets its own form per model ID. Validation runs client-side at once and through the manager
 * (debounced POST /settings/validate); its issues are mapped back onto fields.
 */

import { batch, computed, signal, type ReadonlySignal } from '@preact/signals';
import { ApiError } from '../../api/client';
import type { EffectiveSettings, SchemaField, SettingsSaveResult, SettingsSchema } from '../../api/models';
import type { SettingsDoc } from '../../api/types';
import { loadSettings } from '../../store';
import { readEnvelope, settingsApi, type SettingsEnvelope } from './api';
import {
  applyEdits,
  dirtyEdits,
  draftValue,
  issueRef,
  mapIssues,
  planSave,
  refId,
  storedValue,
  withEdit,
  type Edit,
  type EditValue,
  type FieldRef,
  type Issue,
  type MappedIssues,
  type SavePlan,
} from './form';
import { fieldError } from './validate';

export const VALIDATE_DEBOUNCE_MS = 350;

/** Schema shared by every form (fetched once per page load; `force` refreshes). */
export const schema = signal<SettingsSchema | null>(null);
export const schemaError = signal<unknown>(null);
let schemaRequest: Promise<SettingsSchema | null> | null = null;

export function loadSchema(force = false): Promise<SettingsSchema | null> {
  if (schema.value && !force) return Promise.resolve(schema.value);
  schemaRequest ??= settingsApi
    .schema()
    .then((s) => {
      schema.value = s;
      schemaError.value = null;
      return s;
    })
    .catch((err: unknown) => {
      schemaError.value = err;
      return null;
    })
    .finally(() => {
      schemaRequest = null;
    });
  return schemaRequest;
}

export const fieldsByKey = computed<Map<string, SchemaField>>(() => new Map((schema.value?.fields ?? []).map((f) => [f.key, f])));

export interface SettingsForm {
  model: string | null;
  envelope: ReadonlySignal<SettingsEnvelope | null>;
  loadError: ReadonlySignal<unknown>;
  effective: ReadonlySignal<EffectiveSettings | null>;
  edits: ReadonlySignal<ReadonlyMap<string, Edit>>;
  /** The document a save would send. */
  doc: ReadonlySignal<SettingsDoc | null>;
  dirty: ReadonlySignal<Edit[]>;
  issues: ReadonlySignal<MappedIssues>;
  /** Client-side errors by refId (dirty fields only). */
  clientErrors: ReadonlySignal<Map<string, string>>;
  invalid: ReadonlySignal<boolean>;
  plan: ReadonlySignal<SavePlan>;
  saving: ReadonlySignal<boolean>;
  validating: ReadonlySignal<boolean>;
  load: (force?: boolean) => Promise<void>;
  refreshEffective: () => Promise<void>;
  value: (ref: FieldRef) => unknown;
  stored: (ref: FieldRef) => unknown;
  isDirty: (ref: FieldRef) => boolean;
  errorsFor: (ref: FieldRef) => string[];
  warningsFor: (ref: FieldRef) => string[];
  set: (ref: FieldRef, value: EditValue) => void;
  discard: () => void;
  /** PUT /settings with the whole document; maps 422 issues onto fields and rethrows. */
  save: () => Promise<SettingsSaveResult>;
  /** Replaces the base after another route changed settings (secrets, MCP, presets). */
  adopt: (doc: SettingsDoc) => void;
}

export function createSettingsForm(model: string | null): SettingsForm {
  const envelope = signal<SettingsEnvelope | null>(null);
  const loadError = signal<unknown>(null);
  const effective = signal<EffectiveSettings | null>(null);
  const edits = signal<ReadonlyMap<string, Edit>>(new Map());
  const serverIssues = signal<Issue[]>([]);
  const saving = signal(false);
  const validating = signal(false);

  const base = computed(() => envelope.value?.settings ?? null);
  const doc = computed(() => (base.value ? applyEdits(base.value, edits.value) : null));
  const dirty = computed(() => dirtyEdits(base.value, edits.value));
  const knownKeys = computed(() => new Set(fieldsByKey.value.keys()));
  const issues = computed(() => mapIssues(serverIssues.value, knownKeys.value));
  const clientErrors = computed(() => {
    const out = new Map<string, string>();
    for (const e of dirty.value) {
      const field = fieldsByKey.value.get(e.ref.key);
      if (!field) continue;
      // Per-model "Reset to global" deletes the key: nothing to validate.
      if (draftValue(base.value, edits.value, e.ref) === undefined && e.ref.model) continue;
      const error = fieldError(field, draftValue(base.value, edits.value, e.ref));
      if (error) out.set(refId(e.ref), error);
    }
    return out;
  });
  const invalid = computed(() => {
    if (clientErrors.value.size > 0) return true;
    for (const entry of issues.value.byField.values()) if (entry.errors.length > 0) return true;
    return issues.value.other.some((i) => i.severity !== 'warning');
  });
  const issueRefOf = (i: Issue) => issueRef(i, knownKeys.value);
  const plan = computed(() => planSave(base.value, edits.value, (key) => fieldsByKey.value.get(key)?.applies));

  let timer: ReturnType<typeof setTimeout> | undefined;
  let ctrl: AbortController | null = null;

  const runValidate = async () => {
    ctrl?.abort();
    const current = doc.value;
    if (!current || dirty.value.length === 0) {
      serverIssues.value = [];
      validating.value = false;
      return;
    }
    const c = new AbortController();
    ctrl = c;
    validating.value = true;
    try {
      const result = await settingsApi.validate(current, c.signal);
      if (c.signal.aborted) return;
      serverIssues.value = [...(result.errors ?? []), ...(result.warnings ?? [])] as Issue[];
    } catch (err) {
      if (c.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
      // A failed validate never blocks editing; PUT validates again.
    } finally {
      if (!c.signal.aborted) validating.value = false;
    }
  };

  const scheduleValidate = () => {
    clearTimeout(timer);
    timer = setTimeout(() => void runValidate(), VALIDATE_DEBOUNCE_MS);
  };

  const refreshEffective = async () => {
    try {
      effective.value = await settingsApi.effective(model);
    } catch {
      /* source chips fall back to comparing with the schema default */
    }
  };

  const load = async (force = false) => {
    if (envelope.value && !force) return;
    try {
      const [env] = await Promise.all([settingsApi.get(), loadSchema(), refreshEffective()]);
      batch(() => {
        envelope.value = env;
        loadError.value = null;
      });
      if (dirty.value.length > 0) scheduleValidate();
    } catch (err) {
      loadError.value = err;
    }
  };

  const form: SettingsForm = {
    model,
    envelope,
    loadError,
    effective,
    edits,
    doc,
    dirty,
    issues,
    clientErrors,
    invalid,
    plan,
    saving,
    validating,
    load,
    refreshEffective,
    value: (ref) => draftValue(base.value, edits.value, ref),
    stored: (ref) => storedValue(base.value, ref),
    isDirty: (ref) => dirty.value.some((e) => refId(e.ref) === refId(ref)),
    errorsFor: (ref) => {
      const id = refId(ref);
      const client = clientErrors.value.get(id);
      const server = issues.value.byField.get(id)?.errors ?? [];
      return client ? [client] : server;
    },
    warningsFor: (ref) => issues.value.byField.get(refId(ref))?.warnings ?? [],
    set: (ref, value) => {
      const id = refId(ref);
      batch(() => {
        edits.value = withEdit(base.value, edits.value, ref, value);
        // The manager's verdict on the old value is stale; the debounced validate refreshes it.
        serverIssues.value = serverIssues.value.filter((i) => {
          const r = issueRefOf(i);
          return !r || refId(r) !== id;
        });
      });
      scheduleValidate();
    },
    discard: () => {
      clearTimeout(timer);
      ctrl?.abort();
      batch(() => {
        edits.value = new Map();
        serverIssues.value = [];
        validating.value = false;
      });
    },
    save: async () => {
      const current = doc.value;
      if (!current) throw new Error('settings not loaded');
      clearTimeout(timer);
      ctrl?.abort();
      saving.value = true;
      try {
        const result = await settingsApi.save(current);
        batch(() => {
          if (envelope.value) envelope.value = { ...envelope.value, settings: readEnvelope({ settings: result.settings }).settings };
          edits.value = new Map();
          serverIssues.value = (result.warnings ?? []) as Issue[];
        });
        void refreshEffective();
        void loadSettings(true);
        return result;
      } catch (err) {
        if (err instanceof ApiError && err.issues.length > 0) serverIssues.value = err.issues as Issue[];
        throw err;
      } finally {
        saving.value = false;
      }
    },
    adopt: (next) => {
      if (envelope.value) envelope.value = { ...envelope.value, settings: next };
    },
  };
  return form;
}

/** The global settings form (dirty state persists across sections). */
export const globalForm = createSettingsForm(null);

/** Per-model forms, one per model ID visited in this page load. */
const modelForms = new Map<string, SettingsForm>();
export function modelForm(id: string): SettingsForm {
  let form = modelForms.get(id);
  if (!form) modelForms.set(id, (form = createSettingsForm(id)));
  return form;
}

/** Restart-requiring changes saved with "Save, restart later". */
export const pendingRestart = signal<number>(0);

/** Reads an envelope refresh without losing edits (settings.changed from another client). */
export async function rebase(form: SettingsForm): Promise<void> {
  await form.load(true);
}
