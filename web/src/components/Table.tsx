import { Fragment, type ComponentChildren } from 'preact';

export interface Column<T> {
  key: string;
  label: string;
  align?: 'left' | 'right';
  render?: (row: T) => ComponentChildren;
  /** Keeps the header for screen readers but hides it visually (selection and action columns). */
  hideLabel?: boolean;
}

export interface TableProps<T> {
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T, index: number) => string;
  caption?: string;
  empty?: ComponentChildren;
  /** An extra full-width row under a row (e.g. the injected fields of a request); null hides it. */
  detail?: (row: T) => ComponentChildren | null;
  /** Extra class on the wrapper (e.g. `table-scroll` for wide logs). */
  class?: string;
}

/** 2px rule under the header, 1px between rows, tabular numerals. */
export function Table<T>({ columns, rows, rowKey, caption, empty, detail, class: cls }: TableProps<T>) {
  return (
    <div class={['table-wrap', cls].filter(Boolean).join(' ')}>
      <table class="table">
        {caption && <caption class="visually-hidden">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} scope="col" data-align={c.align}>
                {c.hideLabel ? <span class="visually-hidden">{c.label}</span> : c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} class="mute">
                {empty ?? 'Nothing here yet.'}
              </td>
            </tr>
          ) : (
            rows.map((row, i) => {
              const key = rowKey(row, i);
              const extra = detail?.(row) ?? null;
              return (
                <Fragment key={key}>
                  <tr>
                    {columns.map((c) => (
                      <td key={c.key} data-align={c.align}>
                        {c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? '—')}
                      </td>
                    ))}
                  </tr>
                  {extra !== null && (
                    <tr class="table-detail">
                      <td colSpan={columns.length}>{extra}</td>
                    </tr>
                  )}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
