"""`python -m splash_gui.cli`: the entry the packaged `splash` shim runs.

Same behaviour as the `splash` console script, which the source tree uses through `uv run`."""

from . import main

raise SystemExit(main())
