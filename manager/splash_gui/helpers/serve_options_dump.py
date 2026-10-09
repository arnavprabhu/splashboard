"""Print Splash's `splash serve` options as JSON.

Run with Splash's bundled Python and PYTHONPATH=<PKG>, never inside the manager:

    PYTHONPATH=$PKG $PKG/python/bin/python3 serve_options_dump.py

Standard library only. Output: {"version", "options": [...], "errors": [...]}; each
option has flag, dest, default, help, choices, metavar, action, environment, secret,
source ("shared" for server/serve_options.py, "launcher" for install/launcher.py).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("captured")
        self.parser = parser


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _action_kind(action: argparse.Action) -> str:
    name = type(action).__name__
    return {
        "_StoreTrueAction": "store_true",
        "_StoreFalseAction": "store_false",
        "_AppendAction": "append",
        "_StoreAction": "store",
    }.get(name, name)


def shared_options() -> list[dict[str, Any]]:
    from server import serve_options  # type: ignore[import-not-found]

    out = []
    for option in serve_options.SERVE_OPTIONS:
        kw = option.options
        choices = kw.get("choices")
        if choices is None and option.flag == "--default-reasoning-effort":
            choices = serve_options.REASONING_EFFORTS
        out.append(
            {
                "flag": option.flag,
                "dest": option.dest,
                "default": _jsonable(kw.get("default")),
                "help": kw.get("help", ""),
                "choices": _jsonable(choices),
                "metavar": kw.get("metavar"),
                "action": kw.get("action", "store"),
                "environment": option.environment,
                "secret": option.secret,
                "source": "shared",
            }
        )
    return out


def launcher_options(known: set[str]) -> list[dict[str, Any]]:
    """The options install/launcher.py adds to `serve` itself (--port, --model, ...)."""
    from install import launcher  # type: ignore[import-not-found]

    original = argparse.ArgumentParser.parse_args

    def capture(self: argparse.ArgumentParser, args: Any = None, namespace: Any = None) -> Any:
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = capture  # type: ignore[method-assign]
    try:
        launcher.parse_args(["serve", "--model", "owner/repo"])
    except _Captured as captured:
        parser = captured.parser
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    serve = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            serve = action.choices.get("serve")
    if serve is None:
        raise RuntimeError("launcher has no serve command")
    out = []
    for action in serve._actions:
        flags = [s for s in action.option_strings if s.startswith("--")]
        if not flags or flags[0] in known or flags[0] == "--help":
            continue
        out.append(
            {
                "flag": flags[0],
                "dest": action.dest,
                "default": _jsonable(action.default),
                "help": action.help or "",
                "choices": _jsonable(action.choices),
                "metavar": action.metavar,
                "action": _action_kind(action),
                "environment": None,
                "secret": False,
                "source": "launcher",
            }
        )
    return out


def version(root: Path) -> Any:
    try:
        return json.loads((root / "release.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    root = Path(sys.path[0]).resolve()
    for entry in sys.path:
        if entry and (Path(entry) / "server" / "serve_options.py").is_file():
            root = Path(entry).resolve()
            break
    result = {"version": version(root), "options": [], "errors": []}
    try:
        result["options"] = shared_options()
    except Exception as error:  # report, never crash: the manager shows the error
        result["errors"].append(f"server.serve_options: {type(error).__name__}: {error}")
    try:
        known = {o["flag"] for o in result["options"]}
        result["options"].extend(launcher_options(known))
    except Exception as error:
        result["errors"].append(f"install.launcher: {type(error).__name__}: {error}")
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
