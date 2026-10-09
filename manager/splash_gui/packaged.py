"""Where the manager runs from: a source checkout, or the bundled runtime inside
`Splashboard.app` (SPEC §19, docs/plans/packaging.md PKG-3).

The packaged app keeps its Python at `<App>/Contents/Resources/manager/python/bin/python3`
and its web admin at `<App>/Contents/Resources/web`. Anything else (a checkout, `uv run`,
a test) returns None and keeps the source-tree behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The directories between the .app folder and the bundled interpreter, outermost first.
_RUNTIME_DIR = ("Contents", "Resources", "manager", "python", "bin")


def this_interpreter() -> Path:
    """The absolute path of this interpreter, built from `sys.prefix`.

    `sys.executable` is not reliable here. launchd starts the packaged agent with the
    relative argv[0] of its BundleProgram (`Contents/Resources/manager/python/bin/python3`)
    and the working directory `/`, and Python then reports `//Contents/...`. `sys.prefix`
    is found from the runtime's own files, so it stays absolute (PKG-4 lane 1 reproduction).
    """
    return Path(sys.prefix) / "bin" / "python3"


def bundle_root(executable: str | None = None) -> Path | None:
    """The `.app` folder when `executable` (default: this interpreter) is the bundled
    runtime's Python, else None. The path is not resolved: the bundle may be moved, and
    the interpreter's own location is what identifies it."""
    exe = Path(executable) if executable is not None else this_interpreter()
    parts = exe.parent.parts
    depth = len(_RUNTIME_DIR)
    if len(parts) <= depth or tuple(parts[-depth:]) != _RUNTIME_DIR:
        return None
    return Path(*parts[:-depth])


def bundled_python(root: Path) -> Path:
    """The bundled interpreter inside a `.app` folder."""
    return root.joinpath(*_RUNTIME_DIR, "python3")
