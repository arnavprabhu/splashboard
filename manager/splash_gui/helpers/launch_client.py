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


# D60 (Q33): a profile's `reasoning_effort` is injected only when a request leaves
# the field out (D12), and Claude Code, Codex (with `model_reasoning_effort` in its
# config), Hermes and Pi always send their own. So a `model:profile` session also
# sets the client's own per-run option. Each is session-only: a flag or an
# environment variable, never the client's config (D18). Verified 2026-10-07 against
# Claude Code 2.1.292, codex-cli 0.160.1, Hermes 0.21.5 and Pi 1.0.4.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort`


def _user_sets(args: list[str], *flags: str) -> bool:
    """The user's own arguments (before a `--`) already set one of these flags."""
    own = args[: args.index("--")] if "--" in args else args
    return any(a in flags or a.startswith(tuple(f + "=" for f in flags)) for a in own)


def reasoning_args(client: str, effort: str | None, args: list[str]) -> list[str]:
    """The client's own per-run reasoning option for a profile's effort. A flag the
    user passed wins, so nothing is added then."""
    if not effort:
        return []
    if client == "hermes" and not _user_sets(args, "--reasoning"):
        # Hermes takes every Splash effort ("Overrides agent.reasoning_effort in
        # config.yaml for this run only", `hermes --help`).
        return ["--reasoning", effort]
    if client == "pi" and not _user_sets(args, "--thinking"):
        # Pi calls "none" "off", which Splash's provider maps back to "none"
        # (install/clients.py thinkingLevelMap); Pi clamps xhigh and max to the
        # model's levels (it sends "high").
        return ["--thinking", "off" if effort == "none" else effort]
    if client == "codex":
        # A `-c` override, before the user's own, which still wins (last wins).
        return ["-c", f'model_reasoning_effort="{effort}"']
    if client == "claude" and effort in CLAUDE_EFFORTS and not _user_sets(args, "--effort"):
        return ["--effort", effort]
    # Claude Code has no "minimal" effort; OpenCode sends no effort of its own,
    # so the manager's injection already applies the profile.
    return []


def reasoning_env(client: str, effort: str | None, environ: dict[str, str]) -> dict[str, str]:
    """Environment for the session: Claude Code has no "none" effort, and turns
    thinking off with MAX_THINKING_TOKENS=0 (it then sends no `thinking`, which
    Splash reads as off). A value the user exported wins."""
    if client == "claude" and effort == "none" and "MAX_THINKING_TOKENS" not in environ:
        return {"MAX_THINKING_TOKENS": "0"}
    return {}


def session_args(client: str, args: list[str], effort: str | None = None) -> list[str]:
    """The client arguments plus Splashboard's own session overrides. Ours go
    first: Splash keeps user `-c` overrides in order after its defaults
    (install/clients.py _codex_config_args), so a user's own
    `-c features.apps=true` still wins."""
    ours = reasoning_args(client, effort, args)
    if client == "codex":
        ours = [*CODEX_SESSION_ARGS, *ours]
    return [*ours, *args]


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
    effort = spec.get("reasoning_effort")
    if spec["print"] and spec["client"] in ("hermes", "pi"):
        args = session_args(spec["client"], spec["args"], effort)
        print(shlex.join([spec["client"], "--model", spec["model"], *args]))
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
        client_args=session_args(spec["client"], spec["args"], effort),
        client_version=client_version,
    )
    env.update(reasoning_env(spec["client"], effort, env))
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
            print("# (Settings → Security in Splashboard shows and copies it).")
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
