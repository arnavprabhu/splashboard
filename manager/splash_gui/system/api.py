"""System routes (SPEC §14 System, plus wizard helpers)."""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Annotated

from fastapi import APIRouter, Depends

from .. import __version__
from ..errors import STUB_RESPONSES, not_implemented
from ..schemas import (
    BrewInfo,
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
        status_schema_version=None,
        engine_update=UpdateInfo(),
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
    "/engine/upgrade", response_model=JobAccepted, status_code=202, responses=STUB_RESPONSES
)
def engine_upgrade() -> JobAccepted:
    not_implemented("Engine upgrade")


@router.post(
    "/engine/install", response_model=JobAccepted, status_code=202, responses=STUB_RESPONSES
)
def engine_install() -> JobAccepted:
    """Wizard step 1: `brew install incoai/tap/splash` with output on /events (job events)."""
    not_implemented("Engine install")


@router.get("/doctor", response_model=DoctorReport, responses=STUB_RESPONSES)
def doctor() -> DoctorReport:
    not_implemented("Doctor")


@router.post("/system/reveal", response_model=OkResponse, responses=STUB_RESPONSES)
def reveal(body: RevealRequest) -> OkResponse:
    not_implemented("Reveal in Finder")
