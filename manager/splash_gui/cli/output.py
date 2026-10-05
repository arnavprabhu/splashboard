"""Terminal output for the `splash` CLI (docs/ui/11 §2, §13).

Colour and glyph rules (§2.2), the number formatters of docs/ui/00 §9 (ported
from `web/src/lib/format.ts` with the same rules), tables with middle
truncation of the ID column (§2.4), and the `✗ headline / detail / Fix:` error
format with its exit codes (§13).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

DASH = "—"

# Exit codes (docs/ui/11 §13.2).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_MANAGER_DOWN = 3
EXIT_ENGINE = 4
EXIT_MODEL = 5
EXIT_MISSING = 6
EXIT_BUSY = 7
EXIT_AUTH = 8
EXIT_INTERRUPTED = 130


class CliError(Exception):
    """A failure the CLI reports as `✗ headline` (+ detail, + Fix:) and an exit code."""

    def __init__(
        self,
        headline: str,
        *,
        exit_code: int = EXIT_FAILED,
        detail: str | None = None,
        fix: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(headline)
        self.headline = headline
        self.exit_code = exit_code
        self.detail = detail
        self.fix = fix
        self.code = code or {
            EXIT_USAGE: "usage",
            EXIT_MANAGER_DOWN: "manager_not_running",
            EXIT_ENGINE: "engine_error",
            EXIT_MODEL: "model_not_found",
            EXIT_MISSING: "engine_not_found",
            EXIT_BUSY: "model_switch_busy",
            EXIT_AUTH: "not_authorised",
        }.get(exit_code, "failed")


# Manager error codes → exit codes (docs/ui/11 §13.2, last paragraph).
_CODE_EXIT = {
    "model_not_found": EXIT_MODEL,
    "model_not_installed": EXIT_MODEL,
    "incompatible": EXIT_MODEL,
    "model_switch_busy": EXIT_BUSY,
    "install_in_progress": EXIT_BUSY,  # the engine is busy downloading (docs/ui/11 §13.2)
    "engine_unavailable": EXIT_ENGINE,
    "engine_recovering": EXIT_ENGINE,
    "engine_failed": EXIT_ENGINE,
    "engine_not_found": EXIT_MISSING,
}


def exit_code_for(status: int, code: str | None) -> int:
    if code in _CODE_EXIT:
        return _CODE_EXIT[code]
    if status in (401, 403):
        return EXIT_AUTH
    if 400 <= status < 500:
        return EXIT_USAGE
    return EXIT_FAILED


# Colour and glyphs (§2.2) ------------------------------------------------------

_GLYPHS_UTF8 = {"ok": "✓", "fail": "✗", "warn": "!", "arrow": "▸", "dot": "●", "ellipsis": "…"}
_GLYPHS_ASCII = {"ok": "+", "fail": "x", "warn": "!", "arrow": ">", "dot": "*", "ellipsis": "..."}


def utf8_locale(env: Mapping[str, str] | None = None) -> bool:
    """The locale in effect is UTF-8 (LC_ALL, then LC_CTYPE, then LANG)."""
    env = os.environ if env is None else env
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(name)
        if value:
            return "utf-8" in value.lower() or "utf8" in value.lower()
    return False


def colour_enabled(choice: str, stream: TextIO, env: Mapping[str, str] | None = None) -> bool:
    """`--color always` wins over NO_COLOR; `auto` needs a TTY and no NO_COLOR."""
    env = os.environ if env is None else env
    if choice == "always":
        return True
    if choice == "never" or env.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class Style:
    colour: bool = False
    utf8: bool = True

    def glyph(self, name: str) -> str:
        return (_GLYPHS_UTF8 if self.utf8 else _GLYPHS_ASCII)[name]

    def _sgr(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour and text else text

    def bold(self, text: str) -> str:
        return self._sgr(text, "1")

    def dim(self, text: str) -> str:
        return self._sgr(text, "2")

    def live(self, text: str) -> str:
        """Live states (`ready`, `generating`): bold + 256-colour 166."""
        return self._sgr(text, "1;38;5;166")

    def accent(self, text: str) -> str:
        return self._sgr(text, "38;5;166")

    def error(self, text: str) -> str:
        return self._sgr(text, "1;31")

    def warning(self, text: str) -> str:
        return self._sgr(text, "1;33")

    def mark(self, status: str) -> str:
        """`✓` / `!` / `✗` for ok / warn / fail, styled."""
        if status == "fail":
            return self.error(self.glyph("fail"))
        if status == "warn":
            return self.warning(self.glyph("warn"))
        return self.bold(self.glyph("ok"))


def make_style(choice: str, stream: TextIO, env: Mapping[str, str] | None = None) -> Style:
    return Style(colour=colour_enabled(choice, stream, env), utf8=utf8_locale(env))


def visible_len(text: str) -> int:
    """Length without SGR escape sequences."""
    length, index = 0, 0
    while index < len(text):
        if text[index] == "\033":
            end = text.find("m", index)
            index = end + 1 if end >= 0 else len(text)
            continue
        length += 1
        index += 1
    return length


# Formatters (docs/ui/00 §9) ----------------------------------------------------


def _num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _trim(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def fmt_bytes(value: Any, base: int = 1024, digits: int = 1) -> str:
    """`21.3 GB`, `512 MB`, `0 B`: one decimal below 100, none above; base 1024."""
    if not _num(value):
        return DASH
    sign = "-" if value < 0 else ""
    size = abs(float(value))
    unit = 0
    while size >= base and unit < len(_BYTE_UNITS) - 1:
        size /= base
        unit += 1
    text = str(round(size)) if unit == 0 else _trim(size, 0 if size >= 100 else digits)
    return f"{sign}{text} {_BYTE_UNITS[unit]}"


def fmt_count(value: Any) -> str:
    return f"{round(value):,}" if _num(value) else DASH


def fmt_tps(value: Any) -> str:
    if not _num(value):
        return DASH
    return (f"{round(value):,}" if value >= 100 else f"{value:.1f}") + " tok/s"


def fmt_percent(ratio: Any, digits: int = 1) -> str:
    if not _num(ratio):
        return DASH
    pct = ratio * 100
    if pct != 0 and abs(pct) < 10**-digits:
        return f"<{10**-digits:g}%" if pct > 0 else DASH
    return f"{_trim(pct, digits)}%"


def fmt_duration(seconds: Any) -> str:
    """`45 s`, `3 m 20 s`, `2 h 05 m`, `3 d 4 h`."""
    if not _num(seconds) or seconds < 0:
        return DASH
    s = math.floor(seconds)
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} m {s % 60:02d} s"
    h = m // 60
    if h < 24:
        return f"{h} h {m % 60:02d} m"
    return f"{h // 24} d {h % 24} h"


def fmt_ms(ms: Any) -> str:
    """`0.8 ms`, `850 ms`, `1.24 s`."""
    if not _num(ms) or ms < 0:
        return DASH
    if ms < 10:
        return f"{_trim(ms, 1)} ms"
    if ms < 1000:
        return f"{round(ms)} ms"
    if ms < 60_000:
        return f"{_trim(ms / 1000, 2 if ms < 10_000 else 1)} s"
    return fmt_duration(ms / 1000)


def fmt_tokens(tokens: Any) -> str:
    """Context sizes: 131072 → `128K` (K = 1024)."""
    if not _num(tokens):
        return DASH
    if tokens >= 1024 and tokens % 1024 == 0:
        return f"{int(tokens) // 1024}K"
    return fmt_count(tokens)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_relative(when: Any, now: datetime | None = None) -> str:
    """`just now`, `3 min ago`, `yesterday`, `5 d ago`, then `2026-09-28`."""
    moment = parse_time(when)
    if moment is None:
        return DASH
    now = now or datetime.now(UTC)
    diff = (now - moment).total_seconds()
    future = diff < 0
    span = abs(diff)

    def phrase(value: int, unit: str) -> str:
        return f"in {value} {unit}" if future else f"{value} {unit} ago"

    if span < 45:
        return "just now"
    minutes = round(span / 60)
    if minutes < 60:
        return phrase(minutes, "min")
    hours = round(span / 3600)
    if hours < 24:
        return phrase(hours, "h")
    days = round(span / 86400)
    if days == 1:
        return "tomorrow" if future else "yesterday"
    if days < 7:
        return phrase(days, "d")
    return moment.astimezone().strftime("%Y-%m-%d")


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def home_relative(path: str | os.PathLike[str] | None) -> str:
    if path is None:
        return DASH
    text = str(path)
    home = str(Path("~").expanduser())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


# Tables (§2.4) -------------------------------------------------------------------


def middle_truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    keep = width - len(ellipsis)
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + ellipsis + (text[-tail:] if tail else "")


def terminal_width(stream: TextIO) -> int | None:
    """The terminal's width when `stream` is a TTY, else None (never truncate)."""
    try:
        if not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    return shutil.get_terminal_size((100, 24)).columns


@dataclass
class Table:
    headers: Sequence[str]
    rows: list[list[str]] = field(default_factory=list)
    # The one column that may be shortened (middle ellipsis) to fit the terminal.
    truncate: int | None = 0
    gap: int = 2

    def render(self, style: Style, width: int | None = None) -> list[str]:
        """Columns sized to the content; the truncatable column shrinks only when
        the line would exceed `width` (None: never truncate: pipes, --wide)."""
        cells = [list(self.headers), *self.rows]
        widths = [max(visible_len(row[i]) for row in cells) for i in range(len(self.headers))]
        if width is not None and self.truncate is not None:
            total = sum(widths) + self.gap * (len(widths) - 1)
            excess = total - width
            if excess > 0:
                column = self.truncate
                minimum = max(len(self.headers[column]), 12)
                widths[column] = max(minimum, widths[column] - excess)
                cut = style.glyph("ellipsis")
                for row in self.rows:
                    row[column] = middle_truncate(row[column], widths[column], cut)
        lines = []
        for index, row in enumerate(cells):
            parts = []
            for column, value in enumerate(row):
                text = style.bold(value) if index == 0 else value
                if column < len(row) - 1:
                    text += " " * (widths[column] - visible_len(value))
                parts.append(text)
            lines.append((" " * self.gap).join(parts).rstrip())
        return lines


# Errors (§13.1) --------------------------------------------------------------------


def print_error(error: CliError, style: Style, *, as_json: bool, out: TextIO, err: TextIO) -> None:
    if as_json:
        body = {"error": {"code": error.code, "message": error.headline, "fix": error.fix}}
        if error.detail:
            body["error"]["detail"] = error.detail
        print(json.dumps(body, indent=2), file=out)
        return
    print(f"{style.error(style.glyph('fail'))} {style.error(error.headline)}", file=err)
    if error.detail:
        for line in error.detail.splitlines():
            print("  " + line, file=err)
    if error.fix:
        print("  Fix: " + error.fix, file=err)


def stdout_style(choice: str) -> Style:
    return make_style(choice, sys.stdout)


def stderr_style(choice: str) -> Style:
    return make_style(choice, sys.stderr)
