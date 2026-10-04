/**
 * The string table (docs/ui/00 §8.2, SPEC §2 "strings in one table").
 *
 * One namespace of dotted keys (`<area>.<element>.<variant>`), `{brace}` placeholders and
 * `.one` / `.other` plurals selected by the `n` param. Common and shell strings live in
 * ./en.ts; each page area has its own module (./status.ts, ./models.ts, …) so lazily loaded
 * routes do not pull every page's strings into the initial bundle (SPEC §18.6). Keys are
 * unique across all modules (tests/strings.test.ts).
 */

export type Params = Record<string, string | number | null | undefined>;
export type Table = Readonly<Record<string, string>>;

type PluralBase<K extends string> = K extends `${infer B}.one` ? B : never;
/** A key of the table, or the base of a `.one` / `.other` pair. */
export type Key<T extends Table> = (keyof T & string) | PluralBase<keyof T & string>;

/** Replaces `{name}` placeholders; unknown placeholders are left as written. */
export function format(template: string, params?: Params): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
    const v = params[name];
    return v === undefined || v === null ? whole : typeof v === 'number' ? v.toLocaleString('en-US') : v;
  });
}

export type Translate<T extends Table> = (key: Key<T>, params?: Params) => string;

/** Builds a typed lookup over one table: `t('models.delete.count', {n: 2})`. */
export function makeT<T extends Table>(table: T): Translate<T> {
  const lookup = table as Record<string, string | undefined>;
  return (key, params) => {
    const n = params?.n;
    if (typeof n === 'number') {
      const plural = lookup[`${key}.${n === 1 ? 'one' : 'other'}`];
      if (plural !== undefined) return format(plural, params);
    }
    const value = lookup[key] ?? lookup[`${key}.other`];
    if (value === undefined) {
      if (import.meta.env?.DEV) console.warn(`[strings] missing key ${key}`);
      return key;
    }
    return format(value, params);
  };
}
