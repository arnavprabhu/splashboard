"""The Splash CLI shim. New commands use the manager; engine commands pass through."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from ..engine.discovery import discover
from ..paths import Paths
from ..secrets import SecretName, SecretStore, backend_from_env
from ..settings.store import SettingsStore

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


class Client:
    def __init__(self, port: int | None = None) -> None:
        self.paths = Paths.from_env().ensure()
        self.settings = SettingsStore(self.paths)
        self.settings.load()
        self.port = port or self.settings.current.global_.server.port
        self.url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.url, timeout=180)

    def headers(self) -> dict[str, str]:
        token = self.paths.cli_token.read_text().strip() if self.paths.cli_token.exists() else ""
        return {"Authorization": "Bearer " + token}

    def request(self, method: str, path: str, body: Any = None) -> Any:
        response = self.http.request(method, "/api/admin" + path, json=body, headers=self.headers())
        if response.status_code >= 400:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError):
                message = response.text
            raise ValueError(message)
        return response.json() if response.content else None

    def running(self) -> bool:
        try:
            return self.http.get("/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, foreground: bool = False) -> None:
        if self.running():
            return
        argv = [sys.executable, "-m", "splash_gui", "--port", str(self.port)]
        if foreground:
            os.execv(sys.executable, argv)  # noqa: S606 — fixed Python and argv, no shell
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
        raise ValueError("Manager did not start. Check " + str(self.paths.manager_log))

    def model(self, requested: str | None) -> str:
        engine = self.request("GET", "/engine")
        model = (
            requested or engine.get("model") or self.settings.current.global_.routing.default_model
        )
        installed = self.request("GET", "/models")["models"]
        if model is None and sys.stdin.isatty() and installed:
            for index, item in enumerate(installed, 1):
                print(f"{index}. {item['id']}")
            model = installed[int(input("Choose model: ")) - 1]["id"]
        if not model:
            raise ValueError("Choose a model with --model, or set routing.default_model")
        ids = [item["id"] for item in installed]
        base = next(
            (
                candidate
                for candidate in sorted(ids, key=len, reverse=True)
                if model == candidate or model.startswith(candidate + ":")
            ),
            model,
        )
        if engine.get("model") != base or engine["state"] not in ("ready", "busy", "idle_released"):
            self.request("POST", "/engine/load", {"model": base})
        deadline = time.monotonic() + self.settings.current.global_.routing.load_timeout
        while time.monotonic() < deadline:
            engine = self.request("GET", "/engine")
            if engine["state"] in ("ready", "busy", "idle_released"):
                return str(model)
            if engine["state"] in ("failed", "engine_failed"):
                raise ValueError(
                    str(engine.get("error") or "Model failed to load; see splash logs")
                )
            if sys.stderr.isatty():
                print("\rLoading " + base + "…", end="", file=sys.stderr, flush=True)
            time.sleep(0.2)
        raise ValueError("Timed out loading " + base)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="splash",
        description="Splash GUI manager and local models",
        epilog=(
            "Engine commands (passed to Splash): serve, claude, codex, opencode, "
            "hermes, pi, --version, -h"
        ),
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = commands.add_parser(name)
        sub.add_argument("--port", type=int)
        if name in ("start", "restart"):
            sub.add_argument("--foreground", action="store_true")
        if name in ("status", "ls", "ps", "doctor", "version"):
            sub.add_argument("--json", action="store_true")
        if name in ("pull", "rm", "load", "run"):
            sub.add_argument("model")
        if name == "pull":
            sub.add_argument("--revision")
            sub.add_argument("--draft-model")
            sub.add_argument("--language-only", action="store_true")
            sub.add_argument("--no-verify", action="store_true")
        if name == "rm":
            sub.add_argument("--yes", action="store_true")
        if name == "run":
            sub.add_argument("prompt", nargs="*")
        if name == "open":
            sub.add_argument("page", nargs="?", default="status")
        if name == "logs":
            sub.add_argument("-f", "--follow", action="store_true")
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--engine", action="store_true")
            group.add_argument("--manager", action="store_true")
        if name == "config":
            sub.add_argument("operation", choices=["get", "set", "unset"])
            sub.add_argument("key")
            sub.add_argument("value", nargs="?")
        if name == "launch":
            sub.add_argument(
                "client",
                choices=[
                    "claude",
                    "codex",
                    "opencode",
                    "hermes",
                    "pi",
                    "claude-desktop",
                    "codex-app",
                ],
            )
            sub.add_argument("--model")
            sub.add_argument("--print", dest="print_only", action="store_true")
            sub.add_argument("--restore", action="store_true")
    return root


def launch(client: Client, args: argparse.Namespace, passthrough: list[str]) -> int:
    if args.client in ("claude-desktop", "codex-app"):
        if args.print_only:
            action = (
                "restore saved configuration"
                if args.restore
                else "back up configuration, connect to Splash, and restart app"
            )
            print(f"{args.client}: {action}")
            return 0
        if args.restore and not client.running():
            import asyncio

            from ..app import AppConfig, build_state
            from ..integrations.service import IntegrationsService

            state = build_state(AppConfig(paths=client.paths))
            service = IntegrationsService(state)

            async def restore() -> None:
                await service.start()
                await service.restore_one(args.client, reopen=False)

            asyncio.run(restore())
            return 0
        client.start()
        action = "disconnect" if args.restore else "connect"
        client.request("POST", f"/integrations/{args.client}/{action}", {"confirm_restart": True})
        return 0
    client.start()
    model = client.model(args.model)
    engine = discover(client.settings.current.global_.engine.path, shim_paths=(client.paths.shim,))
    if not engine.python or not engine.pkg:
        raise ValueError("Splash is not installed")
    data = client.http.get("/v1/models", headers=client.headers()).json()["data"]
    entry = next((m for m in data if m["id"] == model), data[0])
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(engine.pkg)
    environment["SPLASH_PORT"] = str(client.port)
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
            "args": passthrough,
            "print": args.print_only,
        }
    )
    helper = Path(__file__).parents[1] / "helpers" / "launch_client.py"
    if not args.print_only:
        record_launch(client.paths, args.client, model)
    os.execve(str(engine.python), [str(engine.python), str(helper)], environment)  # noqa: S606
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


def print_status(client: Client, engine: dict[str, Any], *, full: bool) -> None:
    """`splash status` / `splash ps` for people (`--json` prints the raw view)."""
    state = engine.get("state", "stopped")
    model = engine.get("model") or "—"
    print(f"Engine:   {state}  {model}")
    if engine.get("uptime_s") is not None:
        print(f"Uptime:   {engine['uptime_s']:.0f} s")
    print(f"Requests: {engine.get('requests_in_flight', 0)} in flight")
    try:
        live = client.request("GET", "/metrics/snapshot")
    except ValueError:
        live = {}
    memory = (live.get("memory") or {}).get("current_bytes")
    if memory:
        print(f"Memory:   {memory / 1024**3:.1f} GiB (Metal)")
    tps = (live.get("throughput") or {}).get("decode_tps")
    if tps is not None:
        print(f"Decode:   {tps:.0f} tok/s")
    if full:
        print(f"Manager:  {client.url}  (admin {client.url}/admin/)")
        print(f"OpenAI:   {client.url}/v1   Anthropic: {client.url}")
    if engine.get("error"):
        print(f"Error:    {engine['error'].get('message')}")


def chat(client: Client, model: str, prompt: list[str]) -> None:
    messages: list[dict[str, str]] = []
    while True:
        try:
            text = " ".join(prompt) if prompt else input(">>> ")
        except EOFError:
            break
        if text in ("/exit", "/bye"):
            break
        if text == "/clear":
            messages.clear()
            continue
        if text == "/help":
            print("/clear — clear context; /exit — exit")
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
            response.raise_for_status()
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
    """SPEC §12.2: `splash serve` warns, without blocking, when the manager uses
    the same port."""
    try:
        store = SettingsStore(Paths.from_env())
        store.load()
        port = store.current.global_.server.port
    except Exception:
        return
    if serve_port(arguments[1:]) != port:
        return
    try:
        up = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        up = False
    if up:
        print(
            f"splash: warning: Splash GUI is serving on port {port}; this server will fail "
            "to bind it. Use --port, or `splash load` to serve through Splash GUI.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] not in (*COMMANDS, "--help"):
        engine = discover(shim_paths=(Paths.from_env().shim,))
        if not engine.cli:
            print("splash: " + str(engine.error), file=sys.stderr)
            return 127
        if arguments[0] == "serve":
            warn_if_port_taken(arguments)
        os.execv(str(engine.cli), [str(engine.cli), *arguments])  # noqa: S606
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser().parse_args(arguments[:split])
    client = Client(args.port)
    try:
        name = args.command
        if name == "launch":
            return launch(client, args, arguments[split + 1 :])
        if name == "start":
            client.start(args.foreground)
            print(client.url + "/admin/")
            return 0
        if name == "restart":
            if client.running():
                client.request("POST", "/shutdown")
                deadline = time.monotonic() + 30
                while client.running() and time.monotonic() < deadline:
                    time.sleep(0.2)
                if client.running():
                    raise ValueError("Manager is still shutting down")
            client.start(args.foreground)
            return 0
        if name == "stop":
            if client.running():
                client.request("POST", "/shutdown")
            return 0
        if name == "open":
            client.start()
            webbrowser.open(client.url + "/admin/" + args.page)
            return 0
        if name in ("pull", "load", "run"):
            client.start()
        elif not client.running():
            raise ValueError("Manager is stopped. Run splash start")
        if name in ("status", "ps", "ls", "doctor", "version"):
            route = {
                "status": "/engine",
                "ps": "/engine",
                "ls": "/models",
                "doctor": "/doctor",
                "version": "/versions",
            }[name]
            data = client.request("GET", route)
            if args.json:
                print(json.dumps(data, indent=2))
            elif name in ("status", "ps"):
                print_status(client, data, full=name == "status")
            elif name == "ls":
                for model in data["models"]:
                    gib = model["size_bytes"] / 1024**3
                    print(f"{model['status']:12} {model['id']}  {model['format']}  {gib:.2f} GiB")
            elif name == "doctor":
                for check in data["checks"]:
                    print(f"{check['status'].upper():5} {check['label']}: {check['message']}")
                    if check.get("fix"):
                        print("      " + check["fix"])
                return 0 if data["ok"] else 1
            else:
                print(json.dumps(data, indent=2))
        elif name == "load":
            print(client.model(args.model))
        elif name == "unload":
            client.request("POST", "/engine/stop")
        elif name == "run":
            chat(client, client.model(args.model), args.prompt)
        elif name == "rm":
            if not args.yes and (
                not sys.stdin.isatty() or input("Delete " + args.model + "? [y/N] ").lower() != "y"
            ):
                return 1
            client.request("DELETE", "/models/" + args.model + "?confirm_active=true")
        elif name == "pull":
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
            while item["state"] not in ("done", "failed", "cancelled"):
                if sys.stderr.isatty():
                    percent = (item.get("progress") or 0) * 100
                    print(
                        f"\r{item['state']}: {percent:.1f}%  {item['bytes_done']} bytes",
                        end="",
                        file=sys.stderr,
                    )
                time.sleep(0.5)
                item = next(
                    i for i in client.request("GET", "/downloads")["items"] if i["id"] == item["id"]
                )
            if item["state"] != "done":
                raise ValueError((item.get("error") or {}).get("message", "Download cancelled"))
            print("Downloaded " + args.model)
        elif name == "logs":
            source = "manager" if args.manager else "engine"
            if args.follow:
                with client.http.stream(
                    "GET",
                    "/api/admin/logs/" + source + "/stream",
                    headers=client.headers(),
                    timeout=None,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            data = json.loads(line[5:])
                            if "text" in data:
                                print(data["text"])
            else:
                for line in client.request("GET", "/logs/" + source)["lines"]:
                    print(line["text"])
        elif name == "config":
            document = client.request("GET", "/settings")["settings"]
            path = args.key.split(".")
            if path[0] != "global":
                path.insert(0, "global")
            target = document
            for key in path[:-1]:
                target = target.setdefault(key, {})
            if args.operation == "get":
                print(json.dumps(target.get(path[-1]), indent=2))
            else:
                if args.operation == "unset":
                    target.pop(path[-1], None)
                else:
                    try:
                        value = json.loads(args.value)
                    except (ValueError, TypeError):
                        value = args.value
                    target[path[-1]] = value
                client.request("PUT", "/settings", document)
        return 0
    except (ValueError, OSError, httpx.HTTPError, IndexError) as error:
        print("splash: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        client.http.close()
