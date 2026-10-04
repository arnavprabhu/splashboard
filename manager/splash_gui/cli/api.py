"""Installing the `splash` shim from the app and the settings page (SPEC §12.1)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ..errors import ApiError, error_responses
from ..paths import Paths
from ..schemas import RcFileStatus, ShimInstallRequest, ShimStatus
from ..state import ManagerState, get_state
from . import install as shim

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def home_of(state: ManagerState) -> Path:
    """The home whose shell rc files carry the PATH block (tests: a throwaway one)."""
    return state.user_home if state.user_home is not None else Path.home()


def status(paths: Paths, home: Path | None = None) -> ShimStatus:
    return ShimStatus(
        shim=str(paths.shim),
        installed=shim.shim_is_ours(paths),
        executable=paths.shim.exists() and paths.shim.stat().st_mode & 0o100 != 0,
        command=" ".join(shim.interpreter_command()),
        on_path=shim.on_path(paths.bin_dir),
        rc_files=[RcFileStatus.model_validate(entry) for entry in shim.rc_report(home)],
        problems=shim.self_test(paths, home),
    )


@router.get("/cli/shim", response_model=ShimStatus)
def get_shim(state: State) -> ShimStatus:
    return status(state.paths, home_of(state))


@router.post("/cli/shim", response_model=ShimStatus, responses=error_responses(500))
def post_shim(state: State, body: ShimInstallRequest | None = None) -> ShimStatus:
    """Write the shim, and optionally add our PATH block to the shell rc files."""
    paths = state.paths
    home = home_of(state)
    try:
        shim.install_shim(paths)
        # The user's rc files change only on this explicit request (SPEC §12.1).
        if body and body.add_to_path:
            for entry in shim.rc_report(home):
                shim.add_to_rc(Path(str(entry["file"])), paths.bin_dir)
    except OSError as error:
        raise ApiError(500, f"Could not install the shim: {error}", "shim_install_failed") from None
    return status(paths, home)


@router.delete("/cli/shim", status_code=204, responses=error_responses(404, 500))
def delete_shim(state: State, remove_path: bool = True) -> Response:
    """Remove the shim and, by default, the marked block we added to the rc files."""
    paths = state.paths
    if remove_path:
        for entry in shim.rc_report(home_of(state)):
            with contextlib.suppress(OSError):
                shim.remove_from_rc(Path(str(entry["file"])))
    if not shim.remove_shim(paths):
        raise ApiError(404, "The shim was not installed", "shim_not_installed")
    return Response(status_code=204)
