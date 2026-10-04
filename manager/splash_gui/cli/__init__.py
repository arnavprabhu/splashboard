"""The `splash` CLI shim (SPEC §12). Stub: the CLI track fills this in.

Until then every invocation passes through to the real engine CLI, found by
discovery (which skips our own shim), so installing the shim changes nothing.
"""

from __future__ import annotations

import os
import sys

from ..engine.discovery import discover
from ..paths import Paths


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    engine = discover(shim_paths=(Paths.from_env().shim,))
    if not engine.found or engine.cli is None:
        print(f"splash: {engine.error}", file=sys.stderr)
        return 127
    os.execv(str(engine.cli), [str(engine.cli), *args])  # noqa: S606
