"""The Splash CLI shim (SPEC §12, docs/ui/11). New commands use the manager;
engine commands pass through to the real `splash` unchanged (D21)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from .. import SERVICE, __version__, packaged
from ..engine.discovery import discover
from ..paths import Paths
from ..secrets import SecretName, SecretStore, backend_from_env
from ..settings.effective import profile_reasoning_effort
from ..settings.store import SettingsStore
from . import doctor as doctor_checks
from .output import (
    DASH,
    EXIT_ENGINE,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_MANAGER_DOWN,
    EXIT_MISSING,
    EXIT_MODEL,
    EXIT_USAGE,
    CliError,
    Style,
    Table,
    exit_code_for,
    fmt_bytes,
    fmt_count,
    fmt_duration,
    fmt_ms,
    fmt_percent,
    fmt_relative,
    fmt_tokens,
    fmt_tps,
    home_relative,
    make_style,
    parse_time,
    plural,
    print_error,
    terminal_width,
)

COMMANDS = (
    "start",
    "stop",
    "restart",
    "status",
    "ps",
    "ls",
    "pull",
    "rm",
    "load",
    "unload",
    "run",
    "launch",
    "open",
    "logs",
    "config",
    "doctor",
    "version",
)
CLIENTS = ("claude", "codex", "opencode", "hermes", "pi", "claude-desktop", "codex-app")
DESKTOP_CLIENTS = ("claude-desktop", "codex-app")
# Global options (docs/ui/11 §2.1): accepted before and after the command.
GLOBAL_FLAGS = ("--json", "--quiet", "-q", "-v", "--verbose")
GLOBAL_VALUED = ("--color", "--port")
ENGINE_COMMANDS_FALLBACK = ("serve", "claude", "opencode", "codex", "hermes", "pi")
SERVING_STATES = ("ready", "busy", "idle_released")
NOT_SERVING = ("stopped", "failed", "engine_failed")
NO_MODEL = (
    "No model is loaded. Pass --model <ID> or set a default: "
    "splash config set routing.default_model <ID>"
)


class ApiFailure(CliError):
    """An error answer from the manager, mapped to an exit code (§13.2)."""

    def __init__(
        self,
        status: int,
        message: str,
        code: str | None,
        detail: str | None = None,
        fix: str | None = None,
    ) -> None:
        super().__init__(
            message,
            exit_code=exit_code_for(status, code),
            detail=detail,
            fix=fix,
            code=code or "failed",
        )
        self.status = status


# The manager's 409 says "Send force to stop it anyway"; the CLI has no --force for
# this (docs/ui/11 has none), so it says what a terminal user can do.
INSTALL_IN_PROGRESS_FIX = (
    "wait for the download, or run `splash unload` to stop it (the file in progress restarts)"
)


def api_failure(response: httpx.Response) -> ApiFailure:
    try:
        error = response.json()["error"]
        message, code = str(error["message"]), error.get("code")
    except (ValueError, KeyError, TypeError):
        return ApiFailure(response.status_code, response.text or response.reason_phrase, None)
    if code == "install_in_progress":
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        repo, active = details.get("repo"), details.get("active")
        headline = (
            f"Splash is downloading {repo} for {active}"
            if repo and active
            else message.split(";")[0]
        )
        return ApiFailure(response.status_code, headline, code, fix=INSTALL_IN_PROGRESS_FIX)
    issues = [i for i in error.get("issues") or [] if isinstance(i, dict)]
    lines = [
        f"{i.get('key') or '.'.join(map(str, i.get('path', [])))}: {i.get('message')}"
        for i in issues
    ]
    if lines:
        # `✗ serve.max_context: must be …` (docs/ui/11 §11): the first issue leads.
        return ApiFailure(response.status_code, lines[0], code, "\n".join(lines[1:]) or None)
    return ApiFailure(response.status_code, message, code)


class Client:
    def __init__(self, port: int | None = None) -> None:
        self.paths = Paths.from_env().ensure()
        self.settings = SettingsStore(self.paths)
        self.settings.load()
        self.port = port or env_port() or self.settings.current.global_.server.port
        self.url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.url, timeout=180)

    def headers(self) -> dict[str, str]:
        token = self.paths.cli_token.read_text().strip() if self.paths.cli_token.exists() else ""
        return {"Authorization": "Bearer " + token}

    def request(self, method: str, path: str, body: Any = None) -> Any:
        response = self.http.request(method, "/api/admin" + path, json=body, headers=self.headers())
        if response.status_code >= 400:
            raise api_failure(response)
        return response.json() if response.content else None

    def probe(self) -> str:
        """`ours`, `other` (something else answers on the port) or `down`."""
        try:
            response = self.http.get("/health", timeout=1)
        except httpx.HTTPError:
            return "down"
        return "ours" if is_manager_health(response) else "other"

    def running(self) -> bool:
        """True when our manager answers. Another server on the port is an error:
        starting a manager there would fail to bind, and talking to it would send
        admin calls (and the CLI token) to a stranger."""
        state = self.probe()
        if state == "other":
            raise CliError(foreign_server_message(self.port))
        return state == "ours"

    def start(self, foreground: bool = False, *, note: bool = True) -> None:
        if self.running():
            return
        argv = [sys.executable, "-m", "splash_gui", "--port", str(self.port)]
        if foreground:
            os.execv(sys.executable, argv)  # noqa: S606 — fixed Python and argv, no shell
        if note:
            print("Starting Splash GUI…", file=sys.stderr)
        with self.paths.manager_log.open("ab") as log:
            proc = subprocess.Popen(
                argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.running():
                return
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        raise CliError(
            "Splash GUI did not start.", fix="check " + home_relative(self.paths.manager_log)
        )

    def installed(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/models")["models"])


def env_port() -> int | None:
    try:
        return int(os.environ["SPLASH_PORT"])
    except (KeyError, ValueError):
        return None


def is_manager_health(response: httpx.Response) -> bool:
    """A Splash GUI manager's `/health`: 200 with `service: splash-gui-manager`."""
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("service") == SERVICE


def foreign_server_message(port: int) -> str:
    return (
        f"another server (not Splash GUI) is answering on port {port}. "
        "Pass --port with the Splash GUI manager's port, or move one of them "
        "(Splash GUI: Settings → Server & network → Port)."
    )


def manager_down(port: int | None) -> CliError:
    where = f" on port {port}" if port else ""
    return CliError(
        f"Splash GUI is not running{where}. Start it with: splash start",
        exit_code=EXIT_MANAGER_DOWN,
        code="manager_not_running",
    )


# Arguments -----------------------------------------------------------------------


def split_globals(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    """The global options before the command, and the rest."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in GLOBAL_FLAGS:
            index += 1
        elif argument in GLOBAL_VALUED and index + 1 < len(arguments):
            index += 2
        elif "=" in argument and argument.split("=", 1)[0] in GLOBAL_VALUED:
            index += 1
        else:
            break
    return list(arguments[:index]), list(arguments[index:])


def _globals(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS: an option given before the command is not reset by the
    # subcommand's default (argparse applies sub-parser defaults last).
    s = argparse.SUPPRESS
    parser.add_argument("--json", action="store_true", default=s, help="machine-readable output")
    parser.add_argument("--color", choices=("auto", "always", "never"), default=s)
    parser.add_argument("--port", type=int, default=s, help="manager port")
    parser.add_argument("-q", "--quiet", action="store_true", default=s, help="no notes")
    parser.add_argument("-v", "--verbose", action="store_true", default=s)


EXAMPLES = {
    "status": "splash status\n  splash status --json",
    "ps": "splash ps\n  splash ps --json",
    "ls": "splash ls\n  splash ls --wide | cat",
    "pull": "splash pull mlx-community/Qwen3.8-27B-4bit\n"
    "  splash pull unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M",
    "rm": "splash rm unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL",
    "load": "splash load mlx-community/Qwen3.8-27B-4bit",
    "run": 'splash run mlx-community/Qwen3.8-27B-4bit "Hello"',
    "launch": "splash launch claude\n  splash launch codex --print",
    "config": "splash config get\n  splash config get serve.max_context\n"
    "  splash config set routing.default_model mlx-community/Qwen3.8-27B-4bit",
    "doctor": "splash doctor\n  splash doctor --json\n  splash doctor --uninstall\n"
    "  splash doctor --uninstall --yes --delete-cache",
    "version": "splash version --json",
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="splash", add_help=False)
    _globals(root)
    commands = root.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = commands.add_parser(
            name,
            epilog="examples:\n  " + EXAMPLES[name] if name in EXAMPLES else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        _globals(sub)
        if name in ("start", "restart"):
            sub.add_argument("--foreground", action="store_true")
        if name in ("ls", "ps"):
            sub.add_argument("--wide", action="store_true", help="never truncate IDs")
        if name in ("pull", "rm", "load", "run"):
            sub.add_argument("model")
        if name == "pull":
            sub.add_argument("--revision")
            sub.add_argument("--draft-model")
            sub.add_argument("--language-only", action="store_true")
            sub.add_argument("--no-verify", action="store_true")
        if name == "rm":
            sub.add_argument("--yes", action="store_true")
        if name == "doctor":
            sub.add_argument(
                "--uninstall", action="store_true", help="remove Splash GUI data (SPEC §19)"
            )
            sub.add_argument("--yes", action="store_true", help="do not ask before removing")
            sub.add_argument("--delete-models", action="store_true", help="also delete models")
            sub.add_argument("--delete-cache", action="store_true", help="also delete the cache")
        if name == "run":
            sub.add_argument("prompt", nargs="*")
        if name == "open":
            sub.add_argument("page", nargs="?", default="status")
        if name == "logs":
            sub.add_argument("-f", "--follow", action="store_true")
            sub.add_argument("-n", "--lines", type=int, default=200)
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--engine", action="store_true")
            group.add_argument("--manager", action="store_true")
        if name == "config":
            sub.add_argument("operation", choices=["get", "set", "unset"])
            sub.add_argument("key", nargs="?")
            sub.add_argument("value", nargs="?")
        if name == "launch":
            sub.add_argument("client", choices=CLIENTS)
            sub.add_argument("--model")
            sub.add_argument("--print", dest="print_only", action="store_true")
            sub.add_argument("--restore", action="store_true")
    return root


def parse(arguments: list[str]) -> argparse.Namespace:
    args = parser().parse_args(arguments)
    for name, default in (
        ("json", False),
        ("color", "auto"),
        ("port", None),
        ("quiet", False),
        ("verbose", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def engine_commands() -> tuple[str, ...]:
    """The real `splash -h` subcommands when it answers quickly, else the static list."""
    try:
        engine = discover(shim_paths=(Paths.from_env().shim,))
        if engine.cli is None:
            return ENGINE_COMMANDS_FALLBACK
        out = subprocess.run(
            [str(engine.cli), "-h"], capture_output=True, text=True, timeout=3, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ENGINE_COMMANDS_FALLBACK
    found = re.search(r"\{([a-z0-9_,-]+)\}", out)
    return tuple(found.group(1).split(",")) if found else ENGINE_COMMANDS_FALLBACK


def help_text(engine: Sequence[str] = ENGINE_COMMANDS_FALLBACK) -> str:
    """`splash --help` (docs/ui/11 §3)."""
    passthrough_list = ", ".join([*engine, "--version", "-h"])
    return f"""Splash GUI — run, monitor and chat with Splash models

Usage: splash [--json] [--color auto|always|never] [--port N] [--quiet] <command> [options]

Serving
  start              Start the Splash GUI manager
  stop               Stop the manager, the engine and restore integrations
  restart            Restart the manager
  status             Manager and engine state, model, endpoints, memory, tok/s
  ps                 The active model, uptime, requests in flight, memory
  load <ID[:profile]>   Load a model (switches if another is active)
  unload             Stop the engine, keep the manager running

Models
  ls                 Installed models
  pull <ID>          Download a model (OWNER/REPO[:VARIANT])
  rm <ID>            Delete a model (shared files are kept while in use)

Use
  run <ID[:profile]> [prompt]   Chat in the terminal, or print one answer
  launch <client>    Start an agent on Splash for this session only
                     ({", ".join(CLIENTS)})
  open [page]        Open the admin page signed in (status, models, chat, settings, ...)

Maintenance
  logs [-f]          Tail the engine or manager log
  config get|set|unset <key> [value]
  doctor             Check the install and explain how to fix problems
  version            GUI, manager and engine versions

Options
  --json             Machine-readable output (status, ps, ls, version, doctor, config get)
  --port N           Manager port (default: SPLASH_PORT, settings.json server.port, or 8000)
  --color auto|always|never
  -q, --quiet        No progress or notes; errors still print
  --help             This help. Help for a command: splash pull --help

Engine commands (passed to Splash): {passthrough_list},
  and anything else. See: splash serve --help
"""


# Helpers -------------------------------------------------------------------------


class Ctx:
    """What every command needs: the parsed arguments and the output styles."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.json: bool = args.json
        self.quiet: bool = args.quiet
        self.out = make_style("never" if args.json else args.color, sys.stdout)
        self.err = make_style(args.color, sys.stderr)

    def note(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr)

    def ok(self, text: str) -> None:
        print(f"{self.out.bold(self.out.glyph('ok'))} {text}")

    def emit_json(self, data: Any) -> None:
        print(json.dumps(data, indent=2))


def manager_pid(paths: Paths) -> tuple[int | None, float | None]:
    """The manager's pid and uptime from its pid file (written at startup)."""
    try:
        pid = int(paths.manager_pid.read_text().strip())
        started = paths.manager_pid.stat().st_mtime
    except (OSError, ValueError):
        return None, None
    return pid, max(0.0, time.time() - started)


def format_label(row: dict[str, Any] | None) -> str:
    """The FORMAT column: `MLX 4b` (Splash serves only 4-bit MLX) or `GGUF`."""
    if not row:
        return DASH
    return "MLX 4b" if row.get("format") == "mlx" else "GGUF"


STATE_LABELS = {
    "stopped": "stopped",
    "ready": "ready",
    "busy": "generating",
    "idle_released": "idle",
    "recovering": "recovering",
    "engine_failed": "failed",
    "failed": "failed",
    "stopping": "stopping",
    "crashed": "restarting",
}
PHASE_LABELS = {"installing": "preparing", "loading": "loading weights", "warming": "warming up"}


def state_label(engine: dict[str, Any]) -> str:
    """The lowercase chip label (docs/ui/00 §5.1)."""
    state = engine.get("state") or "stopped"
    if state == "starting":
        return PHASE_LABELS.get(engine.get("phase") or "", "loading")
    return STATE_LABELS.get(state, state)


def styled_state(style: Style, label: str) -> str:
    return style.live(label) if label.split(" ")[0] in ("ready", "generating") else label


def snapshot(client: Client) -> dict[str, Any]:
    try:
        return dict(client.request("GET", "/metrics/snapshot") or {})
    except CliError:
        return {}


def most_recent(installed: list[dict[str, Any]]) -> str | None:
    usable = [m for m in installed if m.get("status") not in ("downloading", "paused", "broken")]
    if not usable:
        return None
    ordered = sorted(usable, key=lambda m: m.get("last_used_at") or "", reverse=True)
    return str(ordered[0]["id"])


def base_model(model: str, installed: list[dict[str, Any]]) -> str:
    """The installed ID behind `ID[:profile]` (GGUF IDs carry a `:VARIANT` too)."""
    ids = [str(item["id"]) for item in installed]
    return next(
        (
            candidate
            for candidate in sorted(ids, key=len, reverse=True)
            if model == candidate or model.startswith(candidate + ":")
        ),
        model,
    )


# status / ps / ls ------------------------------------------------------------------


def status_data(client: Client) -> dict[str, Any]:
    """`splash status --json` (docs/ui/11 §4.2), plus `_` keys for the human view."""
    engine = client.request("GET", "/engine")
    live = snapshot(client)
    versions = client.request("GET", "/versions")
    settings = client.request("GET", "/settings")
    try:
        installed = client.installed()
    except CliError:
        installed = []
    row = next((m for m in installed if m["id"] == engine.get("model")), None)
    g = settings["settings"]["global"]
    pid, uptime = manager_pid(client.paths)
    memory = live.get("memory") or {}
    speed = live.get("throughput") or {}
    latency = live.get("latency") or {}
    totals = live.get("totals") or {}
    cache = live.get("cache") or {}
    disk = live.get("disk") or {}
    error = engine.get("error")
    return {
        "manager": {
            "running": True,
            "url": client.url,
            "pid": pid,
            "version": versions.get("manager"),
            "uptime_s": round(uptime) if uptime is not None else None,
        },
        "engine": {
            "state": engine.get("state"),
            "phase": engine.get("phase"),
            "engine_version": (versions.get("engine") or {}).get("version"),
            "model": engine.get("model"),
            "profile": None,
            "format": row.get("format") if row else None,
            "variant": row.get("variant") if row else None,
            "max_context": engine.get("maximum_context_tokens"),
            "vision": engine.get("vision"),
            "draft": engine.get("draft"),
            "kv_format": engine.get("kv_format"),
            "uptime_s": engine.get("uptime_s"),
            "error": error.get("message") if isinstance(error, dict) else error,
        },
        "memory": {
            "current_bytes": memory.get("current_bytes"),
            "peak_bytes": memory.get("peak_bytes"),
            "limit_bytes": memory.get("limit_bytes"),
            "system_pressure": memory.get("system_pressure"),
        },
        "speed": {
            "decode_tps": speed.get("decode_tps"),
            "prefill_tps": speed.get("prefill_tps"),
            "ttft_p50_ms": latency.get("ttft_p50_ms"),
            "ttft_p95_ms": latency.get("ttft_p95_ms"),
        },
        "requests": {
            "completed": totals.get("requests_completed"),
            "failed": totals.get("requests_failed"),
            "in_flight": engine.get("requests_in_flight"),
            "queued": engine.get("queued"),
        },
        "cache": {
            "hit_rate": cache.get("hit_rate"),
            "reused_tokens": totals.get("reused_tokens"),
            "disk_used_bytes": disk.get("used_bytes"),
            "disk_capacity_bytes": disk.get("capacity_bytes"),
            "persistent": disk.get("persistent") if disk.get("enabled") else None,
            "dir": (settings.get("resolved") or {}).get("cache_dir"),
        },
        "endpoints": {
            "openai": client.url + "/v1",
            "anthropic": client.url,
            "api_key_required": g["security"]["api_key_required"],
            "host": g["server"]["host"],
        },
        "_idle_minutes": (g.get("lifecycle") or {}).get("idle_unload_minutes"),
        "_transport_error": (engine.get("transport") or {}).get("error"),
        "_restart_attempt": (engine.get("restart") or {}).get("attempt"),
    }


def _engine_line(data: dict[str, Any], style: Style) -> str:
    e = data["engine"]
    state = e.get("state") or "stopped"
    model = e.get("model") or DASH
    label = state_label(e)
    if state == "stopped":
        return "stopped · no model loaded · load one with: splash load <ID>"
    if state == "starting":
        tail = " · resolving on Hugging Face" if e.get("phase") == "installing" else ""
        return f"{label} · {model}{tail}"
    if state == "busy":
        decode = fmt_tps(data["speed"].get("decode_tps"))
        return f"{styled_state(style, label)} · {decode} · {model}"
    if state == "idle_released":
        minutes = data.get("_idle_minutes")
        after = f" after {minutes} min" if minutes else ""
        return f"idle · weights released{after} · reloads on the next request"
    if state == "recovering":
        return f"recovering · {data.get('_transport_error') or DASH}"
    if state in ("failed", "engine_failed"):
        return f"{style.error('failed')} · {e.get('error') or DASH} · restart with: splash restart"
    if state == "crashed":
        attempt = data.get("_restart_attempt")
        return f"restarting · {model}" + (f" · attempt {attempt}" if attempt else "")
    version = f"Splash {e['engine_version']}" if e.get("engine_version") else None
    return " · ".join(p for p in (styled_state(style, label), version, model) if p)


def status_lines(data: dict[str, Any], style: Style) -> list[str]:
    """`splash status` for people (docs/ui/11 §4.1)."""
    m, e = data["manager"], data["engine"]
    pad = " " * 11
    manager = [
        "running",
        m["url"],
        f"pid {m['pid']}" if m.get("pid") else None,
        f"Splash GUI {m['version']}" if m.get("version") else None,
        f"up {fmt_duration(m['uptime_s'])}" if m.get("uptime_s") is not None else None,
    ]
    lines = ["Manager    " + " · ".join(p for p in manager if p)]
    lines.append("Engine     " + _engine_line(data, style))
    state = e.get("state") or "stopped"
    if state in SERVING_STATES:
        details = [
            {"mlx": "MLX 4-bit", "gguf": "GGUF"}.get(e.get("format") or ""),
            e.get("variant") if e.get("format") == "gguf" else None,
            f"{fmt_tokens(e['max_context'])} context" if e.get("max_context") else None,
            "vision" if e.get("vision") else None,
            f"draft {e['draft']}" if e.get("draft") else None,
            f"kv {e['kv_format']}" if e.get("kv_format") else None,
            f"up {fmt_duration(e['uptime_s'])}" if e.get("uptime_s") is not None else None,
        ]
        lines.append(pad + style.dim(" · ".join(p for p in details if p)))
        mem, speed, req, cache = data["memory"], data["speed"], data["requests"], data["cache"]
        limit = f" of {fmt_bytes(mem['limit_bytes'])} Metal limit" if mem.get("limit_bytes") else ""
        lines.append(
            f"Memory     {fmt_bytes(mem.get('current_bytes'))}{limit} · peak "
            f"{fmt_bytes(mem.get('peak_bytes'))} · host pressure "
            f"{mem.get('system_pressure') or DASH}"
        )
        lines.append(
            f"Speed      {fmt_tps(speed.get('decode_tps'))} decode · "
            f"{fmt_tps(speed.get('prefill_tps'))} prefill · TTFT p50 "
            f"{fmt_ms(speed.get('ttft_p50_ms'))} · p95 {fmt_ms(speed.get('ttft_p95_ms'))}"
        )
        lines.append(
            f"Requests   {fmt_count(req.get('completed'))} completed · "
            f"{fmt_count(req.get('failed'))} failed · {fmt_count(req.get('in_flight'))} in flight"
            f" · {fmt_count(req.get('queued'))} queued"
        )
        ssd = DASH
        if cache.get("disk_capacity_bytes"):
            ssd = (
                f"{fmt_bytes(cache.get('disk_used_bytes'))} of "
                f"{fmt_bytes(cache['disk_capacity_bytes'])}"
            )
        persistent = {True: "on", False: "off"}.get(cache.get("persistent"), DASH)
        where = (
            f" ({home_relative(cache['dir'])})"
            if cache.get("persistent") and cache.get("dir")
            else ""
        )
        lines.append(
            f"Cache      hit rate {fmt_percent(cache.get('hit_rate'), 0)} · reused "
            f"{fmt_count(cache.get('reused_tokens'))} tokens · SSD {ssd} · persistent "
            f"{persistent}{where}"
        )
    ep = data["endpoints"]
    scope = "This Mac only" if ep["host"] in ("127.0.0.1", "localhost", "::1") else "LAN"
    key = "required" if ep["api_key_required"] else "off"
    lines += [
        f"Endpoints  OpenAI  {ep['openai']}",
        f"{pad}Anthropic  {ep['anthropic']}",
        f"{pad}API key  {key} ({scope})",
    ]
    return lines


def cmd_status(ctx: Ctx, client: Client) -> int:
    if not client.running():
        if ctx.json:
            ctx.emit_json({"manager": {"running": False, "url": client.url}})
        else:
            print("Manager    not running · start with: splash start")
        return EXIT_MANAGER_DOWN
    data = status_data(client)
    if ctx.json:
        ctx.emit_json({k: v for k, v in data.items() if not k.startswith("_")})
    else:
        print("\n".join(status_lines(data, ctx.out)))
    return 0


def ps_row(engine: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    memory = live.get("memory") or {}
    return {
        "model": engine.get("model"),
        "state": engine.get("state"),
        "uptime_s": engine.get("uptime_s"),
        "in_flight": engine.get("requests_in_flight", 0),
        "queued": engine.get("queued", 0),
        "memory_bytes": memory.get("current_bytes"),
        "limit_bytes": memory.get("limit_bytes"),
        "decode_tps": (live.get("throughput") or {}).get("decode_tps"),
    }


def memory_pair(current: Any, limit: Any) -> str:
    """`23.1 / 48.0 GB` (docs/ui/11 §5.1)."""
    gib = 1024**3
    if isinstance(current, int | float) and isinstance(limit, int | float) and limit:
        return f"{current / gib:.1f} / {limit / gib:.1f} GB"
    return fmt_bytes(current)


def cmd_ps(ctx: Ctx, client: Client) -> int:
    require(client)
    engine = client.request("GET", "/engine")
    if engine.get("state") == "stopped" or not engine.get("model"):
        if ctx.json:
            ctx.emit_json([])
        print("No model loaded.", file=sys.stderr)
        return 0
    row = ps_row(engine, snapshot(client))
    if ctx.json:
        ctx.emit_json([row])
        return 0
    label = state_label(engine)
    if engine.get("state") == "busy":
        label += " · " + fmt_tps(row["decode_tps"])
    table = Table(["MODEL", "STATE", "UPTIME", "IN FLIGHT", "QUEUED", "MEMORY"])
    table.rows.append(
        [
            str(row["model"]),
            styled_state(ctx.out, label),
            fmt_duration(row["uptime_s"]),
            fmt_count(row["in_flight"]),
            fmt_count(row["queued"]),
            memory_pair(row["memory_bytes"], row["limit_bytes"]),
        ]
    )
    width = None if ctx.args.wide else terminal_width(sys.stdout)
    print("\n".join(table.render(ctx.out, width)))
    return 0


def ls_status(model: dict[str, Any]) -> str:
    """The §9.5 vocabulary in lowercase; blank when ready."""
    status = model.get("status")
    if status == "downloading":
        progress = model.get("progress")
        return f"downloading {round(progress * 100)} %" if progress is not None else "downloading"
    if status == "update_available":
        return "update available"
    if status == "broken":
        return "broken (re-verify)"
    if status == "ready":
        return "pinned" if model.get("pinned") else ""
    return str(status or "")


def ls_sorted(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Active first, then by last used (most recent first), then name."""

    def stamp(model: dict[str, Any]) -> float:
        moment = parse_time(model.get("last_used_at"))
        return -moment.timestamp() if moment else float("inf")

    return sorted(models, key=lambda m: (m.get("status") != "active", stamp(m), str(m.get("id"))))


def recommended_model(client: Client) -> str:
    """The catalog's recommendation for this Mac's memory, for empty states."""
    try:
        catalog = client.request("GET", "/catalog")
    except (CliError, httpx.HTTPError):
        return "OWNER/REPO[:VARIANT]"
    for family in catalog.get("families", []):
        for group in family.get("groups", []):
            for entry in group.get("entries", []):
                if entry.get("recommended"):
                    variant = entry.get("recommended_variant")
                    model = str(entry["id"])
                    return f"{model}:{variant}" if variant and ":" not in model else model
    return "OWNER/REPO[:VARIANT]"


def cmd_ls(ctx: Ctx, client: Client) -> int:
    require(client)
    data = client.request("GET", "/models")
    models = ls_sorted(list(data.get("models", [])))
    if ctx.json:
        ctx.emit_json(
            [
                {
                    "id": m["id"],
                    "family": m.get("family"),
                    "format": m.get("format"),
                    "variant": m.get("variant"),
                    "language_only": m.get("language_only", False),
                    "size_bytes": m.get("size_bytes"),
                    "unique_bytes": m.get("unique_bytes"),
                    "revision": m.get("revision"),
                    "pinned": m.get("pinned", False),
                    "last_used": m.get("last_used_at"),
                    "status": m.get("status"),
                }
                for m in models
            ]
        )
    if not models:
        ctx.note(f"No models installed. Download one with: splash pull {recommended_model(client)}")
        return 0
    if not ctx.json:
        table = Table(["MODEL", "FORMAT", "SIZE", "UNIQUE", "LAST USED", "STATUS"], truncate=0)
        marks = []
        for m in models:
            active = m.get("status") == "active"
            marks.append(ctx.out.accent("*") if active else " ")
            table.rows.append(
                [
                    str(m["id"]),
                    format_label(m),
                    fmt_bytes(m.get("size_bytes")),
                    DASH
                    if m.get("status") in ("downloading", "paused")
                    else fmt_bytes(m.get("unique_bytes")),
                    fmt_relative(m["last_used_at"]) if m.get("last_used_at") else "never",
                    ls_status(m),
                ]
            )
        width = None if ctx.args.wide else terminal_width(sys.stdout)
        lines = table.render(ctx.out, None if width is None else width - 2)
        print("\n".join(f"{mark} {line}" for mark, line in zip([" ", *marks], lines, strict=True)))
    disk = data.get("disk") or {}
    ctx.note(
        f"{plural(len(models), 'model')} · {fmt_bytes(disk.get('models_bytes'))} on disk"
        f" · {fmt_bytes(disk.get('free_bytes'))} free"
    )
    return 0


# version / config / doctor ---------------------------------------------------------


def cmd_version(ctx: Ctx, client: Client) -> int:
    """Works without the manager: the engine comes from local discovery then."""
    if client.running():
        versions = client.request("GET", "/versions")
        engine = versions.get("engine") or {}
        data = {
            "gui": versions.get("gui"),
            "manager": versions.get("manager"),
            "engine": engine.get("version"),
            "engine_path": engine.get("cli"),
            "status_schema": versions.get("status_schema_version"),
        }
    else:
        info = discover(
            client.settings.current.global_.engine.path, shim_paths=(client.paths.shim,)
        )
        data = {
            "gui": __version__,
            "manager": None,
            "engine": info.version,
            "engine_path": str(info.cli) if info.cli else None,
            "status_schema": None,
        }
    if ctx.json:
        ctx.emit_json(data)
        return 0
    cli = data["engine_path"]
    prefix = home_relative(Path(cli).parent.parent) if cli else None
    engine_text = (
        f"Splash {data['engine']}" + (f" ({prefix})" if prefix else "")
        if data["engine"]
        else "Splash not found"
    )
    parts = [
        f"Splash GUI {data['gui']}",
        f"manager {data['manager']}" if data["manager"] else "manager not running",
        engine_text,
        f"status schema {data['status_schema']}" if data["status_schema"] is not None else None,
    ]
    print(" · ".join(p for p in parts if p))
    return 0


def split_key(key: str) -> list[str]:
    """`models."mlx-community/X".serve.max_context` → its segments."""
    parts, current, quoted = [], "", False
    for char in key:
        if char == '"':
            quoted = not quoted
        elif char == "." and not quoted:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    if quoted or any(not p for p in parts):
        raise CliError(f"Invalid setting key: {key}", exit_code=EXIT_USAGE)
    return parts


def nest(values: dict[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for dotted, value in values.items():
        target = document
        *head, last = dotted.split(".")
        for part in head:
            target = target.setdefault(part, {})
        target[last] = value
    return document


def config_value(client: Client, key: str | None) -> Any:
    """The effective value of `key` (global or per-model), or the whole document.
    Secrets live in the Keychain, never in these values."""
    if key is None:
        values = client.request("GET", "/settings/effective")["values"]
        return nest({k: v["value"] for k, v in values.items()})
    parts = split_key(key)
    model = None
    if parts[0] == "models" and len(parts) > 2:
        model, parts = parts[1], parts[2:]
    elif parts[0] == "global":
        parts = parts[1:]
    path = "/settings/effective" + (f"?{httpx.QueryParams({'model': model})}" if model else "")
    values = {k: v["value"] for k, v in client.request("GET", path)["values"].items()}
    dotted = ".".join(parts)
    if dotted in values:
        return values[dotted]
    # A key inside a dict-valued setting (integrations.claude_desktop.slots.X) …
    for name, value in values.items():
        if dotted.startswith(name + ".") and isinstance(value, dict):
            target: Any = value
            for part in dotted[len(name) + 1 :].split("."):
                if not isinstance(target, dict) or part not in target:
                    break
                target = target[part]
            else:
                return target
    # … or a whole section (serve, routing).
    section = {k[len(dotted) + 1 :]: v for k, v in values.items() if k.startswith(dotted + ".")}
    if section:
        return nest(section)
    raise CliError(
        f"Unknown setting: {key}", exit_code=EXIT_USAGE, fix="list them with: splash config get"
    )


def flatten(document: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(document, dict) and document:
        rows = []
        for key, value in document.items():
            rows += flatten(value, f"{prefix}.{key}" if prefix else key)
        return rows
    return [(prefix, document)]


def show_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def cmd_config(ctx: Ctx, client: Client) -> int:
    require(client)
    args = ctx.args
    if args.operation == "get":
        value = config_value(client, args.key)
        if ctx.json:
            ctx.emit_json(value)
        elif isinstance(value, dict):
            for key, item in flatten(value, args.key or ""):
                print(f"{key} = {show_value(item)}")
        else:
            print(show_value(value))
        return 0
    if args.key is None:
        raise CliError(f"config {args.operation} needs a key", exit_code=EXIT_USAGE)
    if args.operation == "set" and args.value is None:
        raise CliError(
            "config set needs a value",
            exit_code=EXIT_USAGE,
            fix=f"splash config set {args.key} <value>",
        )
    document = client.request("GET", "/settings")["settings"]
    parts = split_key(args.key)
    if parts[0] == "models" and len(parts) > 2:
        path = parts
    else:
        path = ["global", *(parts[1:] if parts[0] == "global" else parts)]
    target = document
    for key in path[:-1]:
        target = target.setdefault(key, {})
    if args.operation == "unset":
        target.pop(path[-1], None)
    else:
        try:
            value = json.loads(args.value)
        except (ValueError, TypeError):
            value = args.value
        target[path[-1]] = value
    result = client.request("PUT", "/settings", document) or {}
    if result.get("restart_required"):
        ctx.note("Saved. Takes effect on the next engine start (restart now: splash restart).")
    else:
        ctx.note("Saved.")
    return 0


def cmd_doctor(ctx: Ctx, client: Client) -> int:
    """Local checks always; the manager's `POST /doctor` items when it answers."""
    if getattr(ctx.args, "uninstall", False):
        return cmd_uninstall(ctx, client)
    paths = client.paths
    local: list[doctor_checks.Check] = []
    manager: list[doctor_checks.Check] = []
    models_bytes = None
    state = client.probe()
    if state == "ours":
        pid, _ = manager_pid(paths)
        title = "Running · " + client.url + (f" · pid {pid}" if pid else "")
        local.append(doctor_checks.Check("manager", "ok", title))
        report = client.request("POST", "/doctor")
        manager = doctor_checks.from_manager(report.get("checks", []))
        try:
            models_bytes = (client.request("GET", "/models").get("disk") or {}).get("models_bytes")
        except CliError:
            models_bytes = None
    else:
        if state == "other":
            local.append(
                doctor_checks.Check(
                    "manager",
                    "fail",
                    f"Another server (not Splash GUI) answers on port {client.port}",
                    fix=["move one of them, or pass --port with Splash GUI's port"],
                )
            )
        else:
            local.append(
                doctor_checks.Check(
                    "manager", "warn", f"Not running (port {client.port})", fix=["splash start"]
                )
            )
        local.append(
            doctor_checks.engine_check(
                discover(client.settings.current.global_.engine.path, shim_paths=(paths.shim,))
            )
        )
    local.append(
        doctor_checks.shell_check(paths, doctor_checks.home_dir(), doctor_checks.run_shell)
    )
    local.append(doctor_checks.disk_check(client.settings.models_dir(), models_bytes))
    local.append(doctor_checks.permissions_check(paths))
    checks = doctor_checks.merge(manager, local)
    counts = doctor_checks.summary(checks)
    if ctx.json:
        ctx.emit_json({"checks": [c.as_json() for c in checks], "summary": counts})
    else:
        print("\n".join(doctor_checks.render(checks, ctx.out)))
    return 1 if counts["fail"] else 0


def agent_label() -> str:
    """The manager LaunchAgent's label: the bundle's `SplashGUIAgentLabel` when this runs from
    the app (PKG-1), else the development bundle's default."""
    root = packaged.bundle_root()
    if root is not None:
        with contextlib.suppress(Exception):
            info = plistlib.loads((root / "Contents" / "Info.plist").read_bytes())
            label = info.get("SplashGUIAgentLabel")
            if isinstance(label, str) and label:
                return label
    return "ai.splashgui.manager"


def agent_pid(label: str) -> int | None:
    """The pid launchd reports for `gui/<uid>/<label>`, or None when it is not loaded."""
    try:
        out = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"^\s*pid = (\d+)", out.stdout, re.MULTILINE)
    return int(found.group(1)) if out.returncode == 0 and found else None


def cmd_uninstall(ctx: Ctx, client: Client) -> int:
    """`splash doctor --uninstall` (SPEC §19, PKG-12): the manager's `POST /uninstall`, with each
    size shown and models and cache asked about separately. When the manager runs as the app's
    LaunchAgent, the agent is booted out afterwards (a stopped KeepAlive agent would start again);
    only when launchd's pid for it is this manager's, so another install is never touched."""
    require(client)
    args = ctx.args
    plan = client.request("POST", "/uninstall/plan")
    interactive = sys.stdin.isatty()
    if not args.yes and not interactive:
        raise CliError("Remove Splash GUI data? Pass --yes to confirm.", exit_code=EXIT_USAGE)
    summary = sys.stderr if ctx.json else sys.stdout  # --json keeps stdout for the result
    print(f"Splash GUI data in {plan['home']}:", file=summary)
    print(f"  data (settings, chats, usage, logs)  {fmt_bytes(plan['data_bytes'])}", file=summary)
    print(f"  models                               {fmt_bytes(plan['models_bytes'])}", file=summary)
    print(f"  cache                                {fmt_bytes(plan['cache_bytes'])}", file=summary)
    for step in plan["steps"]:
        print(f"  · {step}", file=summary)
    delete_models = bool(args.delete_models)
    delete_cache = bool(args.delete_cache)
    if not args.yes:
        if not delete_models and plan["models_bytes"]:
            answer = input(f"Also delete models ({fmt_bytes(plan['models_bytes'])})? [y/N] ")
            delete_models = answer.strip().lower() == "y"
        if not delete_cache and plan["cache_bytes"]:
            answer = input(f"Also delete the cache ({fmt_bytes(plan['cache_bytes'])})? [y/N] ")
            delete_cache = answer.strip().lower() == "y"
        if input("Remove Splash GUI data now? [y/N] ").strip().lower() != "y":
            ctx.note("Nothing was removed.")
            return EXIT_FAILED
    label = agent_label()
    pid, _ = manager_pid(client.paths)
    by_agent = pid is not None and agent_pid(label) == pid
    result = client.request(
        "POST",
        "/uninstall",
        {
            "delete_data": True,
            "delete_models": delete_models,
            "delete_cache": delete_cache,
            "stop": not by_agent,
        },
    )
    if by_agent:
        subprocess.run(
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    if ctx.json:
        ctx.emit_json(result)
        return 0
    restored = ", ".join(result["restored"]) or "nothing connected"
    print(f"Restored: {restored}")
    print(f"PATH block removed from: {', '.join(result['path_block_removed']) or 'none'}")
    print(f"Deleted {len(result['deleted'])} items ({fmt_bytes(result['freed_bytes'])}); kept:")
    for kept in result["kept"]:
        print(f"  {kept}")
    print("The manager has stopped. Drag Splash GUI.app to the Trash to finish.")
    return 0


# load / run / launch ---------------------------------------------------------------


def choose_model(ctx: Ctx, client: Client, requested: str | None) -> str:
    """`--model`, else the active model, else routing.default_model, else a
    numbered choice on a TTY (docs/ui/11 §7, §9.1 step 2)."""
    if requested:
        return requested
    engine = client.request("GET", "/engine")
    if engine.get("model") and engine.get("state") not in NOT_SERVING:
        return str(engine["model"])
    default = client.settings.current.global_.routing.default_model
    if default:
        return default
    installed = [m for m in client.installed() if m.get("status") not in ("downloading", "paused")]
    if not installed:
        raise CliError(
            "No models installed.",
            exit_code=EXIT_MODEL,
            fix=f"splash pull {recommended_model(client)}",
        )
    if not sys.stdin.isatty():
        raise CliError(NO_MODEL, exit_code=EXIT_USAGE)
    ordered = ls_sorted(installed)
    fallback = most_recent(ordered) or str(ordered[0]["id"])
    print("No model is loaded. Which one should Splash serve?", file=sys.stderr)
    table = Table(["", "", "", ""], truncate=None)
    for index, m in enumerate(ordered, 1):
        used = m.get("last_used_at")
        table.rows.append(
            [
                f"{index}) {m['id']}",
                format_label(m),
                fmt_bytes(m.get("size_bytes")),
                f"last used {fmt_relative(used)}" if used else "",
            ]
        )
    for line in table.render(Style())[1:]:
        print("  " + line, file=sys.stderr)
    default_index = next(i for i, m in enumerate(ordered, 1) if m["id"] == fallback)
    while True:
        try:
            answer = input(f"Choose [{default_index}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if answer.lower() == "q":
            raise KeyboardInterrupt
        if not answer:
            return fallback
        if answer.isdigit() and 1 <= int(answer) <= len(ordered):
            return str(ordered[int(answer) - 1]["id"])
        if any(answer == m["id"] or answer.startswith(f"{m['id']}:") for m in ordered):
            return answer
        print(f"Type a number from 1 to {len(ordered)}, or q to cancel.", file=sys.stderr)


def ensure_loaded(ctx: Ctx, client: Client, model: str) -> dict[str, Any]:
    """Load `model` (ID[:profile]) unless it is already serving; wait for it."""
    base = base_model(model, client.installed())
    engine = client.request("GET", "/engine")
    if engine.get("model") == base and engine.get("state") in SERVING_STATES:
        return dict(engine)
    # Already starting this model (perhaps downloading its files): just wait. A
    # second load would get 409 install_in_progress (docs/api.md §3).
    if not (engine.get("model") == base and engine.get("state") == "starting"):
        client.request("POST", "/engine/load", {"model": base})
    timeout = client.settings.current.global_.routing.load_timeout
    started = time.monotonic()
    deadline = started + timeout
    tty = sys.stderr.isatty() and not ctx.quiet
    last = None
    if tty:
        ctx.note(f"Loading {base}")
    while time.monotonic() < deadline:
        engine = client.request("GET", "/engine")
        state = engine.get("state")
        if state in SERVING_STATES and engine.get("model") == base:
            if tty:
                print("\r\033[K", end="", file=sys.stderr)
            return dict(engine)
        if state in NOT_SERVING and (engine.get("error") or time.monotonic() - started > 2):
            error = engine.get("error") or {}
            suggestions = [s.get("label") for s in error.get("suggestions") or [] if s.get("label")]
            raw = error.get("raw") or []
            raise CliError(
                str(error.get("message") or f"{base} failed to load"),
                exit_code=EXIT_ENGINE,
                detail=("Splash: " + raw[-1]) if raw else None,
                fix="; ".join(suggestions) if suggestions else "see: splash logs",
                code=str(error.get("code") or "engine_failed"),
            )
        label = state_label(engine)
        if label != last:
            if tty:
                print(f"\r\033[K  {label}…", end="", file=sys.stderr, flush=True)
            else:
                ctx.note(f"load {base}: {label}")
            last = label
        time.sleep(0.2)
    raise CliError(
        f"Timed out after {fmt_duration(timeout)} waiting for the engine. "
        "It may still be loading: splash status",
        exit_code=EXIT_ENGINE,
        code="startup_timeout",
    )


def cmd_load(ctx: Ctx, client: Client) -> int:
    client.start(note=not ctx.quiet)
    started = time.monotonic()
    engine = ensure_loaded(ctx, client, ctx.args.model)
    base = str(engine.get("model"))
    parts = [f"{base} ready in {time.monotonic() - started:.1f} s"]
    if engine.get("maximum_context_tokens"):
        parts.append(f"{fmt_tokens(engine['maximum_context_tokens'])} context")
    if engine.get("vision"):
        parts.append("vision")
    ctx.ok(" · ".join(parts))
    if ctx.args.model != base:
        ctx.note(
            "Note: profiles apply per request. Use ID:profile in your client, or: "
            f"splash run {ctx.args.model}"
        )
    return 0


def restore_desktop(client: Client, name: str) -> int:
    """`splash launch claude-desktop|codex-app --restore` (SPEC §11.3, §11.4): an explicit
    Disconnect. With the manager down the restore runs here, from `state.json`, and it
    reopens the app when it was running, as Disconnect does through the manager."""
    if client.running():
        client.request("POST", f"/integrations/{name}/disconnect")
        return 0
    import asyncio

    from ..app import AppConfig, build_state
    from ..integrations.service import IntegrationsService

    state = build_state(AppConfig(paths=client.paths))
    service = IntegrationsService(state)

    async def restore() -> None:
        await service.start()
        await service.restore_one(name, reopen=True)

    asyncio.run(restore())
    return 0


def connect_desktop(ctx: Ctx, client: Client, name: str) -> int:
    """`splash launch claude-desktop|codex-app` (SPEC §11.3.1 step 1, docs/ui/11 §9.4).
    Connecting restarts a running app, so the manager answers 409
    `restart_confirmation_required` until the restart is confirmed. On a TTY the user is
    asked first; off a TTY the command fails and nothing is changed."""
    client.start(note=not ctx.quiet)
    path = f"/integrations/{name}/connect"
    try:
        client.request("POST", path, {"confirm_restart": False})
        return 0
    except ApiFailure as error:
        if error.code != "restart_confirmation_required":
            raise
    label = "Claude" if name == "claude-desktop" else "Codex"
    if not sys.stdin.isatty():
        raise CliError(
            f"{label} is running, and connecting restarts it.",
            exit_code=EXIT_USAGE,
            fix=f"quit {label} and run this again, or run it in a terminal to confirm",
        )
    try:
        answer = input(
            f"{label} will restart. Your previous configuration is backed up and restored "
            "when you disconnect or quit Splash GUI. Continue? [y/N] "
        )
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        ctx.note("Not connected.")
        return EXIT_FAILED
    client.request("POST", path, {"confirm_restart": True})
    return 0


def launch(ctx: Ctx, client: Client, passthrough_args: list[str]) -> int:
    args = ctx.args
    if args.client in DESKTOP_CLIENTS:
        if args.print_only:
            action = (
                "restore saved configuration"
                if args.restore
                else "back up configuration, connect to Splash, and restart app"
            )
            print(f"{args.client}: {action}")
            return 0
        if args.restore:
            return restore_desktop(client, args.client)
        return connect_desktop(ctx, client, args.client)
    loaded: str | None = None
    if args.print_only:
        # `--print` changes nothing (docs/ui/11 §9.3): it never starts the manager
        # or loads a model, and never asks. The model is the one a launch would
        # start without asking: --model, active, routing default, most recent.
        running = client.running()
        model = args.model
        default = client.settings.current.global_.routing.default_model
        if running:
            engine = client.request("GET", "/engine")
            if engine.get("state") not in NOT_SERVING:
                loaded = engine.get("model")
            model = model or loaded or default or most_recent(client.installed())
        else:
            model = model or default
        if not model:
            raise CliError(NO_MODEL, exit_code=EXIT_USAGE)
    else:
        client.start(note=not ctx.quiet)
        running = True
        model = choose_model(ctx, client, args.model)
        loaded = ensure_loaded(ctx, client, model).get("model")
    engine_info = discover(
        client.settings.current.global_.engine.path, shim_paths=(client.paths.shim,)
    )
    if not engine_info.python or not engine_info.pkg:
        raise CliError(
            "Splash is not installed.", exit_code=EXIT_MISSING, fix="brew install incoai/tap/splash"
        )
    entry: dict[str, Any] = {}
    if running:
        data = client.http.get("/v1/models", headers=client.headers()).json().get("data", [])
        entry = next((m for m in data if m["id"] == model), data[0] if data else {})
    environment = dict(os.environ)
    # Not PYTHONPATH: the helper puts it on sys.path, so the client's own
    # environment stays the user's (D18).
    environment["SPLASH_GUI_ENGINE_PKG"] = str(engine_info.pkg)
    environment["SPLASH_PORT"] = str(client.port)
    # SPEC §11.2 step 3: the key only when the public port requires it. Hermes and
    # Pi profiles reference it as ${SPLASH_API_KEY} / $SPLASH_API_KEY and read it
    # from this environment (install/clients.py:415-417, :463 at 1.3.0).
    if client.settings.current.global_.security.api_key_required:
        key = SecretStore(backend_from_env(client.paths)).get(SecretName.API_KEY)
        if key:
            environment["SPLASH_API_KEY"] = key
    environment["SPLASH_GUI_CLIENT_SPEC"] = json.dumps(
        {
            "client": args.client,
            "model": model,
            "url": client.url,
            "context": entry.get("context_length", 262144),
            "modalities": entry.get("input_modalities", ["text"]),
            "args": passthrough_args,
            "print": args.print_only,
            # D60: the profile's effort becomes the client's own per-run option.
            "reasoning_effort": profile_reasoning_effort(client.settings.current, model, loaded),
        }
    )
    helper = Path(__file__).parents[1] / "helpers" / "launch_client.py"
    if args.print_only:
        launcher = f"Splash {engine_info.version} launcher" if engine_info.version else "Splash"
        print(f"# splash launch {args.client} --print  ({launcher}, session only)")
        if loaded is None or (model != loaded and not str(model).startswith(f"{loaded}:")):
            print(f"# {model} is not loaded now; Splash GUI loads it on the first request")
        sys.stdout.flush()
    else:
        record_launch(client.paths, args.client, model)
        ctx.note(
            f"{ctx.err.glyph('arrow')} {args.client} on {model} · {client.url} · session only "
            f"(plain `{args.client}` is unchanged)"
        )
    os.execve(str(engine_info.python), [str(engine_info.python), str(helper)], environment)  # noqa: S606
    return 0  # type: ignore[unreachable]  # only when execve is stubbed (tests)


def record_launch(paths: Paths, name: str, model: str | None) -> None:
    """SPEC §11.2 step 4: a session row in usage.db for "last launched"."""
    from ..usage.db import UsageDB

    try:
        db = UsageDB(paths.usage_db)
        try:
            db.record_launch(name, model)
        finally:
            db.close()
    except Exception as error:  # never block a launch on bookkeeping
        print(f"splash: could not record the launch: {error}", file=sys.stderr)


def chat(client: Client, model: str, prompt: list[str]) -> None:
    messages: list[dict[str, str]] = []
    while True:
        try:
            text = " ".join(prompt) if prompt else input(">>> ")
        except EOFError:
            break
        if text in ("/exit", "/bye", "/quit"):
            break
        if text == "/clear":
            messages.clear()
            continue
        if text == "/help":
            print("/clear — forget the conversation; /bye — leave")
            continue
        if not text.strip():
            continue
        messages.append({"role": "user", "content": text})
        answer = ""
        with client.http.stream(
            "POST",
            "/v1/chat/completions",
            headers=client.headers(),
            json={"model": model, "messages": messages, "stream": True},
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise api_failure(response)
            for line in response.iter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                event = json.loads(line[5:])
                for choice in event.get("choices", []):
                    part = choice.get("delta", {}).get("content") or ""
                    print(part, end="", flush=True)
                    answer += part
        print()
        messages.append({"role": "assistant", "content": answer})
        if prompt:
            break


# Mutating commands -------------------------------------------------------------------


def cmd_rm(ctx: Ctx, client: Client) -> int:
    require(client)
    model = ctx.args.model
    if not ctx.args.yes:
        if not sys.stdin.isatty():
            raise CliError(f"Delete {model}? Pass --yes to confirm.", exit_code=EXIT_USAGE)
        row = next((m for m in client.installed() if m["id"] == model), None)
        frees = f" Frees {fmt_bytes(row['unique_bytes'])} (unique)." if row else ""
        if input(f"Delete {model}?{frees} [y/N] ").strip().lower() != "y":
            ctx.note("Not deleted.")
            return EXIT_FAILED
    try:
        result = client.request("DELETE", "/models/" + model)
    except ApiFailure as error:
        # The loaded model needs its own confirmation: deleting it stops the engine.
        if error.code != "model_active":
            raise
        if not ctx.args.yes and (
            not sys.stdin.isatty()
            or input(f"{model} is loaded (active). Stop the engine and delete it? [y/N] ")
            .strip()
            .lower()
            != "y"
        ):
            raise CliError(
                f"Not deleted: {model} is the active model.", fix=f"splash rm {model} --yes"
            ) from None
        result = client.request("DELETE", "/models/" + model + "?confirm_active=true")
    ctx.ok(f"Deleted · {fmt_bytes((result or {}).get('freed_bytes'))} freed")
    return 0


def cmd_pull(ctx: Ctx, client: Client) -> int:
    args = ctx.args
    client.start(note=not ctx.quiet)
    item = client.request(
        "POST",
        "/downloads",
        {
            "id": args.model,
            "revision": args.revision,
            "draft_model": args.draft_model,
            "language_only": args.language_only,
            "verify": not args.no_verify,
        },
    )
    tty = sys.stderr.isatty() and not ctx.quiet
    started, last_line, last_state = time.monotonic(), 0.0, None
    try:
        while item["state"] not in ("done", "failed", "cancelled"):
            sizes = f"{fmt_bytes(item.get('bytes_done'))}/{fmt_bytes(item.get('bytes_total'))}"
            rate = f"{fmt_bytes(item['speed_bps'])}/s" if item.get("speed_bps") else ""
            eta = f"eta {fmt_duration(item['eta_s'])}" if item.get("eta_s") else ""
            progress = item.get("progress")
            percent = fmt_percent(progress, 0) if progress is not None else ""
            text = " ".join(p for p in (item["state"], percent, sizes, rate, eta) if p)
            if tty:
                # The overall bar (docs/ui/11 §6.2), drawn from the manager's byte progress.
                drawn = f"{ctx.err.bar(progress)} " if progress is not None else ""
                print(f"\r\033[K{args.model}: {drawn}{text}", end="", file=sys.stderr, flush=True)
            elif item["state"] != last_state or time.monotonic() - last_line >= 5:
                ctx.note(f"pull {args.model}: {text}")
                last_line, last_state = time.monotonic(), item["state"]
            time.sleep(0.5)
            item = next(
                i for i in client.request("GET", "/downloads")["items"] if i["id"] == item["id"]
            )
    except KeyboardInterrupt:
        print(
            f"\nPausing… partial files are kept. Resume with: splash pull {args.model}",
            file=sys.stderr,
        )
        with contextlib.suppress(CliError):
            client.request("POST", f"/downloads/{item['id']}/pause")
        return EXIT_INTERRUPTED
    if tty:
        print("\r\033[K", end="", file=sys.stderr)
    if item["state"] != "done":
        message = (item.get("error") or {}).get("message") or "the download was cancelled"
        raise CliError(f"Download failed: {message}")
    elapsed = fmt_duration(time.monotonic() - started)
    ctx.ok(f"Ready: {args.model} · {fmt_bytes(item.get('bytes_total'))} · {elapsed}")
    return 0


def cmd_logs(ctx: Ctx, client: Client) -> int:
    require(client)
    args = ctx.args
    source = "manager" if args.manager else "engine"
    if args.follow:
        with client.http.stream(
            "GET",
            "/api/admin/logs/" + source + "/stream",
            headers=client.headers(),
            timeout=None,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise api_failure(response)
            for line in response.iter_lines():
                if line.startswith("data:"):
                    data = json.loads(line[5:])
                    if "text" in data:
                        print(log_text(ctx, data))
    else:
        lines = max(1, min(args.lines, 10000))
        for line in client.request("GET", f"/logs/{source}?tail={lines}")["lines"]:
            print(log_text(ctx, line))
    return 0


def log_text(ctx: Ctx, line: dict[str, Any]) -> str:
    text = str(line.get("text", ""))
    return ctx.out.error(text) if line.get("level") == "error" else text


# Main --------------------------------------------------------------------------------


def require(client: Client) -> None:
    """Read-only commands never start the manager (D-11-3): exit 3 when it is down."""
    if not client.running():
        raise manager_down(getattr(client, "port", None))


def serve_port(arguments: list[str]) -> int:
    """The port a passed-through `splash serve` will bind (Splash's launcher:
    `--port`, else SPLASH_PORT, else 8000)."""
    for index, argument in enumerate(arguments):
        value = None
        if argument == "--port" and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif argument.startswith("--port="):
            value = argument.split("=", 1)[1]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return 0
    try:
        return int(os.environ.get("SPLASH_PORT", "8000"))
    except ValueError:
        return 0


def warn_if_port_taken(arguments: list[str]) -> None:
    """SPEC §12.2, docs/ui/11 §14: `splash serve` warns, without blocking, when the
    manager uses the same port."""
    try:
        store = SettingsStore(Paths.from_env())
        store.load()
        port = store.current.global_.server.port
    except Exception:
        return
    if serve_port(arguments[1:]) != port:
        return
    try:
        up = is_manager_health(httpx.get(f"http://127.0.0.1:{port}/health", timeout=1))
    except httpx.HTTPError:
        up = False
    if up:
        style = make_style("auto", sys.stderr)
        print(
            f"{style.warning('!')} Splash GUI's manager is already listening on {port}; "
            "`splash serve` will fail to bind. Use --port 9999, or stop the manager: splash stop",
            file=sys.stderr,
        )


def passthrough(arguments: list[str]) -> int:
    """D21: exec the real engine CLI with the arguments untouched."""
    engine = discover(shim_paths=(Paths.from_env().shim,))
    if not engine.cli:
        print("splash: " + str(engine.error), file=sys.stderr)
        return 127
    if arguments and arguments[0] == "serve":
        warn_if_port_taken(arguments)
    os.execv(str(engine.cli), [str(engine.cli), *arguments])  # noqa: S606
    raise SystemExit(0)  # only when execv is stubbed (tests)


def admin_url(client: Client, page: str) -> str:
    """D58: a one-time login link that lands on `page`, so the browser is signed in
    without typing the key. Falls back to the plain page (the login form) when the
    manager cannot issue one."""
    target = "/admin/" + page.strip("/")
    try:
        link = client.request("POST", "/auth/link")
    except (CliError, httpx.HTTPError):
        return client.url + target
    return f"{client.url}{link['url']}&next={urllib.parse.quote(target, safe='/')}"


def manager_exited(paths: Paths) -> bool:
    """The manager holds `run/manager.lock` for its whole life (`manager.instance_lock`),
    and the OS releases the lock at exit. A lock that can be taken means the manager has
    exited, after the lifespan shutdown restored the integrations (SPEC §4.2)."""
    lock_path = paths.run_dir / "manager.lock"
    if not lock_path.exists():
        return True
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def shut_down(client: Client) -> None:
    """Ask the manager to stop (`POST /shutdown`, SPEC §12.2) and wait until it has exited.
    The port stops answering before the restore ends, so the wait is on the lock: a
    `start` that follows never races the restore for the configuration files."""
    client.request("POST", "/shutdown")
    deadline = time.monotonic() + 30
    while not manager_exited(client.paths):
        if time.monotonic() >= deadline:
            raise CliError(
                "Splash GUI is still shutting down.",
                fix="check " + home_relative(client.paths.manager_log),
            )
        time.sleep(0.2)


def dispatch(ctx: Ctx, client: Client, rest: list[str]) -> int:
    name = ctx.args.command
    if name == "launch":
        return launch(ctx, client, rest)
    if name == "start":
        client.start(ctx.args.foreground, note=not ctx.quiet)
        ctx.ok(f"Splash GUI running at {client.url} (admin: /admin)")
        return 0
    if name == "restart":
        if client.running():
            shut_down(client)
        client.start(ctx.args.foreground, note=not ctx.quiet)
        return 0
    if name == "stop":
        if client.running():
            shut_down(client)
            ctx.ok("Splash GUI stopped")
        else:
            ctx.note("Splash GUI is not running.")
        return 0
    if name == "open":
        client.start(note=not ctx.quiet)
        webbrowser.open(admin_url(client, ctx.args.page))
        return 0
    if name == "unload":
        require(client)
        client.request("POST", "/engine/stop")
        ctx.ok("Engine stopped")
        return 0
    if name == "run":
        client.start(note=not ctx.quiet)
        ensure_loaded(ctx, client, ctx.args.model)
        chat(client, ctx.args.model, ctx.args.prompt)
        return 0
    handlers = {
        "status": cmd_status,
        "ps": cmd_ps,
        "ls": cmd_ls,
        "version": cmd_version,
        "config": cmd_config,
        "doctor": cmd_doctor,
        "load": cmd_load,
        "rm": cmd_rm,
        "pull": cmd_pull,
        "logs": cmd_logs,
    }
    return handlers[name](ctx, client)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    leading, rest = split_globals(arguments)
    command = rest[0] if rest else None
    if command is None or command == "--help":
        print(help_text(engine_commands() if command else ENGINE_COMMANDS_FALLBACK), end="")
        return 0
    if command not in COMMANDS:
        # D21: anything that is not ours goes to the real engine, untouched.
        return passthrough(arguments)
    split = rest.index("--") if "--" in rest else len(rest)
    args = parse(leading + rest[:split])
    ctx = Ctx(args)
    client: Client | None = None

    def fail(error: CliError) -> int:
        print_error(error, ctx.err, as_json=ctx.json, out=sys.stdout, err=sys.stderr)
        return error.exit_code

    try:
        client = Client(args.port)
        return dispatch(ctx, client, rest[split + 1 :])
    except CliError as error:
        return fail(error)
    except httpx.HTTPError as error:
        return fail(CliError(f"Could not talk to Splash GUI: {error}"))
    except (OSError, ValueError) as error:
        if args.verbose:
            raise
        return fail(CliError(str(error)))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        if client is not None:
            client.http.close()
