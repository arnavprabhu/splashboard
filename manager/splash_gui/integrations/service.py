"""Session-only client launch controls and reversible desktop connections (§11)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shlex
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import uvicorn
from fastapi import FastAPI, Request, Response

from ..errors import ApiError
from ..paths import write_atomic
from ..schemas import (
    CliIntegration,
    DesktopIntegration,
    EntriesRemoved,
    IntegrationChanges,
    IntegrationError,
    Integrations,
    OpenTerminalResult,
    RestoreAllResult,
)
from ..usage.db import iso
from .snapshots import encode_document, read_document, restore, snapshot

if TYPE_CHECKING:
    from ..state import ManagerState

CLIENTS = {
    "claude": ("Claude Code", "https://code.claude.com/docs/en/setup"),
    "codex": ("Codex CLI", "https://developers.openai.com/codex/cli/"),
    "opencode": ("OpenCode", "https://opencode.ai/docs/"),
    "hermes": (
        "Hermes",
        "https://hermes-agent.nousresearch.com/docs/getting-started/installation/",
    ),
    "pi": ("Pi", "https://pi.dev/"),
}
DEPLOYMENT = "c4f53a60-833b-4a8a-b15c-5641d82a5902"


class GatewayServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The manager alone owns process signal handlers.
        yield


class IntegrationsService:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.home = Path.home()
        self.records: dict[str, Any] = {}
        self.recovery: set[str] = set()
        self.lock = asyncio.Lock()
        self.gateway: uvicorn.Server | None = None
        self.gateway_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            self.records = json.loads(self.state.paths.integrations_state.read_text())
            self.recovery = set(self.records)
        except FileNotFoundError:
            pass

    def persist(self) -> None:
        write_atomic(
            self.state.paths.integrations_state, json.dumps(self.records, indent=2).encode()
        )

    def app(self, name: str) -> Path | None:
        names = ["Claude.app"] if name == "claude-desktop" else ["Codex.app", "ChatGPT.app"]
        return next(
            (
                root / filename
                for root in (self.home / "Applications", Path("/Applications"))
                for filename in names
                if (root / filename).is_dir()
            ),
            None,
        )

    def running(self, name: str) -> bool:
        return bool(
            self.state.macos.pgrep(
                "-f",
                "Claude.app/Contents/MacOS/Claude"
                if name == "claude-desktop"
                else "(Codex|ChatGPT).app/Contents/MacOS/",
            )
        )

    def desktop(self, name: str) -> DesktopIntegration:
        app = self.app(name)
        record = self.records.get(name)
        return DesktopIntegration.model_validate(
            {
                "name": name,
                "label": "Claude Desktop" if name == "claude-desktop" else "Codex app",
                "detected": app is not None,
                "app_path": str(app) if app else None,
                "version": self.state.macos.app_version(app) if app else None,
                "untested_version": True,
                "running": self.running(name),
                "state": "needs_restore"
                if name in self.recovery
                else "connected"
                if record
                else "not_connected",
                "connected_at": record.get("connected_at") if record else None,
                "warning": (
                    "The app restarts. Its previous configuration is restored when you "
                    "disconnect or quit."
                ),
            }
        )

    def listing(self) -> Integrations:
        launched = self.state.usage.last_launches()
        cli = []
        for name, (label, url) in CLIENTS.items():
            path = shutil.which(name)
            version = (
                self.state.macos.run([path, "--version"]).stdout.strip().splitlines()
                if path
                else []
            )
            cli.append(
                CliIntegration.model_validate(
                    {
                        "name": name,
                        "label": label,
                        "installed": bool(path),
                        "path": path,
                        "version": version[0] if version else None,
                        "install_url": url,
                        "command": f"splash launch {name}",
                        "changes": IntegrationChanges(
                            notes=[
                                "Splash configures this session only. Your default "
                                "provider remains unchanged."
                            ]
                        ),
                        "last_launched_at": launched.get(name),
                    }
                )
            )
        return Integrations(
            cli=cli,
            desktop=[self.desktop(n) for n in ("claude-desktop", "codex-app")],
            unclean_shutdown=bool(self.recovery),
        )

    async def quit_app(self, name: str) -> bool:
        running = await asyncio.to_thread(self.running, name)
        if not running:
            return False
        app = self.app(name)
        label = app.stem if app else "Claude" if name == "claude-desktop" else "Codex"
        result = await asyncio.to_thread(self.state.macos.quit_app, label)
        if result.returncode:
            raise ApiError(409, "Could not quit " + label, "app_quit_failed")
        deadline = time.monotonic() + (30 if name == "claude-desktop" else 5)
        while await asyncio.to_thread(self.running, name):
            if time.monotonic() >= deadline:
                raise ApiError(409, "Quit " + label + " and try again", "app_quit_timeout")
            await asyncio.sleep(0.2)
        return True

    def plans(self, name: str) -> list[tuple[Path, bytes, list[str] | None]]:
        plans: list[tuple[Path, bytes, list[str] | None]] = []

        def patch(path: Path, values: dict[str, Any], remove: tuple[str, ...] = ()) -> None:
            data = read_document(path)
            data.update(values)
            for key in remove:
                data.pop(key, None)
            plans.append((path, encode_document(path, data), list(values) + list(remove)))

        if name == "claude-desktop":
            support = self.home / "Library" / "Application Support"
            config = support / "Claude-3p" / "configLibrary"
            patch(
                config / (DEPLOYMENT + ".json"),
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": f"http://127.0.0.1:{self.state.settings.current.global_.integrations.claude_desktop.port}",
                    "inferenceGatewayApiKey": "splash",
                    "inferenceGatewayAuthScheme": "bearer",
                    "deploymentDisplayName": "Splash",
                    "chatTabEnabled": True,
                    "disableDeploymentModeChooser": True,
                    "disableTelemetry": True,
                },
                ("inferenceModels",),
            )
            meta = read_document(config / "_meta.json")
            entries = [e for e in meta.get("entries", []) if e.get("id") != DEPLOYMENT]
            patch(
                config / "_meta.json",
                {
                    "appliedId": DEPLOYMENT,
                    "entries": [*entries, {"id": DEPLOYMENT, "name": "Splash"}],
                },
            )
            for dirname in ("Claude", "Claude-3p"):
                patch(support / dirname / "claude_desktop_config.json", {"deploymentMode": "3p"})
        else:
            codex = self.home / ".codex"
            models = self.state.proxy.models_list()["data"]
            catalog = []
            for model in models:
                catalog.append(
                    {
                        "slug": model["id"],
                        "display_name": model["id"],
                        "description": "Splash local model",
                        "context_window": model.get("context_length", 262144),
                        "max_context_window": model.get("context_length", 262144),
                        "input_modalities": model.get("input_modalities", ["text"]),
                        "supported_reasoning_levels": [
                            {"effort": e, "description": e}
                            for e in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                        ],
                        "supported_in_api": True,
                        "shell_type": "unified_exec",
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "effective_context_window_percent": 95,
                    }
                )
            cache = codex / "models_cache.json"
            if cache.exists():
                native = json.loads(cache.read_text())
                for entry in native.get("models", []):
                    catalog.append({**entry, "supported_in_api": False})
            folder = self.state.paths.codex_app_dir
            plans.extend(
                [
                    (folder / "models.json", json.dumps({"models": catalog}).encode(), None),
                    (folder / "routing.json", json.dumps([m["id"] for m in models]).encode(), None),
                ]
            )
            config = codex / "config.toml"
            old = read_document(config)
            desktop = dict(old.get("desktop", {}))
            desktop["enabled-reasoning-efforts"] = list(
                dict.fromkeys([*desktop.get("enabled-reasoning-efforts", []), "none", "max"])
            )
            values: dict[str, Any] = {
                "openai_base_url": f"http://127.0.0.1:{self.state.settings.current.global_.server.port}/api/codex/v1",
                "model_catalog_json": str(folder / "models.json"),
                "desktop": desktop,
            }
            if self.state.settings.current.global_.integrations.codex_app.make_default and models:
                values["model"] = models[0]["id"]
            patch(config, values)
            if not (codex / "auth.json").exists():
                plans.append(
                    (
                        codex / "auth.json",
                        b'{"OPENAI_API_KEY":"splash-local-codex","auth_mode":"apikey"}',
                        None,
                    )
                )
        return plans

    async def connect(self, name: str, confirm: bool) -> DesktopIntegration:
        async with self.lock:
            if name in self.records and name not in self.recovery:
                return self.desktop(name)
            if not self.app(name):
                raise ApiError(404, "Install the desktop app first", "app_not_found")
            if self.running(name) and not confirm:
                raise ApiError(
                    409, "Confirm restarting the app to connect it", "restart_confirmation_required"
                )
            if name in self.recovery:
                await self.restore_one(name, reopen=False)
            await self.quit_app(name)
            plans = self.plans(name)
            backup = self.state.paths.integrations_backups / name / str(time.time_ns())
            backup.mkdir(parents=True, mode=0o700)
            records = {}
            for index, (path, data, keys) in enumerate(plans):
                record = snapshot(path, data, keys)
                if record["existed"]:
                    write_atomic(backup / str(index), base64.b64decode(record["before"]))
                records[str(path)] = record
            self.records[name] = {"connected_at": iso(), "files": records}
            self.persist()  # Must precede every third-party write.
            try:
                if name == "claude-desktop":
                    await self.start_gateway()
                for path, data, _ in plans:
                    write_atomic(path, data)
                app = self.app(name)
                assert app
                result = self.state.macos.open("-a", str(app))
                if result.returncode:
                    raise ApiError(
                        503, result.stderr or "Could not open the app", "app_open_failed"
                    )
            except Exception:
                await self.restore_one(name, reopen=False)
                raise
            self.recovery.discard(name)
            self.state.events.publish("integration.state", self.desktop(name))
            return self.desktop(name)

    async def restore_one(self, name: str, reopen: bool = True) -> DesktopIntegration:
        record = self.records.get(name)
        if not record:
            return self.desktop(name)
        running = await self.quit_app(name)
        for path, item in record["files"].items():
            restore(Path(path), item)
        self.records.pop(name)
        self.recovery.discard(name)
        self.persist()
        if name == "claude-desktop" and self.gateway:
            self.gateway.should_exit = True
            if self.gateway_task:
                await self.gateway_task
            self.gateway = None
        if reopen and running and (app := self.app(name)):
            self.state.macos.open("-a", str(app))
        self.state.events.publish("integration.state", self.desktop(name))
        return self.desktop(name)

    async def disconnect(self, name: str) -> DesktopIntegration:
        async with self.lock:
            return await self.restore_one(name)

    async def restore_all(self) -> RestoreAllResult:
        restored = []
        errors = []
        async with self.lock:
            for name in list(self.records):
                try:
                    await self.restore_one(name, reopen=False)
                    restored.append(name)
                except Exception as error:
                    errors.append(IntegrationError(name=name, message=str(error)))
        return RestoreAllResult(restored=restored, errors=errors)

    async def start_gateway(self) -> None:
        from .router import local_only

        app = FastAPI()

        @app.middleware("http")
        async def guard(request: Request, call_next: Any) -> Response:
            if not local_only(request):
                return Response(status_code=403)
            return cast(Response, await call_next(request))

        @app.get("/_splash/health")
        def health() -> Response:
            return Response(status_code=204)

        @app.get("/v1/models")
        def models() -> dict[str, Any]:
            slots = self.state.settings.current.global_.integrations.claude_desktop.slots
            entries = [
                {
                    "id": slot,
                    "type": "model",
                    "display_name": target or self.state.active_model() or "Splash",
                    "created_at": "2026-10-03T00:00:00Z",
                    "max_tokens": 262144,
                    "anthropic_family_tier": slot.split("-")[1],
                    "is_family_default": True,
                }
                for slot, target in slots.items()
            ]
            return {
                "data": entries,
                "first_id": entries[0]["id"],
                "last_id": entries[-1]["id"],
                "has_more": False,
            }

        async def messages(request: Request) -> Response:
            try:
                body = await request.json()
            except ValueError:
                return Response(status_code=400)
            if not isinstance(body, dict):
                return Response(status_code=400)
            slots = self.state.settings.current.global_.integrations.claude_desktop.slots
            slot = body.get("model")
            if not isinstance(slot, str) or slot not in slots:
                return Response(status_code=404)
            model = (
                slots.get(slot)
                or self.state.active_model()
                or self.state.settings.current.global_.routing.default_model
            )
            return cast(
                Response,
                await self.state.proxy.handle(
                    request,
                    request.url.path,
                    body_override=body,
                    rewrite_model=model,
                    checked=True,
                    client_label="claude-desktop",
                ),
            )

        app.add_api_route("/v1/messages", messages, methods=["POST"])
        app.add_api_route("/v1/messages/count_tokens", messages, methods=["POST"])
        sock = socket.socket()
        try:
            sock.bind(
                ("127.0.0.1", self.state.settings.current.global_.integrations.claude_desktop.port)
            )
            sock.listen(128)
        except BaseException:
            sock.close()
            raise
        config = uvicorn.Config(app, log_level="warning", lifespan="off")
        self.gateway = GatewayServer(config)
        self.gateway_task = asyncio.create_task(self.gateway.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not self.gateway.started:
                    if self.gateway_task.done():
                        await self.gateway_task
                        raise RuntimeError("Claude gateway exited before listening")
                    await asyncio.sleep(0.01)
        except BaseException:
            self.gateway.should_exit = True
            self.gateway_task.cancel()
            await asyncio.gather(self.gateway_task, return_exceptions=True)
            sock.close()
            raise

    def open_terminal(self, name: str, model: str | None) -> OpenTerminalResult:
        command = shlex.join(
            [str(self.state.paths.shim), "launch", name] + (["--model", model] if model else [])
        )
        result = self.state.macos.open_in_terminal(command)
        if result.returncode:
            raise ApiError(503, result.stderr or "Could not open Terminal", "terminal_failed")
        self.state.usage.record_launch(name, model)
        return OpenTerminalResult(ok=True, command=command)

    def remove_entries(self, name: str) -> EntriesRemoved:
        port = self.state.settings.current.global_.server.port
        entry = "splash" if port == 8000 else f"splash-{port}"
        backup = self.state.paths.integrations_backups / name / str(time.time_ns())
        backup.mkdir(parents=True, mode=0o700)
        removed = []
        if name == "hermes":
            path = self.home / ".hermes" / "profiles" / entry
            if path.is_dir() and not path.is_symlink():
                shutil.copytree(path, backup / entry)
                shutil.rmtree(path)
                removed.append(str(path))
        else:
            path = self.home / ".pi" / "agent" / "models.json"
            if path.exists():
                data = read_document(path)
                if entry in data.get("providers", {}):
                    write_atomic(backup / "models.json", path.read_bytes())
                    del data["providers"][entry]
                    write_atomic(path, encode_document(path, data))
                    removed.append(entry)
        return EntriesRemoved(removed=removed, backups=[str(p) for p in backup.iterdir()])

    async def shutdown(self) -> None:
        await self.restore_all()


def create(state: ManagerState) -> IntegrationsService:
    service = IntegrationsService(state)
    state.integrations = service
    return service
