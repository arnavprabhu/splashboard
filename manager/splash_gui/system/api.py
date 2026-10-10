"""System routes."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from .. import __version__
from ..engine.discovery import SUPPORTED_RANGE
from ..errors import ApiError, error_responses
from ..schemas import (
    BrewInfo,
    DoctorCheck,
    DoctorReport,
    EngineDiscoveryInfo,
    JobAccepted,
    OkResponse,
    OpenTerminalResult,
    RevealRequest,
    SystemInfo,
    UpdateInfo,
    Versions,
)
from ..state import ManagerState, get_state
from ..units import format_bytes
from .info import system_info

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.post("/system", response_model=SystemInfo)
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


@router.post("/system/brew", response_model=BrewInfo)
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


# The official installer command from https://brew.sh (checked 2026-10-04).
BREW_INSTALL = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'


@router.post(
    "/system/brew/install",
    response_model=OpenTerminalResult,
    responses=error_responses(409, 503),
)
def install_brew(state: State) -> OpenTerminalResult:
    """Homebrew needs the user's password, so its official
    installer runs in Terminal; the wizard then polls `POST /system/brew`."""
    if get_brew(state).installed:
        raise ApiError(409, "Homebrew is already installed", "brew_installed")
    result = state.macos.open_in_terminal(BREW_INSTALL)
    if result.returncode:
        raise ApiError(503, result.stderr or "Could not open Terminal", "terminal_failed")
    return OpenTerminalResult(ok=True, command=BREW_INSTALL)


@router.post(
    "/engine/upgrade", response_model=JobAccepted, status_code=202, responses=error_responses(409)
)
async def engine_upgrade(state: State) -> JobAccepted:
    """stop → brew update && brew upgrade → rediscover → restart the model."""
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


@router.post("/doctor", response_model=DoctorReport, responses=error_responses(400, 404, 503))
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
            status="fail"
            if not engine.found or engine.support == "too_old"
            else "warn"
            if engine.support in ("untested", "unknown")
            else "ok",
            message=(
                f"Splash {engine.version} ({engine.source}); Splashboard supports {SUPPORTED_RANGE}"
                if engine.found
                else engine.error or "Splash is not installed"
            ),
            fix=None
            if engine.found and engine.support == "supported"
            else "brew upgrade incoai/tap/splash"
            if engine.found
            else "brew install incoai/tap/splash",
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
            # Bytes on disk: base 1024, as the web shows them.
            message=f"{format_bytes(system.disk.models.free_bytes)} free on the models volume",
            fix=None
            if system.disk.models.free_bytes > 2 * 1024**3
            else "Free space, or move the models folder in Settings → Storage",
        ),
        _permissions_check(state.paths.base),
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
    checks.extend(_more_checks(state))
    # A fix is advice for a check that is not ok; never shown beside a passing one.
    checks = [c if c.status != "ok" else c.model_copy(update={"fix": None}) for c in checks]
    return DoctorReport(ok=all(c.status != "fail" for c in checks), checks=checks)


def _permissions_check(base: Path) -> DoctorCheck:
    """~/.splash holds secrets-adjacent data (chats, usage, logs): owner-only, 700."""
    private = base.stat().st_mode & 0o077 == 0
    return DoctorCheck(
        id="permissions",
        label="Private data directory",
        status="ok" if private else "warn",
        message=f"{base} is private (700)" if private else f"{base} is readable by other users",
        fix=None if private else "chmod 700 " + str(base),
    )


def _more_checks(state: ManagerState) -> list[DoctorCheck]:
    """The rest of `splash doctor`: the shim's place on PATH, ports,
    the Hugging Face token and leftover integration state."""
    from ..cli import install as shim
    from ..secrets import SecretName, SecretsError

    checks = []
    first = shim.on_path(state.paths.bin_dir)
    checks.append(
        DoctorCheck(
            id="shim",
            label="splash CLI shim",
            status="ok" if first else "warn",
            message=f"{state.paths.shim} comes first on PATH"
            if first
            else f"{state.paths.bin_dir} is not first on PATH, so `splash` runs the engine CLI",
            fix=None if first else "Add it in Settings → About (CLI), then open a new terminal",
        )
    )
    g = state.settings.current.global_
    bound = state.bound
    checks.append(
        DoctorCheck(
            id="ports",
            label="Ports",
            status="ok",
            message=(
                f"Manager on {bound[0]}:{bound[1]}" if bound else f"Manager port {g.server.port}"
            )
            + f"; Claude Desktop gateway port {g.integrations.claude_desktop.port}",
        )
    )
    try:
        override = bool(state.secrets.get(SecretName.HF_TOKEN))
    except SecretsError:
        override = False
    from ..models.hf import login_token

    source = "Keychain override" if override else "hf login / HF_TOKEN" if login_token() else None
    checks.append(
        DoctorCheck(
            id="hf_token",
            label="Hugging Face token",
            status="ok" if source else "warn",
            message=f"Using the {source}" if source else "No token: gated or private models fail",
            fix=None
            if source
            else "Run `hf auth login`, or add a token in Settings → Hugging Face",
        )
    )
    leftovers = sorted(getattr(state.integrations, "recovery", set()) or set())
    checks.append(
        DoctorCheck(
            id="integrations",
            label="Desktop integrations",
            status="warn" if leftovers else "ok",
            message="Connected before an unclean shutdown: " + ", ".join(leftovers)
            if leftovers
            else "No leftover integration state",
            fix="Integrations → Restore now, or `splash launch <app> --restore`"
            if leftovers
            else None,
        )
    )
    return checks


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
