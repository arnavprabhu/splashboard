# Splash GUI manager

Python package `splash_gui`: the manager service, the admin API and the `splash` CLI shim.
See `../SPEC.md` §4 and `../docs/api.md`.

```
uv sync
uv run pytest
uv run ruff check . && uv run mypy
uv run splash-gui-manager
```

Set `SPLASH_GUI_HOME` to use a base directory other than `~/.splash`, and
`SPLASH_GUI_SECRETS=memory|file|keychain` to pick the secrets backend (tests use `memory`).

Calling the admin API from scripts: mutating routes need the CLI token from loopback,
`Authorization: Bearer $(cat ~/.splash/run/cli.token)` (see `../docs/api.md` §1.2).
Streaming routes use `splash_gui/sse.py`; Host checks for the proxy can reuse
`splash_gui/auth/guard.py` (`allowed_hosts`, `check_host`).
