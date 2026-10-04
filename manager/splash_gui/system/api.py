"""System routes (SPEC §14 System, plus wizard helpers)."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from .. import __version__
from ..errors import ApiError, error_responses
from ..schemas import (
    BrewInfo,
    DoctorCheck,
    DoctorReport,
    EngineDiscoveryInfo,
    JobAccepted,
    OkResponse,
    RevealRequest,
    SystemInfo,
    UpdateInfo,
    Versions,
)
from ..state import ManagerState, get_state
from .info import system_info

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/system", response_model=SystemInfo)
def get_system(state: State) -> SystemInfo:
    return system_info(state.settings.models_dir(), state.settings.cache_dir())


@router.get("/versions", response_model=Versions)
def get_versions(state: State) -> Versions:
    engine = state.engine()
    return Versions(
        gui=__version__,
        manager=__version__,
        python=platform.python_version(),
        engine=EngineDiscoveryInfo.model_validate(engine.as_dict()),
        status_schema_version=state.supervisor.schema_version if state.supervisor else None,
        engine_update=state.updates.engine_update if state.updates is not None else UpdateInfo(),
    )


@router.get("/system/brew", response_model=BrewInfo)
def get_brew(state: State) -> BrewInfo:
    brew = shutil.which("brew") or next(
        (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if shutil.which(b)), None
    )
    if brew is None:
        return BrewInfo(installed=False)
    version = None
    try:
        out = subprocess.run(
            [brew, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        version = out.split()[1] if out.startswith("Homebrew") else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    engine = state.engine()
    return BrewInfo(
        installed=True,
        path=brew,
        version=version,
        splash_formula_installed=engine.found and engine.source == "brew",
    )


@router.post(
    "/engine/upgrade", response_model=JobAccepted, status_code=202, responses=error_responses(409)
)
async def engine_upgrade(state: State) -> JobAccepted:
    """SPEC §6.7: stop → brew update && brew upgrade → rediscover → restart the model."""
    return state.updates.upgrade()  # type: ignore[no-any-return]


@router.post(
    "/engine/install", response_model=JobAccepted, status_code=202, responses=error_responses(409)
)
async def engine_install(state: State) -> JobAccepted:
    """Wizard step 1: `brew install incoai/tap/splash` with output on /events (job events)."""
    return state.updates.install()  # type: ignore[no-any-return]


@router.post("/engine/check-update", response_model=UpdateInfo)
async def engine_check_update(state: State) -> UpdateInfo:
    """Check for a newer engine now (brew outdated, then GitHub releases)."""
    return await state.updates.check()  # type: ignore[no-any-return]


@router.get("/doctor", response_model=DoctorReport, responses=error_responses(400, 404, 503))
def doctor(state: State) -> DoctorReport:
    system = get_system(state)
    engine = state.engine()
    checks = [
        DoctorCheck(
            id="hardware",
            label="Hardware and macOS",
            status="ok" if system.supported else "fail",
            message=system.chip or system.arch,
            fix="; ".join(system.unsupported_reasons) or None,
        ),
        DoctorCheck(
            id="engine",
            label="Splash engine",
            status="ok" if engine.found else "fail",
            message=engine.version or "Splash is not installed",
            fix=None if engine.found else "brew install incoai/tap/splash",
        ),
        DoctorCheck(
            id="brew",
            label="Homebrew",
            status="ok" if shutil.which("brew") else "warn",
            message=shutil.which("brew") or "Install Homebrew from brew.sh",
        ),
        DoctorCheck(
            id="disk",
            label="Free disk space",
            status="ok" if system.disk.models.free_bytes > 2 * 1024**3 else "warn",
            message=f"{system.disk.models.free_bytes} bytes free",
        ),
        DoctorCheck(
            id="permissions",
            label="Private data directory",
            status="ok" if state.paths.base.stat().st_mode & 0o077 == 0 else "warn",
            message=str(state.paths.base),
            fix="chmod 700 " + str(state.paths.base),
        ),
    ]
    hidden = []
    for name in (".zshrc", ".zprofile", ".bashrc", ".bash_profile"):
        path = Path.home() / name
        if path.is_file() and re.search(
            r"(?m)^\s*(?:function\s+splash\b|splash\s*\(\s*\)|alias\s+splash=)",
            path.read_text(errors="replace"),
        ):
            hidden.append(str(path))
    checks.append(
        DoctorCheck(
            id="path",
            label="CLI PATH",
            status="warn" if hidden else "ok",
            message="A splash function or alias hides the shim: " + ", ".join(hidden)
            if hidden
            else "No splash shell override found",
            fix="Remove the splash function or alias" if hidden else None,
        )
    )
    return DoctorReport(ok=all(c.status != "fail" for c in checks), checks=checks)


@router.post("/system/reveal", response_model=OkResponse, responses=error_responses(400, 404, 503))
def reveal(state: State, body: RevealRequest) -> OkResponse:
    from ..paths import splash_data_dir

    targets = {
        "models_dir": state.settings.models_dir(),
        "cache_dir": state.settings.cache_dir(),
        "logs_dir": state.paths.logs_dir,
        "splash_data_dir": splash_data_dir(),
    }
    if body.target == "model":
        detail = state.models.detail(body.id or "")
        path = Path(detail.link_path).resolve()
    elif body.target == "trace":
        from ..logs.api import _is_trace_name

        if not body.id or not _is_trace_name(body.id):
            raise ApiError(400, "Invalid trace name", "invalid_trace")
        path = state.crash_trace_dir / body.id
    else:
        path = targets[body.target]
    if not path.exists():
        raise ApiError(404, "The requested file does not exist", "not_found")
    result = state.macos.reveal(path)
    if result.returncode:
        raise ApiError(503, result.stderr or "Finder could not reveal this file", "reveal_failed")
    return OkResponse()
