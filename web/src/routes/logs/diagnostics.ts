/**
 * Pure helpers for Logs → Diagnostics (docs/ui/06 §5): the diagnostic bundle's text form
 * (rendered client-side from `POST /diagnostics`, D58, docs/ui/README §3), its file name, the raw
 * `/status` annotations, and the doctor report's text form.
 */

import type { DiagnosticsBundle, DoctorReport, TraceItem } from '../../api/models';
import { formatBytes, formatMs } from '../../lib/format';
import { t } from '../../strings/logs';

/** L4: these `/status` groups open by default, the rest collapsed. */
export const STATUS_OPEN_KEYS = ['transport', 'memory_governor', 'scheduler'] as const;
/** The `/status` schema this GUI reads (SPEC Appendix B, splash/runtime/engine/Status.cpp). */
export const STATUS_SCHEMA = 6;
export const STATUS_REFRESH_MS = 5000;

/** `*_bytes` leaves get a byte size, `*_ms` leaves a duration (docs/ui/06 §5.2). */
export function annotateStatus(key: string, value: unknown): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (key.endsWith('_bytes') && Math.abs(value) >= 1024) return formatBytes(value);
  if (key.endsWith('_ms')) return formatMs(value);
  return null;
}

const pad = (v: number) => String(v).padStart(2, '0');

/** `splash-gui-diagnostics-20261003-1407.txt` (local time). */
export function bundleFilename(when: Date = new Date()): string {
  return `splash-gui-diagnostics-${when.getFullYear()}${pad(when.getMonth() + 1)}${pad(when.getDate())}-${pad(when.getHours())}${pad(when.getMinutes())}.txt`;
}

function json(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? 'null';
  } catch {
    return String(value);
  }
}

function line(...parts: Array<string | number | null | undefined | false>): string {
  return parts.filter((p) => p !== null && p !== undefined && p !== false && p !== '').join(' · ');
}

/**
 * The text a Splash issue asks for (SPEC §10.8; Splash's CONTRIBUTING.md wants the Splash
 * version, Mac, macOS and model): header, versions, hardware, engine and its redacted
 * command, settings (already redacted by the manager), the engine log tail, raw `/status`.
 */
export function bundleText(b: DiagnosticsBundle): string {
  const v = b.versions;
  const e = v?.engine;
  const s = b.system;
  const eng = b.engine;
  const out: string[] = [];
  out.push(t('logs.bundle.header', { time: b.generated_at }));
  out.push('');
  out.push(`== ${t('logs.bundle.versions')}`);
  out.push(line(`Splash GUI ${v?.gui ?? '—'}`, v?.manager && `manager ${v.manager}`, v?.python && `Python ${v.python}`));
  out.push(
    e?.found
      ? line(`Splash ${e.version ?? '—'}`, e.support, e.source && `${t('logs.bundle.source')} ${e.source}`, e.cli)
      : line(t('logs.bundle.engine_missing'), e?.error),
  );
  out.push(line(`/status schema ${v?.status_schema_version ?? '—'}`));
  out.push('');
  out.push(`== ${t('logs.bundle.hardware')}`);
  if (s) {
    out.push(
      line(
        s.chip,
        s.cpu_cores != null && `${s.cpu_cores} CPU`,
        s.gpu_cores != null && `${s.gpu_cores} GPU`,
        formatBytes(s.memory_bytes),
        `macOS ${s.macos_version}${s.macos_build ? ` (${s.macos_build})` : ''}`,
        s.arch,
      ),
    );
  } else {
    out.push(t('logs.bundle.unknown'));
  }
  out.push('');
  out.push(`== ${t('logs.bundle.engine')}`);
  if (eng) {
    out.push(line(eng.state, eng.phase, eng.model));
    out.push(eng.command ? eng.command : t('logs.bundle.no_command'));
    if (eng.error) out.push(line(eng.error.kind, eng.error.code, eng.error.message));
  } else {
    out.push(t('logs.bundle.unknown'));
  }
  out.push('');
  out.push(`== ${t('logs.bundle.settings')}`);
  out.push(json(b.settings));
  out.push('');
  out.push(`== ${t('logs.bundle.log', { n: b.engine_log_tail.length })}`);
  out.push(...b.engine_log_tail);
  out.push('');
  out.push(`== /status`);
  out.push(b.status ? json(b.status) : t('logs.bundle.stopped'));
  out.push('');
  return out.join('\n');
}

/** Byte size of the UTF-8 text (for "Bundle copied · 48 KB"). */
export function textBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function tracesTotal(traces: readonly TraceItem[]): number {
  return traces.reduce((sum, tr) => sum + (tr.size_bytes ?? 0), 0);
}

export const DOCTOR_GLYPH: Record<DoctorReport['checks'][number]['status'], string> = {
  ok: '✓',
  warn: '!',
  fail: '✕',
  skip: '–',
};

/** The doctor list as plain text (what `splash doctor` prints). */
export function doctorText(report: DoctorReport): string {
  return report.checks
    .map((c) => {
      const head = `${DOCTOR_GLYPH[c.status]} ${c.label}: ${c.message}`;
      return c.fix ? `${head}\n    ${c.fix}` : head;
    })
    .join('\n');
}
