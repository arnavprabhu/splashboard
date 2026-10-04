#!/usr/bin/env python3
"""Fake splash/install/launcher.py: `splash --version`, `splash serve` and
the agent subcommands, with the real argument parsing, help text and
launcher checks (locks, port probe, device check, installer run).

`serve` runs the fake installer (install/models.py prepare) for the
selection, as the real launcher does, then the fake server in this process
(the real one execs server/server.py, so the PID is the same either way).
Set FAKE_SPLASH_SKIP_INSTALL=1 to skip the installer step.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "install"

from server import serve_options

from . import models as model_artifacts
from . import paths

PORT = 8000
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# install/clients.py INSTALL_URLS keys, in its order.
CLIENTS = ("claude", "opencode", "codex", "hermes", "pi")
DESCRIPTION = "Serve in the foreground, or connect an installed agent to the local server."


class LauncherError(RuntimeError):
    pass


class StopSignal(KeyboardInterrupt):
    def __init__(self, number: int):
        super().__init__(number)
        self.number = number


def _interrupt(number: int, _frame: object) -> None:
    raise StopSignal(number)


def _run_held(command: list[str], **options: Any) -> int:
    """launcher.py _run_held: spawn with the stop signals blocked."""
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        with subprocess.Popen(command, **options) as program:
            try:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
                return program.wait()
            except BaseException:
                program.kill()
                raise
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _request_json(path: str, timeout: float = 2, *, port: int = PORT) -> Any:
    request = urllib.request.Request(_base_url(port) + path)
    if key := os.environ.get("SPLASH_API_KEY"):
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise LauncherError("Splash authentication failed; set SPLASH_API_KEY to the server's key") from None
        return None
    except (OSError, UnicodeDecodeError, ValueError, http.client.HTTPException):
        return None


def _version() -> str:
    return "Splash " + str(json.loads((paths.ROOT / "release.json").read_text())["version"])


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """install/launcher.py parse_args, with the same help text."""
    argv = list(sys.argv[1:] if argv is None else argv)
    client_args: list[str] = []
    if argv and argv[0] in CLIENTS:
        argv, client_args = argv[:1], argv[1:]
        if client_args[:1] == ["--"]:
            client_args = client_args[1:]
    elif "--" in argv:
        boundary = argv.index("--")
        argv, client_args = argv[:boundary], argv[boundary + 1 :]
    parser = argparse.ArgumentParser(
        prog="splash",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Quick start:\n"
            "  splash serve --model mlx-community/Qwen3.8-27B-4bit\n"
            "  splash opencode  # in another terminal, after Ready\n\n"
            "Use splash serve --help for server settings. Client arguments,\n"
            "including --help, are passed through to the installed agent."
        ),
    )
    parser.add_argument("--version", action="version", version=_version())
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser(
        "serve",
        help="run the local server; Ctrl+C stops it",
        description="Load an upstream model, automatically select its DFlash2 draft, and serve in the foreground.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  splash serve --model mlx-community/Qwen3.8-27B-4bit\n"
            "  splash serve --model unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M --max-context 128K\n\n"
            "After Ready, open http://127.0.0.1:8000 or connect an installed agent.\n"
            "The startup summary and /status report the effective context limit.\n"
            "A client may impose a smaller limit. Keep this terminal open; Ctrl+C stops serving."
        ),
    )
    server.add_argument(
        "--port",
        type=_parse_port,
        default=os.environ.get("SPLASH_PORT", str(PORT)),
        help="HTTP port (default: SPLASH_PORT or 8000)",
    )
    server.add_argument(
        "--model",
        type=model_artifacts.parse_model_id,
        required=True,
        metavar="OWNER/REPO[:VARIANT]",
        help="upstream Hugging Face model, with a GGUF variant after ':' (e.g. :UD-Q4_K_M)",
    )
    server.add_argument("--revision", help="optional model branch, tag or commit (default: repository default)")
    server.add_argument(
        "--draft-model",
        type=model_artifacts.parse_draft_model,
        help="override the automatically selected DFlash2 repository or local directory",
    )
    server.add_argument("--language-only", action="store_true", help="skip vision preparation and loading")
    server.add_argument(
        "--offline",
        action="store_true",
        help="start the installed model without contacting the Hugging Face Hub (as HF_HUB_OFFLINE=1)",
    )
    serve_options.add_serve_arguments(server)
    for name in CLIENTS:
        commands.add_parser(name, help=f"connect {name} to the running server")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve_options.check_serve_arguments(parser, args)
    if args.command in CLIENTS:
        try:
            args.port = _parse_port(os.environ.get("SPLASH_PORT", str(PORT)))
        except argparse.ArgumentTypeError as error:
            parser.error(f"SPLASH_PORT: {error}")
    if client_args and args.command == "serve":
        parser.error("arguments after -- are only supported for coding clients")
    args.client_args = client_args
    return args


def _serve_lock_owner(lock: Any) -> str:
    try:
        lock.seek(0)
        owner = json.load(lock)
    except (OSError, UnicodeError, ValueError):
        return ""
    if not isinstance(owner, dict):
        return ""
    pid, model, port = owner.get("pid"), owner.get("model"), owner.get("port")
    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(model, str)
        or not model
        or not model.isprintable()
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        return ""
    return f" (PID {pid}, model {model}, port {port})"


def _check_port(host: str, port: int) -> None:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        address = probe.getsockname()[0]
    if address == "0.0.0.0":  # noqa: S104 - install/launcher.py _check_port
        address = "127.0.0.1"
    with socket.socket() as client:
        client.settimeout(1)
        if client.connect_ex((address, port)) == 0:
            raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))


def _device_check() -> None:
    """launcher.py _ensure_installed: the engine's own device check runs
    before any download."""
    check = subprocess.run([str(paths.BINARY), "device-check"], capture_output=True, text=True, check=False)
    if check.returncode:
        report = check.stderr.strip()
        raise LauncherError(
            report.splitlines()[-1].removeprefix("error: ")
            if check.returncode > 0 and report
            else f"the engine's device check failed: {report or f'status {check.returncode}'}"
        )


def _ensure_installed(selection: model_artifacts.Selection) -> None:
    _device_check()
    if os.environ.get("FAKE_SPLASH_SKIP_INSTALL") == "1":
        return
    command = [
        str(paths.PYTHON),
        str(paths.ROOT / "install/models.py"),
        "--models",
        str(selection.models_root),
        "--model",
        selection.model,
        "prepare",
    ]
    for flag, value in (("--revision", selection.revision), ("--draft-model", selection.draft_model)):
        if value is not None:
            command[-1:-1] = [flag, value]
    if selection.language_only:
        command.insert(-1, "--language-only")
    if _run_held(command, cwd=paths.ROOT):
        raise LauncherError("model download or verification failed")


def serve(args: argparse.Namespace, argv: list[str]) -> int:
    for number in STOP_SIGNALS:
        signal.signal(number, _interrupt)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    paths.RUNTIME.mkdir(parents=True, exist_ok=True)
    with (
        (paths.RUNTIME / "serve.lock").open("a+") as installation,
        (paths.RUNTIME / f"serve-{args.port}.lock").open("a+") as lock,
    ):
        try:
            fcntl.flock(installation, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LauncherError(
                "Splash installation is busy; stop the running server or wait for the upgrade to finish"
            ) from None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LauncherError(
                f"Splash is already serving{_serve_lock_owner(lock)}; stop it with Ctrl+C first"
            ) from None
        lock.seek(0)
        lock.truncate()
        json.dump({"pid": os.getpid(), "model": args.model, "port": args.port}, lock)
        lock.flush()
        try:
            _check_port(args.host, args.port)
        except OSError as error:
            raise LauncherError(f"cannot bind {args.host}:{args.port}: {error}") from None
        selection = model_artifacts.Selection.of(
            paths.MODELS,
            args.model,
            revision=args.revision,
            language_only=args.language_only,
            draft_model=args.draft_model,
        )
        _ensure_installed(selection)
        from server import fake_server

        return fake_server.serve(args, argv)


def coding_client(args: argparse.Namespace) -> int:
    """Reads /v1/models like the real launcher and prints its banners, then
    prints the client it would exec as one JSON line instead of exec'ing."""
    listing = _request_json("/v1/models", port=args.port)
    if listing is None:
        raise LauncherError(
            f"No ready Splash server at {_base_url(args.port)}. "
            "Run 'splash serve --model <HF_REPO_ID>' in another terminal first."
        )
    models = listing.get("data", []) if isinstance(listing, dict) else []
    if (
        not isinstance(models, list)
        or not models
        or not isinstance(models[0], dict)
        or models[0].get("owned_by") != "splash"
        or type(models[0].get("context_length")) is not int
        or models[0]["context_length"] <= 0
    ):
        raise LauncherError("Could not identify the local Splash server")
    model, context = models[0].get("id"), models[0]["context_length"]
    print(f"Starting {args.command}: {model} · {context:,} context tokens", flush=True)
    if args.command == "claude":
        print("Claude hosted WebSearch is unavailable. WebFetch, local tools and MCP are unchanged.", flush=True)
    elif args.command == "codex":
        print(
            "Codex hosted WebSearch is disabled: Splash does not provide "
            "OpenAI's search service. Local tools and MCP are unchanged.",
            flush=True,
        )
    print(
        json.dumps(
            {
                "fake_exec": args.command,
                "base_url": _base_url(args.port),
                "model": model,
                "context": context,
                "input_modalities": models[0].get("input_modalities"),
                "args": args.client_args,
            }
        ),
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw)
    try:
        return serve(args, raw) if args.command == "serve" else coding_client(args)
    except (LauncherError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except StopSignal as stop:
        return 128 + stop.number
    except KeyboardInterrupt:
        return 128 + signal.SIGINT


if __name__ == "__main__":
    raise SystemExit(main())
