"""Logs, crash traces and diagnostics (SPEC §14 Logs, §10.8)."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import io
import re
import zipfile
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from ..engine.api import get_engine
from ..errors import SSE_RESPONSES, ApiError, error_responses
from ..schemas import (
    CancelResult,
    DeletedBytes,
    DiagnosticsBundle,
    LogLine,
    LogSource,
    LogTail,
    TraceItem,
    TraceList,
)
from ..secrets import redact_mapping
from ..sse import sse_response
from ..state import ManagerState, get_state
from ..system.api import get_system, get_versions

router = APIRouter()
BACKFILL_LINES = 200
BACKFILL_BYTES = 256 * 1024
DIAGNOSTIC_LOG_LINES = 500
POLL_INTERVAL_S = 0.25
# Splash's own name for its traces (server/crash_trace.py lists this glob).
TRACE_GLOB = "splash-crash-g*-*.json"
State = Annotated[ManagerState, Depends(get_state)]
_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) (\S+) (.*)$")
_ERROR = re.compile(r"\b(error|exception|traceback|fatal|failed)\b", re.IGNORECASE)
_WARN = re.compile(r"\b(warn(ing)?|deprecated)\b", re.IGNORECASE)


def _log_file(state: ManagerState, source: LogSource) -> Path:
    return state.paths.engine_log if source == "engine" else state.paths.manager_log


def _tail(path: Path, count: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=count)]
    except FileNotFoundError:
        return []


def parse_line(seq: int, raw: str, source: LogSource) -> LogLine:
    """Heuristic level parsing (SPEC §10.8): manager lines carry a level; engine lines don't."""
    match = _LINE.match(raw)
    ts, text, stream, level = None, raw, None, "info"
    if match:
        stamp, second, rest = match.groups()
        try:
            ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").astimezone(UTC).isoformat()
        except ValueError:
            ts = None
        if source == "engine":
            stream = second if second in ("stdout", "stderr") else None
            text = rest
        else:
            level = {
                "WARNING": "warn",
                "ERROR": "error",
                "CRITICAL": "error",
                "DEBUG": "debug",
            }.get(second, "info")
            text = rest
    if source == "engine":
        level = "error" if _ERROR.search(text) else "warn" if _WARN.search(text) else "info"
    return LogLine(seq=seq, ts=ts, level=level, stream=stream, text=text)  # type: ignore[arg-type]


@router.get("/logs/{source}", response_model=LogTail)
def tail(
    state: State, source: LogSource, tail: Annotated[int, Query(ge=1, le=10000)] = 500
) -> LogTail:
    lines = _tail(_log_file(state, source), tail)
    return LogTail(source=source, lines=[parse_line(i, raw, source) for i, raw in enumerate(lines)])


async def follow(
    path: Path, source: LogSource, poll: float = POLL_INTERVAL_S
) -> AsyncGenerator[tuple[str, Any], None]:
    """`backfill` with the last lines, then one `line` per new line. Survives rotation
    (a new inode) and truncation (Clear logs) by reopening from the start."""
    # Backfill and the follow offset come from one snapshot, so no line is lost or repeated.
    try:
        stat = path.stat()
        inode, offset = stat.st_ino, stat.st_size
        with path.open("rb") as handle:
            handle.seek(max(0, offset - BACKFILL_BYTES))
            head = handle.read(offset - handle.tell())
    except FileNotFoundError:
        inode, offset, head = None, 0, b""
    complete_head = head[: head.rfind(b"\n") + 1] if b"\n" in head else b""
    offset -= len(head) - len(complete_head)
    raw_lines = complete_head.decode("utf-8", "replace").splitlines()[-BACKFILL_LINES:]
    backfill = [parse_line(i, raw, source).model_dump() for i, raw in enumerate(raw_lines)]
    seq = len(backfill)
    yield "backfill", {"lines": backfill}
    partial = b""
    while True:
        await asyncio.sleep(poll)
        try:
            stat = path.stat()
        except FileNotFoundError:
            inode, offset, partial = None, 0, b""
            continue
        if stat.st_ino != inode or stat.st_size < offset:
            inode, offset, partial = stat.st_ino, 0, b""
        if stat.st_size == offset:
            continue
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(stat.st_size - offset)
        offset += len(chunk)
        *complete, partial = (partial + chunk).split(b"\n")
        for data in complete:
            text = data.decode("utf-8", "replace").rstrip("\r")
            yield "line", parse_line(seq, text, source).model_dump()
            seq += 1


@router.get("/logs/{source}/stream", response_class=StreamingResponse, responses=SSE_RESPONSES)
def stream(state: State, source: LogSource) -> StreamingResponse:
    return sse_response(follow(_log_file(state, source), source))


@router.get(
    "/logs/{source}/download",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
def download(state: State, source: LogSource) -> Response:
    base = _log_file(state, source)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(base.parent.glob(base.name + "*")):
            archive.write(path, path.name)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{source}-logs-{stamp}.zip"'},
    )


@router.delete("/logs", response_model=DeletedBytes)
def clear_logs(state: State) -> DeletedBytes:
    """Truncates the live files and deletes rotated ones."""
    freed = 0
    for base in (state.paths.manager_log, state.paths.engine_log):
        for path in base.parent.glob(base.name + "*"):
            size = path.stat().st_size
            if path == base:
                with path.open("r+b") as handle:
                    handle.truncate(0)
            else:
                path.unlink()
            freed += size
    return DeletedBytes(deleted_bytes=freed)


def _is_trace_name(name: str) -> bool:
    return "/" not in name and fnmatch.fnmatchcase(name, TRACE_GLOB)


@router.get("/traces", response_model=TraceList)
def traces(state: State) -> TraceList:
    directory = state.crash_trace_dir
    items = []
    if directory.is_dir():
        for path in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and _is_trace_name(path.name):
                stat = path.stat()
                items.append(
                    TraceItem(
                        name=path.name,
                        path=str(path),
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    )
                )
    return TraceList(
        enabled=state.settings.current.global_.advanced.crash_trace,
        directory=str(directory),
        traces=items,
    )


@router.post(
    "/traces/{name}/replay",
    response_class=StreamingResponse,
    responses={**SSE_RESPONSES, **error_responses(404)},
)
def replay(state: State, name: str) -> StreamingResponse:
    if not _is_trace_name(name):
        raise ApiError(400, "Invalid trace name", "invalid_trace")
    path = state.crash_trace_dir / name
    if not path.is_file() or path.is_symlink():
        raise ApiError(404, "Trace not found", "trace_not_found")
    engine = state.engine()
    if not engine.python or not engine.pkg:
        raise ApiError(503, "Install Splash to replay traces", "engine_unavailable")

    async def output() -> AsyncGenerator[tuple[str, Any], None]:
        proc = await asyncio.create_subprocess_exec(
            str(engine.python),
            "-m",
            "server.crash_trace",
            str(path),
            cwd=engine.pkg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        state.replays[name] = proc
        try:
            assert proc.stdout
            seq = 0
            async for line in proc.stdout:
                yield (
                    "line",
                    parse_line(seq, line.decode(errors="replace").rstrip(), "engine").model_dump(),
                )
                seq += 1
            yield "exit", {"code": await proc.wait()}
        finally:
            if state.replays.get(name) is proc:
                del state.replays[name]
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return sse_response(output())


@router.post(
    "/traces/{name}/replay/cancel", response_model=CancelResult, responses=error_responses(400)
)
def cancel_replay(state: State, name: str) -> CancelResult:
    """Replay → Stop: end the running replay of this trace; the stream then sends
    `exit` with the signal's code (`-15`). `cancelled` is false when none runs."""
    if not _is_trace_name(name):
        raise ApiError(400, "Invalid trace name", "invalid_trace")
    proc = state.replays.get(name)
    if proc is None or proc.returncode is not None:
        return CancelResult(cancelled=False)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    return CancelResult(cancelled=True)


@router.delete("/traces/{name}", status_code=204, responses=error_responses(400, 404))
def delete_trace(state: State, name: str) -> Response:
    if not _is_trace_name(name):
        raise ApiError(400, "invalid trace name", "invalid_trace")
    path = state.crash_trace_dir / name
    if not path.is_file():
        raise ApiError(404, f"no trace named {name}", "trace_not_found")
    path.unlink()
    return Response(status_code=204)


@router.get("/diagnostics", response_model=DiagnosticsBundle)
def diagnostics(state: State) -> DiagnosticsBundle:
    """SPEC §10.8 "Copy diagnostic bundle": everything a Splash issue report asks for."""
    known = state.secrets.known_values()
    settings = redact_mapping(state.settings.current.to_json_dict(), known)
    tail = [
        state.secrets.redact(line) for line in _tail(state.paths.engine_log, DIAGNOSTIC_LOG_LINES)
    ]
    try:
        system = get_system(state)
    except Exception:  # hardware probes are best-effort
        system = None
    status = state.raw_status()
    return DiagnosticsBundle(
        generated_at=datetime.now(UTC).isoformat(),
        versions=get_versions(state),
        system=system,
        settings=settings,
        engine=get_engine(state),
        status=redact_mapping(status, known) if status is not None else None,
        engine_log_tail=tail,
    )
