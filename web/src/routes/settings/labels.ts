import type { SchemaField } from '../../api/models';
import { settingsStrings } from '../../strings/settings';
export function fieldLabel(field: SchemaField): string { return field.label; }
export function fieldHelp(field: SchemaField): string { return field.help; }
/** A choice's label: ours where the string table has one (`settings.choice.<key>.<choice>`),
 * else the value itself. */
export function choiceLabel(field: string, choice: string): string {
  const own = (settingsStrings as Record<string, string | undefined>)[`settings.choice.${field}.${choice}`];
  return own ?? choice.replace(/_/g, ' ');
}
