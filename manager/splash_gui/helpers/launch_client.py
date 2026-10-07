"""Use Splash's maintained client configurator with an explicit profile model."""

import json
import os
import shlex
import shutil
import sys

# D46: Codex sends the user's ChatGPT Apps connectors as namespace tools, and
# Splash 1.2.0 through 1.3.0 rejects any tool name over 64 characters ("invalid namespace tool
# name", server/api_shapes.py:468-475 at 1.3.0). Turning the `apps` feature off is a `-c`
# override, so it applies to this session only; plain `codex` keeps it (D18).
CODEX_SESSION_ARGS = ("-c", "features.apps=false")


def session_args(client: str, args: list[str]) -> list[str]:
    """The client arguments plus Splash GUI's own session overrides. Ours go
    first: Splash keeps user `-c` overrides in order after its defaults
    (install/clients.py _codex_config_args), so a user's own
    `-c features.apps=true` still wins."""
    if client == "codex":
        return [*CODEX_SESSION_ARGS, *args]
    return list(args)


def main() -> None:
    # Splash's package comes in through sys.path, not PYTHONPATH, as Splash's own
    # launcher does (install/launcher.py puts its root on sys.path): PYTHONPATH
    # would reach the client and every tool it runs, which plain `claude` never
    # sees (D18).
    pkg = os.environ.pop("SPLASH_GUI_ENGINE_PKG", None)
    if pkg:
        sys.path.insert(0, pkg)
    from install import clients  # type: ignore[import-not-found]

    spec = json.loads(os.environ.pop("SPLASH_GUI_CLIENT_SPEC"))
    path = shutil.which(spec["client"])
    if not path and not spec["print"]:
        raise SystemExit("Install " + spec["client"] + " before launching it")
    # Hermes/Pi's configurators intentionally write dedicated entries. A preview
    # describes the invocation without calling those writers.
    if spec["print"] and spec["client"] in ("hermes", "pi"):
        print(shlex.join([spec["client"], "--model", spec["model"], *spec["args"]]))
        print("# Splash creates only its dedicated profile/provider; defaults stay unchanged.")
        return
    # As Splash's own launcher does (install/launcher.py coding_client): only
    # OpenCode needs its major version, because OpenCode 2 reads configuration in
    # a background service and needs `--standalone` to see the inline config.
    client_version = (
        clients.probe_major_version(path) if spec["client"] == "opencode" and path else None
    )
    argv, env = clients.command(
        spec["client"],
        path or spec["client"],
        spec["url"],
        spec["model"],
        spec["context"],
        input_modalities=spec["modalities"],
        client_args=session_args(spec["client"], spec["args"]),
        client_version=client_version,
    )
    if spec.get("format") == "json":
        # `POST /integrations/{client}/print`: the same configuration as data.
        print(
            json.dumps(
                {
                    "argv": argv,
                    "env": {k: v for k, v in env.items() if os.environ.get(k) != v},
                    "removed": sorted(k for k in os.environ if k not in env),
                }
            )
        )
        return
    if spec["print"]:
        changed = {key: value for key, value in env.items() if os.environ.get(key) != value}
        secret = os.environ.get("SPLASH_API_KEY")
        if secret and secret in changed.values():
            print("# The API key is required: export SPLASH_API_KEY=<your key> first")
            print("# (Settings → Security in Splash GUI shows and copies it).")
        for key in sorted(changed):
            value = changed[key]
            if secret and value == secret:
                # Never print the key itself; the shell expands the reference.
                print("export " + key + '="${SPLASH_API_KEY}"')
            else:
                print("export " + key + "=" + shlex.quote(value))
        print(shlex.join(argv))
    else:
        os.execvpe(argv[0], argv, env)  # noqa: S606 — Splash builds the client argv, no shell


if __name__ == "__main__":
    main()
