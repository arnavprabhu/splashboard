"""Tolerant readers for Splash's `/status` (schema 6, Appendix B).

Splash requires readers to tolerate missing fields (SPEC §6.7), so every getter
returns None instead of raising.
"""

from __future__ import annotations

import math
from typing import Any

SCHEMA_VERSION = 6
LATENCY_STAGES = (
    "http_request",
    "upload",
    "preparation_queue",
    "preparation",
    "template",
    "tokenization",
    "grammar",
    "images",
    "native_queue",
    "http_ttft",
    "output_interval",
)


def dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def num(data: Any, path: str) -> float | None:
    value = dig(data, path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def integer(data: Any, path: str) -> int | None:
    value = num(data, path)
    return None if value is None else int(value)


def boolean(data: Any, path: str) -> bool | None:
    value = dig(data, path)
    return value if isinstance(value, bool) else None


def text(data: Any, path: str) -> str | None:
    value = dig(data, path)
    return value if isinstance(value, str) else None


def histogram_percentile(hist: Any, q: float) -> float | None:
    """p-quantile (seconds) of a cumulative histogram `{buckets: {le: count}, count}`
    (server/latency.py), by linear interpolation inside the crossing bucket."""
    if not isinstance(hist, dict):
        return None
    buckets = hist.get("buckets")
    total = hist.get("count")
    if not isinstance(buckets, dict) or not isinstance(total, int | float) or total <= 0:
        return None
    bounds: list[tuple[float, float]] = []
    for key, count in buckets.items():
        if not isinstance(count, int | float):
            continue
        bound = math.inf if key in ("+Inf", "inf", "Inf") else _float(key)
        if bound is None:
            continue
        bounds.append((bound, float(count)))
    if not bounds:
        return None
    bounds.sort()
    rank = q * float(total)
    previous_bound, previous_count = 0.0, 0.0
    for bound, count in bounds:
        if count >= rank:
            if math.isinf(bound):
                return previous_bound
            span = count - previous_count
            fraction = 0.0 if span <= 0 else (rank - previous_count) / span
            return previous_bound + (bound - previous_bound) * fraction
        previous_bound, previous_count = bound, count
    return previous_bound


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def busy(status: Any) -> bool:
    return (integer(status, "scheduler.prefilling") or 0) + (
        integer(status, "scheduler.decoding") or 0
    ) > 0


def build_id(status: Any) -> str | None:
    return text(status, "identity.cache.build_id")
