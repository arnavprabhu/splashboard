"""Human-readable sizes for API messages, as the web formats them (docs/ui/00 §9).

`format_bytes` mirrors `web/src/lib/format.ts` `formatBytes`: base 1024 for bytes on
disk or in memory, base 1000 for Hub download sizes (F8); one decimal below 100,
none above."""

from __future__ import annotations

UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(value: float | None, *, base: int = 1024, digits: int = 1) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    amount = abs(float(value))
    unit = 0
    while amount >= base and unit < len(UNITS) - 1:
        amount /= base
        unit += 1
    if unit == 0:
        text = str(round(amount))
    else:
        places = 0 if amount >= 100 else digits
        text = f"{amount:.{places}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    return f"{sign}{text} {UNITS[unit]}"
