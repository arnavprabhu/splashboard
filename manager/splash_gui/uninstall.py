"""Remove Splash GUI data (SPEC §19, D72; docs/plans/packaging.md PKG-12).

`POST /uninstall/plan` lists what would go, with sizes; `POST /uninstall` runs the steps in
SPEC §19 order: restore the integrations (all of them, or nothing else happens: D36), remove
the PATH block and the shim, then, when asked, delete the Keychain items and the data folder,
the models and the cache, and stop the manager last. The menu bar app's About window and
`splash doctor --uninstall` call these; the app unregisters its login item and LaunchAgent
itself (SMAppService), which the manager cannot do.

Only entries inside the data folder (`SPLASH_GUI_HOME`, `~/.splash`) are ever deleted. A models
or cache folder moved elsewhere (`storage.models_dir`, `storage.cache_dir`) is listed but never
deleted: it may be the user's own Hugging Face cache (SPEC §5, Q21). Symlinks are unlinked, never
followed. The Splash engine and its Homebrew formula are never touched (D72).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from .cli import install as shim
from .cli.api import home_of
from .errors import ApiError, error_responses
from .events.alerts import MENUBAR_CLIENT
from .mcp.secrets import _current_values, secret_name
from .models.layout import directory_size
from .schemas import UninstallItem, UninstallPlan, UninstallRequest, UninstallResult
from .secrets import SecretName
from .state import ManagerState, get_state

log = logging.getLogger(__name__)
router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]

STEPS = [
    "Restore Claude Desktop and the Codex app (stops here if one cannot be restored)",
    "Remove the PATH block from the shell startup files",
    "Remove the splash command (~/.splash/bin/splash)",
    "Delete the Keychain items (API key, Hugging Face token, MCP secrets)",
    "Delete the data folder: settings, chats, usage history, logs",
    "Delete models and the cache only if you tick them",
    "Stop the manager",
]


def _size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_dir():
        return directory_size(path)
    with contextlib.suppress(OSError):
        return path.stat().st_size
    return 0


def _same(a: Path, b: Path) -> bool:
    with contextlib.suppress(OSError):
        return a.resolve() == b.resolve()
    return False


def _guard_base(base: Path, home: Path) -> Path:
    """The data folder, refused when it is the user's home, a folder above it, or the root."""
    resolved = base.resolve()
    home_resolved = home.resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == home_resolved
        or home_resolved.is_relative_to(resolved)
    ):
        raise ApiError(
            409, f"Refusing to delete {resolved}: it is not a Splash GUI data folder", "unsafe_home"
        )
    return resolved


def _items(state: ManagerState) -> list[UninstallItem]:
    base = state.paths.base
    models = state.settings.models_dir()
    cache = state.settings.cache_dir()
    items: list[UninstallItem] = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            kind: Literal["data", "models", "cache"] = (
                "models" if _same(entry, models) else "cache" if _same(entry, cache) else "data"
            )
            items.append(UninstallItem(path=str(entry), kind=kind, bytes=_size(entry)))
    moved: tuple[tuple[Literal["models", "cache"], Path], ...] = (
        ("models", models),
        ("cache", cache),
    )
    for other, folder in moved:
        inside = any(_same(Path(item.path), folder) for item in items)
        if not inside and folder.exists():
            items.append(
                UninstallItem(path=str(folder), kind=other, bytes=_size(folder), deletable=False)
            )
    return items


def _secret_names(state: ManagerState) -> list[str]:
    names = [str(name) for name in SecretName]
    with contextlib.suppress(Exception):
        for server, kind, key in _current_values(state.settings.current):
            names.append(secret_name(server, kind, key))
    return names


def build_plan(state: ManagerState) -> UninstallPlan:
    home = home_of(state)
    items = _items(state)
    integrations = state.integrations
    connected = sorted(integrations.records) if integrations is not None else []
    return UninstallPlan(
        home=str(state.paths.base),
        items=items,
        models_bytes=sum(i.bytes for i in items if i.kind == "models"),
        cache_bytes=sum(i.bytes for i in items if i.kind == "cache"),
        data_bytes=sum(i.bytes for i in items if i.kind == "data"),
        connected_integrations=connected,
        path_block_files=[str(e["file"]) for e in shim.rc_report(home) if e["managed"]],
        shim_installed=state.paths.shim.exists(),
        app_connected=state.events.subscriber_count(MENUBAR_CLIENT) > 0,
        steps=STEPS,
    )


def _delete(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


@router.post("/uninstall/plan", response_model=UninstallPlan)
def uninstall_plan(state: State) -> UninstallPlan:
    """What `POST /uninstall` would remove, with each folder's size (SPEC §19)."""
    return build_plan(state)


@router.post("/uninstall", response_model=UninstallResult, responses=error_responses(409))
async def uninstall(state: State, body: UninstallRequest | None = None) -> UninstallResult:
    """Run the SPEC §19 steps. A failed integration restore stops before anything is removed
    (`409 restore_incomplete`, D36); every later step is skipped, so the user can retry."""
    request = body or UninstallRequest()
    home = home_of(state)
    deleting = request.delete_data or request.delete_models or request.delete_cache
    base = _guard_base(state.paths.base, home) if deleting else state.paths.base

    restored: list[str] = []
    if state.integrations is not None:
        outcome = await state.integrations.restore_all()
        if outcome.errors:
            names = ", ".join(e.name for e in outcome.errors)
            raise ApiError(
                409,
                f"Could not restore {names}; nothing was removed. Fix it and try again.",
                "restore_incomplete",
                details={"errors": [e.model_dump() for e in outcome.errors]},
            )
        restored = outcome.restored

    removed_blocks = []
    for entry in shim.rc_report(home):
        if entry["managed"] and shim.remove_from_rc(Path(str(entry["file"]))):
            removed_blocks.append(str(entry["file"]))
    shim_removed = shim.remove_shim(state.paths)

    secrets_deleted = 0
    deleted: list[str] = []
    kept: list[str] = []
    freed = 0
    # The engine holds the cache and model files open: stop it before anything goes.
    if deleting and state.supervisor is not None and state.supervisor.state != "stopped":
        with contextlib.suppress(Exception):
            await state.supervisor.stop("uninstall")
    if request.delete_data:
        for name in _secret_names(state):
            try:
                state.secrets.delete(name)
                secrets_deleted += 1
            except Exception:
                log.debug("no secret %s to delete", name)
    if request.delete_data:
        state.data_removed = True
    for item in _items(state):
        path = Path(item.path)
        wanted = {
            "data": request.delete_data,
            "models": request.delete_models,
            "cache": request.delete_cache,
        }[item.kind]
        inside = path.parent.resolve() == base.resolve()
        if not (wanted and item.deletable and inside):
            kept.append(item.path)
            continue
        try:
            _delete(path)
        except OSError as error:
            log.warning("could not delete %s: %s", path, error)
            kept.append(item.path)
            continue
        deleted.append(item.path)
        freed += item.bytes

    stopping = bool(request.stop and state.request_shutdown is not None)
    log.info(
        "uninstall: restored %s, deleted %d entries; stopping=%s", restored, len(deleted), stopping
    )
    if stopping and state.request_shutdown is not None:
        state.request_shutdown()
    return UninstallResult(
        restored=restored,
        path_block_removed=removed_blocks,
        shim_removed=shim_removed,
        secrets_deleted=secrets_deleted,
        deleted=deleted,
        kept=kept,
        freed_bytes=freed,
        stopping=stopping,
    )
