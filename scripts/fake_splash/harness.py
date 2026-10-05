"""Start, drive and stop the fake Splash engine from tests. Standard library
only, so the manager's tests can import it directly:

    import sys; sys.path.insert(0, "scripts/fake_splash")
    from harness import FakeSplash

    with FakeSplash(tmp_path, api_key="k") as engine:
        engine.set_mode("engine_recovering")
        engine.last_requests()

Every run is isolated under a base directory (SPLASH_GUI_FAKE_DATA for the
selection links, HF_HUB_CACHE for blobs, TMPDIR), never the real ~/.splash
or ~/Library/Application Support/Splash.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PKG = HERE / "pkg"
SPLASH_BIN = PKG / "bin" / "splash"
PYTHON = PKG / "python" / "bin" / "python3"
INSTALLER = PKG / "install" / "models.py"
DEFAULT_MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def base_dir(explicit: str | os.PathLike | None = None) -> Path:
    """SPLASH_GUI_HOME (default ~/.splash) unless a directory is given; tests
    should always give one."""
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("SPLASH_GUI_HOME", str(Path.home() / ".splash")))


def fake_env(base: str | os.PathLike, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the GUI gives Splash processes, rooted at `base`
    (SPEC §5/§6.2), plus FAKE_SPLASH_PYTHON so the fake's interpreter shim
    uses the current Python."""
    root = Path(base)
    (root / "cache" / "tmp").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HF_HUB_CACHE": str(root / "models"),
        "TMPDIR": str(root / "cache" / "tmp"),
        "SPLASH_GUI_FAKE_DATA": str(root / "fake-data"),
        "FAKE_SPLASH_PYTHON": sys.executable,
        "PYTHONUNBUFFERED": "1",
        # A hung fake dumps every thread's stack on SIGABRT (see `_dump_stacks`).
        "PYTHONFAULTHANDLER": "1",
    }
    env.pop("SPLASH_API_KEY", None)
    env.update(extra or {})
    return env


def run_installer(
    base: str | os.PathLike,
    args: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    **popen: Any,
) -> subprocess.Popen[str]:
    """Start PKG/python/bin/python3 PKG/install/models.py ARGS in its own
    process group, as the manager runs it (SPEC §9.4)."""
    return subprocess.Popen(
        [str(PYTHON), str(INSTALLER), *args],
        env=fake_env(base, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **popen,
    )


class FakeSplash:
    """One `PKG/bin/splash serve` process on a free port."""

    def __init__(
        self,
        base: str | os.PathLike,
        *,
        model: str = DEFAULT_MODEL,
        port: int | None = None,
        api_key: str | None = None,
        args: Iterable[str] = ("--no-webui",),
        env: Mapping[str, str] | None = None,
        fast: bool = True,
    ):
        self.base = Path(base)
        self.model = model
        self.port = port or free_port()
        self.api_key = api_key
        self.args = list(args)
        extra = {"FAKE_SPLASH_LOAD_SECONDS": "0.05", "FAKE_SPLASH_TOKS": "2000"} if fast else {}
        extra.update(env or {})
        if api_key is not None:
            extra["SPLASH_API_KEY"] = api_key
        self.env = fake_env(self.base, extra)
        self.process: subprocess.Popen | None = None
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self._lines = threading.Condition()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def command(self) -> list[str]:
        return [
            str(SPLASH_BIN),
            "serve",
            "--model",
            self.model,
            "--port",
            str(self.port),
            "--host",
            "127.0.0.1",
            *self.args,
        ]

    def _pump(self, stream, sink: list[str]) -> None:
        for line in stream:
            with self._lines:
                sink.append(line.rstrip("\n"))
                self._lines.notify_all()
        with self._lines:
            self._lines.notify_all()

    def start(self, *, wait: bool = True, timeout: float = 20.0) -> FakeSplash:
        self.process = subprocess.Popen(
            self.command,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        for stream, sink in ((self.process.stdout, self.stdout), (self.process.stderr, self.stderr)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()
        if wait:
            self.wait_ready(timeout)
        return self

    def wait_for_line(self, pattern: str, timeout: float = 10.0, *, stream: str = "any") -> str:
        """The first output line matching `pattern` (regex)."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self._lines:
            while True:
                sources = {"stdout": self.stdout, "stderr": self.stderr}
                for name, lines in sources.items():
                    if stream in ("any", name):
                        for line in lines:
                            if regex.search(line):
                                return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self.process is not None and self.process.poll() is not None and remaining > 0.5:
                    self._lines.wait(0.2)
                    deadline = min(deadline, time.monotonic() + 0.5)
                    continue
                self._lines.wait(min(remaining, 0.2))
        self._dump_stacks()
        raise TimeoutError(f"no line matching {pattern!r}; stdout={self.stdout} stderr={self.stderr}")

    def _dump_stacks(self) -> None:
        """Abort a still-running fake (and its installer child) so faulthandler
        writes their Python stacks to stderr, which the TimeoutError then shows."""
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGABRT)
        except OSError:
            return
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return
        time.sleep(0.3)  # let the pump threads drain the pipes

    def wait_ready(self, timeout: float = 20.0) -> None:
        self.wait_for_line(r" Ready · ", timeout, stream="stdout")

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        auth: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth and self.api_key is not None:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, method: str, path: str, body: object = None, **kwargs: Any) -> tuple[int, Any]:
        """(status, decoded JSON body or None)."""
        status, _, data = self.request(method, path, body, **kwargs)
        return status, json.loads(data) if data else None

    def set_mode(self, mode: str | None = None, config: Mapping[str, object] | None = None, **fields: object) -> dict:
        """POST /_fake/mode: a mode and/or FakeConfig fields (as `config` or
        keyword arguments)."""
        payload: dict = {}
        if mode is not None:
            payload["mode"] = mode
        merged = {**(config or {}), **fields}
        if merged:
            payload["config"] = merged
        status, state = self.json("POST", "/_fake/mode", payload, auth=False)
        if status != 200:
            raise RuntimeError(f"set_mode failed: {state}")
        return dict(state)

    def state(self) -> dict:
        return dict(self.json("GET", "/_fake/state", auth=False)[1])

    def last_requests(self, n: int | None = None) -> list[dict]:
        path = "/_fake/last_requests" + (f"?n={n}" if n else "")
        return list(self.json("GET", path, auth=False)[1]["requests"])

    def clear_requests(self) -> None:
        self.request("DELETE", "/_fake/last_requests", auth=False)

    def crash(self, code: int = 1, signal_name: str | None = None) -> None:
        body: dict = {"code": code}
        if signal_name:
            body["signal"] = signal_name
        with contextlib.suppress(OSError):  # URLError and ConnectionError are OSErrors
            self.request("POST", "/_fake/crash", body, auth=False, timeout=5)

    def signal_group(self, number: int = signal.SIGINT) -> None:
        """Signal the engine's process group, as the manager does (SPEC §6.2,
        §6.5); the group includes the installer while `serve` prepares."""
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                # Only a group this process leads (start_new_session), never ours.
                if os.getpgid(self.process.pid) == self.process.pid != os.getpgrp():
                    os.killpg(self.process.pid, number)
                else:
                    self.process.send_signal(number)

    def interrupt(self) -> None:
        self.signal_group(signal.SIGINT)

    def stop(self, timeout: float = 15.0) -> int | None:
        """SIGINT, then wait; a second SIGINT after `timeout`, then SIGKILL
        (the manager's stop sequence, SPEC §6.5)."""
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.signal_group(signal.SIGINT)
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.signal_group(signal.SIGINT)
                try:
                    self.process.wait(5)
                except subprocess.TimeoutExpired:
                    self.signal_group(signal.SIGKILL)
                    self.process.wait()
        return self.process.returncode

    def __enter__(self) -> FakeSplash:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
