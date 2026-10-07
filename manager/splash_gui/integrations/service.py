"""Session-only client launch controls and reversible desktop connections (§11)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
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
    LaunchPrint,
    OpenedPath,
    OpenTerminalResult,
    PrintedFile,
    RestoreAllResult,
)
from ..settings import parsers
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
CODEX_SENTINEL = b'{"OPENAI_API_KEY":"splash-local-codex","auth_mode":"apikey"}'
CODEX_BUNDLE_ID = "com.openai.codex"
# Desktop app versions this integration was verified against (SPEC §11.3.2
# "Versions"). None: not yet verified on a real app, so every version shows the
# "untested version" note (docs/progress/session3-backend.md).
TESTED_VERSIONS: dict[str, str | None] = {"claude-desktop": None, "codex-app": None}
# Splash's reasoning efforts (server/serve_options.py REASONING_EFFORTS).
SPLASH_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _version_key(text: str | None) -> tuple[int, ...]:
    parts = []
    for piece in (text or "").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def untested(name: str, version: str | None) -> bool:
    tested = TESTED_VERSIONS.get(name)
    return tested is None or version is None or _version_key(version) < _version_key(tested)


def through_link(path: Path) -> Path:
    """The file to edit for `path`: a symlinked config (dotfile managers) is edited
    at its target so the link survives; a broken link is refused rather than
    creating the target's folders (review R34)."""
    if not path.is_symlink():
        return path
    if not path.exists():
        raise ApiError(409, f"{path} is a broken symlink; fix or remove it first", "broken_symlink")
    return path.resolve()


def bundled_codex(app: Path) -> Path | None:
    """The Codex CLI inside the Codex/ChatGPT app: `Contents/Resources/codex`
    (SPEC §11.3.2), or, in ChatGPT builds that ship `codex-cli/` (checked on
    ChatGPT 26.930 on 2026-10-04), the entrypoint its `codex-package.json` names."""
    resources = app / "Contents" / "Resources"
    direct = resources / "codex"
    if direct.is_file() and os.access(direct, os.X_OK):
        return direct
    package = resources / "codex-cli" / "codex-package.json"
    try:
        entry = json.loads(package.read_text()).get("entrypoint")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(entry, str) and ".." not in Path(entry).parts:
        candidate = package.parent / entry
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class GatewayServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The manager alone owns process signal handlers.
        yield


class IntegrationsService:
    def __init__(self, state: ManagerState, *, home: Path | None = None) -> None:
        """`home` is the user's home directory whose app configs are edited
        (default: the real one); tests pass a throwaway directory."""
        self.state = state
        self.home = home if home is not None else Path.home()
        self._mdfind: tuple[float, Path | None] | None = None
        self.corrupt_state: str | None = None
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
        except ValueError:
            # A torn state.json: keep it for inspection and start clean. Connect
            # then refuses an app whose config is still ours (refuse_if_already_applied).
            corrupt = self.state.paths.integrations_state.with_suffix(".corrupt")
            self.state.paths.integrations_state.rename(corrupt)
            self.corrupt_state = str(corrupt)
        if self.recovery:
            self._unclean_alert()

    def _unclean_alert(self) -> None:
        """SPEC §11.4: a connection survived a crash or power loss; offer
        Reconnect or Restore now."""
        from ..events.alerts import action

        names = sorted(self.recovery)
        labels = ", ".join(
            "Claude Desktop" if n == "claude-desktop" else "Codex app" for n in names
        )
        self.state.alerts.raise_alert(
            "unclean_integration_shutdown",
            f"{labels} still connected after an unclean shutdown",
            "Splash GUI stopped without restoring the app's configuration. Reconnect, or "
            "restore the original configuration now.",
            source="integrations",
            actions=[
                *(
                    action(
                        f"reconnect-{n}",
                        "Reconnect",
                        f"/api/admin/integrations/{n}/connect",
                        body={"confirm_restart": True},
                    )
                    for n in names
                ),
                action("restore", "Restore now", "/api/admin/integrations/restore-all"),
            ],
            dismissible=False,
        )

    def persist(self) -> None:
        write_atomic(
            self.state.paths.integrations_state, json.dumps(self.records, indent=2).encode()
        )

    def app(self, name: str) -> Path | None:
        names = ["Claude.app"] if name == "claude-desktop" else ["Codex.app", "ChatGPT.app"]
        found = next(
            (
                root / filename
                for root in (self.home / "Applications", Path("/Applications"))
                for filename in names
                if (root / filename).is_dir()
            ),
            None,
        )
        if found is None and name == "codex-app":
            found = self._spotlight_codex()
        return found

    def _spotlight_codex(self) -> Path | None:
        """`mdfind` for the Codex bundle id anywhere (SPEC §11.3.2), cached a minute."""
        now = time.monotonic()
        if self._mdfind is not None and now - self._mdfind[0] < 60:
            return self._mdfind[1]
        result = self.state.macos.run(
            ["/usr/bin/mdfind", f"kMDItemCFBundleIdentifier == '{CODEX_BUNDLE_ID}'"], timeout=5
        )
        hit = next(
            (
                Path(line)
                for line in result.stdout.splitlines()
                if line.endswith(".app") and Path(line).is_dir()
            ),
            None,
        )
        self._mdfind = (now, hit)
        return hit

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
        version = self.state.macos.app_version(app) if app else None
        record = self.records.get(name)
        return DesktopIntegration.model_validate(
            {
                "name": name,
                "label": "Claude Desktop" if name == "claude-desktop" else "Codex app",
                "detected": app is not None,
                "app_path": str(app) if app else None,
                "version": version,
                "untested_version": untested(name, version) if app else False,
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

    def progress(self, name: str, state: str, step: str, message: str | None = None) -> None:
        """One `integration.state` event: the row as it is now, plus the step the
        connect/restore sequence just entered (docs/ui/09 §4, API gap G14)."""
        row = self.desktop(name).model_copy(
            update={"state": state, "step": step, "message": message}
        )
        self.state.events.publish("integration.state", row)

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
                        "changes": self.changes(name),
                        "entries": self.entries(name),
                        "last_launched_at": launched.get(name),
                    }
                )
            )
        return Integrations(
            cli=cli,
            desktop=[self.desktop(n) for n in ("claude-desktop", "codex-app")],
            unclean_shutdown=bool(self.recovery),
            corrupt_state=self.corrupt_state,
        )

    def changes(self, name: str) -> IntegrationChanges:
        """What `splash launch <name>` changes, as Splash's `install/clients.py`
        (1.3.0) configures each client (SPEC §3.5, §10.7 "What this changes").
        `splash launch <name> --print` shows the exact values for a session."""
        g = self.state.settings.current.global_
        port = self.public_port()
        url = f"http://127.0.0.1:{port}"
        model = "<model>"
        entry = "splash" if port == 8000 else f"splash-{port}"
        key = "$SPLASH_API_KEY" if g.security.api_key_required else "local"
        session = "This session only: plain `{0}` keeps its normal configuration."
        if name == "claude":
            return IntegrationChanges(
                env={
                    "ANTHROPIC_BASE_URL": url,
                    "ANTHROPIC_AUTH_TOKEN": key,
                    "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                    "ANTHROPIC_SMALL_FAST_MODEL": model,
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "<context>",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "<context>",
                    "CLAUDE_CODE_USE_BEDROCK": "0",
                    "CLAUDE_CODE_USE_VERTEX": "0",
                    "CLAUDE_CODE_USE_FOUNDRY": "0",
                },
                args=[
                    "--disallowedTools",
                    "WebSearch",
                    "--model",
                    model,
                    "--permission-mode",
                    "default",
                ],
                notes=[session.format("claude"), "ANTHROPIC_API_KEY is removed for the session."],
            )
        if name == "codex":
            return IntegrationChanges(
                env={"SPLASH_API_KEY": key},
                args=[
                    "-c",
                    f'model="{model}"',
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    'model_provider="splash"',
                    "-c",
                    f'model_providers.splash={{name="Splash",base_url="{url}/v1",'
                    'env_key="SPLASH_API_KEY",wire_api="responses"}',
                    "-c",
                    "model_context_window=<context>",
                    "-c",
                    "model_auto_compact_token_limit=<90% of context>",
                    "-c",
                    "features.apps=false",
                ],
                notes=[
                    session.format("codex"),
                    "Only `-c` overrides; no file is written.",
                    "ChatGPT Apps connectors are off for the session: Splash rejects "
                    "tool names over 64 characters.",
                ],
            )
        if name == "opencode":
            return IntegrationChanges(
                env={
                    "OPENCODE_CONFIG_CONTENT": (
                        f'{{"model": "splash/{model}", "provider": {{"splash": …}}}}'
                    )
                },
                args=["--standalone (OpenCode 2 only)"],
                notes=[
                    session.format("opencode"),
                    "The configuration is inline in the environment.",
                ],
            )
        if name == "hermes":
            home = self.hermes_profiles() / entry
            # install/clients.py:415-417 (1.3.0): a reference, never the key.
            profile_key = "${SPLASH_API_KEY}" if key != "local" else "local"
            return IntegrationChanges(
                env={"HERMES_HOME": str(home), "OPENAI_BASE_URL": f"{url}/v1"},
                args=["--provider", "custom", "--model", model],
                files=[str(home / "config.yaml")],
                notes=[
                    f"Writes only its own Hermes profile `{entry}`; "
                    "your default profile is unchanged.",
                    f"The profile's api_key is `{profile_key}`; the key itself is never saved.",
                    # D54: Splash 1.2.x wrote the key itself; 1.3.0 rewrites
                    # model.api_key on every launch.
                    "A profile written by Splash 1.2.x keeps the old key until the next "
                    "`splash launch hermes` rewrites it.",
                ],
            )
        models = self.pi_models()
        return IntegrationChanges(
            args=["--provider", entry, "--model", model],
            files=[str(models)],
            notes=[
                f"Adds one provider `{entry}` to Pi's models.json; "
                "Pi's default provider is unchanged."
            ],
        )

    def public_port(self) -> int:
        """The port the manager actually listens on (`--port` may differ from
        `server.port`); clients and Splash's launchers must use it."""
        bound = self.state.bound
        return bound[1] if bound else self.state.settings.current.global_.server.port

    def env(self) -> dict[str, str]:
        """The environment Splash's configurators would see (tests replace it)."""
        return dict(os.environ)

    def hermes_profiles(self) -> Path:
        """`<root>/profiles` by Splash's rule (install/clients.py
        `hermes_profile_home`): ~/.hermes, unless HERMES_HOME names a home outside
        it, whose root is its parent's parent for `<root>/profiles/<name>`."""
        root = self.home / ".hermes"
        value = self.env().get("HERMES_HOME", "").strip()
        if value:
            home = Path(os.path.expandvars(value)).expanduser()
            if not home.resolve().is_relative_to(root.resolve()):
                root = home.parent.parent if home.parent.name == "profiles" else home
        return root / "profiles"

    def pi_models(self) -> Path:
        """Pi's models.json (install/clients.py `_pi_models_path`):
        `$PI_CODING_AGENT_DIR/models.json`, else ~/.pi/agent/models.json."""
        agent = self.env().get("PI_CODING_AGENT_DIR")
        directory = Path(agent).expanduser() if agent else self.home / ".pi" / "agent"
        return directory / "models.json"

    def entries(self, name: str) -> list[str]:
        """The Hermes `splash*` profiles or Pi `splash*` providers Splash's
        launchers created (SPEC §11.2), for the Remove button."""
        pattern = re.compile(r"splash(-\d+)?")
        if name == "hermes":
            profiles = self.hermes_profiles()
            if not profiles.is_dir():
                return []
            return sorted(
                p.name for p in profiles.iterdir() if p.is_dir() and pattern.fullmatch(p.name)
            )
        if name == "pi":
            try:
                providers = read_document(self.pi_models().resolve()).get("providers", {})
            except (OSError, ValueError):
                return []
            return sorted(k for k in providers if isinstance(k, str) and pattern.fullmatch(k))
        return []

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
            path = through_link(path)
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
                            {"effort": e, "description": e} for e in SPLASH_EFFORTS
                        ],
                        "supported_in_api": True,
                        "shell_type": "unified_exec",
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "effective_context_window_percent": 95,
                    }
                )
            for entry in self.native_codex_models(codex):
                if isinstance(entry, dict) and entry.get("slug") not in {
                    m["slug"] for m in catalog
                }:
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
                "openai_base_url": f"http://127.0.0.1:{self.public_port()}/api/codex/v1",
                "model_catalog_json": str(folder / "models.json"),
                "desktop": desktop,
            }
            if self.state.settings.current.global_.integrations.codex_app.make_default and models:
                values["model"] = models[0]["id"]
            patch(config, values)
            auth = codex / "auth.json"
            if auth.is_symlink() and not auth.exists():
                raise ApiError(
                    409, f"{auth} is a broken symlink; fix or remove it first", "broken_symlink"
                )
            if not auth.exists():
                plans.append((auth, CODEX_SENTINEL, None))
        return plans

    def native_codex_models(self, codex_home: Path) -> list[Any]:
        """The app's own models, so they stay in the picker next to Splash's
        (SPEC §11.3.2): `<bundled codex> debug models` with a scratch CODEX_HOME,
        falling back to `~/.codex/models_cache.json`. Never raises."""
        app = self.app("codex-app")
        binary = bundled_codex(app) if app else None
        if binary is not None:
            with tempfile.TemporaryDirectory(prefix="splash-codex-") as scratch:
                try:
                    result = subprocess.run(
                        [str(binary), "debug", "models"],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        env={**os.environ, "CODEX_HOME": scratch},
                        stdin=subprocess.DEVNULL,
                        check=False,
                    )
                    models = json.loads(result.stdout).get("models")
                    if result.returncode == 0 and isinstance(models, list):
                        return list(models)
                except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
                    pass
        try:
            cached = json.loads((codex_home / "models_cache.json").read_text())
            models = cached.get("models") if isinstance(cached, dict) else None
            return list(models) if isinstance(models, list) else []
        except (OSError, ValueError):
            return []

    def refuse_if_already_applied(self, name: str) -> None:
        """No record, yet the app's config already points at Splash (a lost or
        corrupt state.json, review R34): snapshotting now would save our own
        settings as the "original", so refuse until the user restores it."""
        if name == "claude-desktop":
            support = self.home / "Library" / "Application Support"
            ours = support / "Claude-3p" / "configLibrary" / (DEPLOYMENT + ".json")
            applied = ours.exists() or ours.is_symlink()
        else:
            try:
                config = read_document(self.home / ".codex" / "config.toml")
            except (OSError, ValueError):
                config = {}
            applied = "/api/codex/v1" in str(config.get("openai_base_url", ""))
        if applied:
            backups = self.state.paths.integrations_backups / name
            raise ApiError(
                409,
                "This app's configuration already points at Splash GUI but there is no "
                "restore record (state.json was lost or unreadable). Restore it from "
                f"{backups} or remove the Splash keys, then connect.",
                "foreign_connection_state",
                details={"backups": str(backups)},
            )

    def owned(self, name: str) -> set[str]:
        """Files Splash creates outright: removed on restore whatever they hold."""
        if name == "claude-desktop":
            support = self.home / "Library" / "Application Support"
            return {str(support / "Claude-3p" / "configLibrary" / (DEPLOYMENT + ".json"))}
        folder = self.state.paths.codex_app_dir
        return {str(folder / "models.json"), str(folder / "routing.json")}

    def open_app(self, name: str, app: Path) -> Any:
        if name == "codex-app":
            # SPEC §11.3.2 restart: open straight into a new Codex thread.
            return self.state.macos.open("-a", str(app), "codex://threads/new?mode=codex")
        return self.state.macos.open("-a", str(app))

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
            elif name not in self.records:
                self.refuse_if_already_applied(name)
            label = "Claude" if name == "claude-desktop" else "Codex"
            self.progress(name, "connecting", "quitting_app", f"Quitting {label}…")
            await self.quit_app(name)
            self.progress(name, "connecting", "backing_up", "Backing up the current configuration…")
            plans = self.plans(name)
            backup = self.state.paths.integrations_backups / name / str(time.time_ns())
            backup.mkdir(parents=True, mode=0o700)
            records = {}
            owned = self.owned(name)
            for index, (path, data, keys) in enumerate(plans):
                record = snapshot(path, data, keys)
                if record["existed"]:
                    write_atomic(backup / str(index), base64.b64decode(record["before"]))
                    record["backup"] = str(backup / str(index))
                if str(path) in owned and not record["existed"]:
                    record["owned"] = True
                if path.name == "_meta.json":
                    record["entry_ids"] = [DEPLOYMENT]
                if path.name == "auth.json" and not record["existed"]:
                    record["sentinel"] = True
                if path.name == "claude_desktop_config.json":
                    record["absent_defaults"] = {"deploymentMode": "1p"}
                records[str(path)] = record
            self.records[name] = {"connected_at": iso(), "files": records}
            self.persist()  # Must precede every third-party write.
            try:
                if name == "claude-desktop":
                    self.progress(name, "connecting", "starting_gateway", "Starting the gateway…")
                    await self.start_gateway()
                self.progress(name, "connecting", "writing_config", "Writing the configuration…")
                for path, data, _ in plans:
                    # Through a symlink, with the file's own mode (snapshots.snapshot).
                    record = records[str(path)]
                    write_atomic(Path(record.get("target") or path), data, int(record["mode"]))
                app = self.app(name)
                assert app
                self.progress(name, "connecting", "opening_app", f"Opening {label}…")
                result = self.open_app(name, app)
                if result.returncode:
                    raise ApiError(
                        503, result.stderr or "Could not open the app", "app_open_failed"
                    )
            except Exception as error:
                await self.restore_one(name, reopen=False)
                message = error.message if isinstance(error, ApiError) else str(error)
                self.progress(name, "not_connected", "failed", message)
                raise
            self.recovery.discard(name)
            if not self.recovery:
                self.state.alerts.clear_condition("unclean_integration_shutdown")
            self.progress(name, "connected", "done")
            return self.desktop(name)

    async def restore_one(self, name: str, reopen: bool = True) -> DesktopIntegration:
        record = self.records.get(name)
        if not record:
            return self.desktop(name)
        self.progress(name, "restoring", "quitting_app")
        running = await self.quit_app(name)
        self.progress(name, "restoring", "restoring_files", "Restoring the previous configuration…")
        failures = []
        for path, item in record["files"].items():
            try:
                restore(Path(path), item)
            except (OSError, ValueError) as error:
                failures.append(f"{path}: {error}")
        if failures:
            # Keep the record so Restore can be retried; nothing is forgotten.
            self.persist()
            self.progress(name, "needs_restore", "failed", "; ".join(failures))
            raise ApiError(
                409,
                "Some files could not be restored: " + "; ".join(failures),
                "restore_incomplete",
            )
        self.records.pop(name)
        self.recovery.discard(name)
        if not self.recovery:
            self.state.alerts.clear_condition("unclean_integration_shutdown")
        self.persist()
        if name == "claude-desktop" and self.gateway:
            self.gateway.should_exit = True
            if self.gateway_task:
                await self.gateway_task
            self.gateway = None
        if reopen and running and (app := self.app(name)):
            self.progress(name, "restoring", "opening_app")
            self.open_app(name, app)
        self.progress(name, "not_connected", "done")
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
                    # Codex app: restart it if it was running (SPEC §11.3.2); Claude
                    # Desktop reopens only on an explicit Disconnect (§11.3.1).
                    await self.restore_one(name, reopen=name == "codex-app")
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
            if not model:
                from ..proxy.pipeline import ProxyError

                return ProxyError(
                    503,
                    "Splash GUI: no model loaded",
                    "engine_unavailable",
                    headers={"Retry-After": "5"},
                ).response(anthropic=True)
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

    def open(self, name: str) -> OpenedPath:
        """ "Open app" (docs/ui/09 §5, G17): `open -a Claude`, or the Codex app on a
        new thread."""
        app = self.app(name)
        if app is None:
            raise ApiError(404, "Install the desktop app first", "app_not_found")
        result = self.open_app(name, app)
        if result.returncode:
            raise ApiError(503, result.stderr or "Could not open the app", "app_open_failed")
        return OpenedPath(path=str(app))

    def reveal_backup(self, name: str) -> OpenedPath:
        """ "View backup" (G16): the newest backup folder of this integration in Finder."""
        root = self.state.paths.integrations_backups / name
        folders = (
            sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
            if root.is_dir()
            else []
        )
        if not folders:
            raise ApiError(404, f"No backup of {name} yet", "no_backup")
        result = self.state.macos.reveal(folders[-1])
        if result.returncode:
            raise ApiError(503, result.stderr or "Finder could not reveal it", "reveal_failed")
        return OpenedPath(path=str(folders[-1]))

    def print_launch(self, client: str, model: str | None) -> LaunchPrint:
        """`splash launch <client> --print` as data (G13). Claude, Codex and OpenCode
        run Splash's own `install/clients.py` (the helper's JSON mode), so this and
        the CLI share one source; Hermes and Pi would write their profile/provider
        when configured, so they get the static description of what that writes."""
        g = self.state.settings.current.global_
        chosen = model or self.state.active_model() or g.routing.default_model
        listing = self.state.proxy.models_list()["data"]
        if chosen is None and listing:
            chosen = str(listing[0]["id"])
        static = self.changes(client)
        if chosen is not None:
            static = IntegrationChanges.model_validate_json(
                static.model_dump_json().replace("<model>", chosen)
            )
        entry: dict[str, Any] = next((m for m in listing if m.get("id") == chosen), {})
        context = entry.get("context_length")
        if not isinstance(context, int) or context <= 0:
            context = 262144
        engine = self.state.engine_cached()
        exact_ok = (
            client in ("claude", "codex", "opencode")
            and chosen is not None
            and engine.python is not None
            and engine.pkg is not None
            and (engine.pkg / "install" / "clients.py").is_file()
        )
        fallback = LaunchPrint(
            client=client,
            model=chosen,
            exact=False,
            env=static.env,
            args=static.args,
            command=shlex.join([client, *static.args]),
            files=[PrintedFile(path=f, change=self._file_change(client)) for f in static.files],
            notes=static.notes,
        )
        if not exact_ok:
            return fallback
        placeholder = "${SPLASH_API_KEY}"
        spec = {
            "client": client,
            "model": chosen,
            "url": f"http://127.0.0.1:{self.public_port()}",
            "context": context,
            "modalities": entry.get("input_modalities") or ["text"],
            "args": [],
            "print": True,
            "format": "json",
        }
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "SPLASH_GUI_ENGINE_PKG": str(engine.pkg),
            "SPLASH_GUI_CLIENT_SPEC": json.dumps(spec),
            # Present so the output shows that Splash removes it for the session.
            "ANTHROPIC_API_KEY": "(yours)",
        }
        if g.security.api_key_required:
            env["SPLASH_API_KEY"] = placeholder
        helper = Path(__file__).parents[1] / "helpers" / "launch_client.py"
        try:
            result = subprocess.run(
                [str(engine.python), str(helper)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            data = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            data = None
        if not isinstance(data, dict):
            return fallback
        argv = [str(a) for a in data.get("argv", [])]
        changed = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
        changed.pop("SPLASH_GUI_CLIENT_SPEC", None)
        secret = sorted(k for k, v in changed.items() if placeholder in v)
        shown = {k: v.replace(placeholder, "••••") for k, v in changed.items()}
        removed = [k for k in data.get("removed") or [] if k != "SPLASH_GUI_CLIENT_SPEC"]
        return LaunchPrint(
            client=client,
            model=chosen,
            exact=True,
            env=shown,
            secret_env=secret,
            removed_env=removed,
            args=argv[1:],
            command=shlex.join([client, *argv[1:]]),
            files=[],
            notes=static.notes,
        )

    @staticmethod
    def _file_change(client: str) -> str:
        if client == "hermes":
            return "model: default, provider custom, base_url, api_key, context_length, max_tokens"
        if client == "pi":
            return "adds or replaces this server's provider entry"
        return ""

    def open_terminal(self, name: str, model: str | None) -> OpenTerminalResult:
        if model is not None:
            # A model ID or `<id>:<profile>` (D22, §7.5). The command is typed into a
            # terminal, so refuse anything else rather than rely on quoting alone.
            base, _, profile = model.rpartition(":")
            try:
                parsers.parse_model_id(model)
            except ValueError:
                try:
                    parsers.parse_model_id(base)
                except ValueError:
                    base = ""
                if not base or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", profile):
                    message = f"{model!r} is not a model ID"
                    raise ApiError(400, message, "invalid_model_id") from None
        command = shlex.join(
            [str(self.state.paths.shim), "launch", name] + (["--model", model] if model else [])
        )
        result = self.state.macos.open_in_terminal(command)
        if result.returncode:
            raise ApiError(503, result.stderr or "Could not open Terminal", "terminal_failed")
        # `splash launch` itself records the session (SPEC §11.2 step 4).
        return OpenTerminalResult(ok=True, command=command)

    def remove_entries(self, name: str) -> EntriesRemoved:
        """Delete the Hermes `splash*` profiles or the Pi `splash*` providers, after a
        backup in `integrations/backups/<name>/` (SPEC §11.2)."""
        wanted = self.entries(name)
        backup = self.state.paths.integrations_backups / name / str(time.time_ns())
        backup.mkdir(parents=True, mode=0o700)
        removed = []
        if name == "hermes":
            for entry in wanted:
                path = self.hermes_profiles() / entry
                if path.is_dir() and not path.is_symlink():
                    shutil.copytree(path, backup / entry, symlinks=True)
                    shutil.rmtree(path)
                    removed.append(entry)
        elif wanted:
            path = self.pi_models()
            # Write through a symlinked models.json, as Splash's launcher does.
            target = path.resolve()
            write_atomic(backup / "models.json", target.read_bytes())
            data = read_document(target)
            for entry in wanted:
                data["providers"].pop(entry, None)
                removed.append(entry)
            write_atomic(target, encode_document(target, data))
        return EntriesRemoved(removed=removed, backups=[str(p) for p in backup.iterdir()])

    async def shutdown(self) -> None:
        await self.restore_all()


def create(state: ManagerState) -> IntegrationsService:
    service = IntegrationsService(state, home=state.user_home)
    state.integrations = service
    return service
