import type { SchemaField } from '../../api/models';
export function fieldLabel(field: SchemaField): string { return field.label; }
export function fieldHelp(field: SchemaField): string { return field.help; }
export function choiceLabel(_field: string, choice: string): string { return choice.replace(/_/g, ' '); }
