"""D58 done-condition: a hostile local process cannot use the admin API.

A real manager runs as its own OS process with a throwaway home. A second OS process
(`python -c`, standing in for any local program: another user's tool, a sandboxed app
with network access) sends the headers a browser page would, forged: `Origin` naming
the manager and `Sec-Fetch-Site: same-origin`, with no cookie and no token. It tries
to (a) change a setting, (b) read the API key, (c) create and start an MCP stdio
server, i.e. run a command as the owner. All three must be refused and nothing may
change, with admin sign-in on (the default) and again with it turned off. The same
calls with the CLI token are the positive control: they succeed, so the refusals are
the guard's doing, not a broken route.

The second test covers an MCP server the owner already configured: the hostile process
must not be able to start it either (D58 amendment, 2026-10-07). Listing tools starts
every enabled server, so it is a POST that needs a credential even with sign-in off;
the test also tries the old GET, HEAD and path variants a guard might mis-match.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import FAKE_SPLASH, REPO

MANAGER_DIR = REPO / "manager"
AVOID_PORTS = {8000, 8123, 9999}

# The hostile process. argv: port, marker path, then an optional bearer token for the
# positive control. Prints one JSON object: each attempt's status and body.
ATTACKER = r"""
import json, sys, urllib.error, urllib.request

port, marker = int(sys.argv[1]), sys.argv[2]
token = sys.argv[3] if len(sys.argv) > 3 else None
base = f"http://127.0.0.1:{port}"
forged = {"Origin": base, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
if token:
    forged["Authorization"] = "Bearer " + token


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers=forged)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"status": response.status, "body": response.read().decode()}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "body": error.read().decode()}


out = {}
settings = {
    "version": 2,
    "global": {
        "server": {"host": "127.0.0.1", "port": port, "allowed_origins": ["*"]},
        "security": {"api_key_required": False, "admin_requires_key": False},
    },
}
out["a_change_setting"] = call("PUT", "/api/admin/settings", settings)
out["b_read_api_key"] = call("GET", "/api/admin/settings/secrets/api-key")
servers = {"servers": {"pwn": {"command": "/bin/sh", "args": ["-c", "touch " + marker]}}}
out["c_create_mcp_server"] = call("PUT", "/api/admin/mcp/servers", servers)
out["c_start_mcp_servers"] = call("POST", "/api/admin/mcp/tools", {})
out["mint_login_link"] = call("POST", "/api/admin/auth/link")
print(json.dumps(out))
"""


# The hostile process against a server the owner configured. argv: port, server name.
START_CONFIGURED = r"""
import json, sys, urllib.error, urllib.request

port, server = int(sys.argv[1]), sys.argv[2]
base = f"http://127.0.0.1:{port}"
forged = {"Origin": base, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers=forged)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"status": response.status, "body": response.read().decode()}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "body": error.read().decode()}


out = {}
for method in ("POST", "GET", "HEAD"):
    for path in (
        "/api/admin/mcp/tools",
        "/api/admin/mcp/tools/",
        "//api/admin/mcp/tools",
        "/api/admin/%6dcp/tools",
        "/api/admin/mcp%2Ftools",
        "/API/admin/mcp/tools",
        "/api/admin//mcp/tools",
    ):
        out[f"{method} {path}"] = call(method, path, {} if method == "POST" else None)
call_body = {"server": server, "tool": "anything", "arguments": {}, "confirmed": True}
out["POST /api/admin/mcp/call"] = call("POST", "/api/admin/mcp/call", call_body)
print(json.dumps(out))
"""


def free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in AVOID_PORTS:
            return port


@dataclass
class Manager:
    process: subprocess.Popen[bytes]
    home: Path
    port: int

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        return (self.home / "run" / "cli.token").read_text().strip()

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    def settings_bytes(self) -> bytes | None:
        """settings.json as it is on disk; None while a fresh install has none."""
        try:
            return self.settings_file.read_bytes()
        except FileNotFoundError:
            return None

    def admin(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return httpx.request(
            method,
            self.base + "/api/admin" + path,
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[Manager]:
    home = tmp_path / "attack-home"
    port = free_port()
    env = {
        **os.environ,
        "SPLASH_GUI_HOME": str(home),
        "SPLASH_GUI_SECRETS": "file",
        "SPLASH_GUI_REAL_SPLASH": str(FAKE_SPLASH / "pkg" / "bin" / "splash"),
        "SPLASH_GUI_FAKE_DATA": str(home / "fake-data"),
        "SPLASH_GUI_UPDATE_CHECK": "0",
        "SPLASH_GUI_OSASCRIPT": "0",
        "HF_HOME": str(home / "hf-home"),
        "HF_HUB_CACHE": str(home / "models"),
        "HOME": str(tmp_path / "user-home"),
    }
    (tmp_path / "user-home").mkdir()
    log = (tmp_path / "manager.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "splash_gui", "--port", str(port), "--log-level", "warning"],
        cwd=MANAGER_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    started = Manager(process, home, port)
    deadline = time.monotonic() + 60
    while True:
        if process.poll() is not None:
            log.close()
            raise AssertionError((tmp_path / "manager.log").read_text())
        try:
            if httpx.get(started.base + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("the manager did not start in 60 s")
        time.sleep(0.2)
    try:
        yield started
    finally:
        with_group = os.getpgid(process.pid)
        os.killpg(with_group, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(with_group, signal.SIGKILL)
            process.wait()
        log.close()


def attack(manager: Manager, marker: Path, token: str | None = None) -> dict[str, Any]:
    """Run the hostile requests from a separate OS process."""
    argv = [sys.executable, "-c", ATTACKER, str(manager.port), str(marker)]
    if token:
        argv.append(token)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    outcome: dict[str, Any] = json.loads(result.stdout)
    return outcome


def mcp_processes(marker: Path) -> list[str]:
    found = subprocess.run(
        ["/usr/bin/pgrep", "-fl", str(marker)], capture_output=True, text=True, check=False
    )
    return [line for line in found.stdout.splitlines() if "pgrep" not in line]


def assert_refused(manager: Manager, marker: Path, key: str, *, sign_in: bool) -> None:
    before = manager.settings_bytes()
    outcome = attack(manager, marker)
    print(f"\nsign-in {'on' if sign_in else 'off'}, forged Origin, no credential:")
    for name, response in outcome.items():
        print(f"  {name:22} {response['status']} {response['body'][:110]}")

    for name in (
        "a_change_setting",
        "b_read_api_key",
        "c_create_mcp_server",
        "c_start_mcp_servers",
    ):
        assert outcome[name]["status"] in (401, 403), (name, outcome[name])
        error = json.loads(outcome[name]["body"])["error"]
        assert error["code"] == "auth_required", (name, error)
        assert error["details"] == {"admin_requires_key": sign_in}
    assert key not in json.dumps(outcome), "the API key leaked"
    assert "sk-splash-" not in json.dumps(outcome)
    assert outcome["mint_login_link"]["status"] in (401, 403)

    time.sleep(1.0)  # an MCP server, had one started, would have run by now
    assert manager.settings_bytes() == before, "settings.json changed"
    assert not marker.exists(), "the attacker's MCP command ran"
    assert mcp_processes(marker) == []
    settings = manager.admin("GET", "/settings").json()["settings"]
    assert settings["global"]["chat"]["mcp_servers"] == {}
    assert settings["global"]["security"]["admin_requires_key"] is sign_in


def test_hostile_local_process_cannot_use_the_admin(manager: Manager, tmp_path: Path) -> None:
    marker = tmp_path / "pwned-by-mcp"
    key = manager.admin("GET", "/settings/secrets/api-key").json()["key"]
    assert key.startswith("sk-splash-"), "the manager creates the key at first start"
    state = httpx.get(manager.base + "/api/admin/auth/state", timeout=10).json()
    assert state == {"admin_requires_key": True, "authenticated": False, "method": None}

    # 1. Admin sign-in on (the default).
    assert_refused(manager, marker, key, sign_in=True)

    # 2. The owner turns sign-in off: reads open up on this Mac, writes and secrets do not.
    doc = manager.admin("GET", "/settings").json()["settings"]
    doc["global"]["security"]["admin_requires_key"] = False
    assert manager.admin("PUT", "/settings", doc).status_code == 200
    assert_refused(manager, marker, key, sign_in=False)

    # 3. Positive control: the same calls, from a separate process, with the CLI token.
    outcome = attack(manager, marker, token=manager.token)
    print("\nsign-in off, same calls with the CLI token (positive control):")
    for name, response in outcome.items():
        shown = response["body"].replace(key, "<api key>")[:110]
        print(f"  {name:22} {response['status']} {shown}")
    assert outcome["a_change_setting"]["status"] == 200, outcome["a_change_setting"]
    changed = manager.admin("GET", "/settings").json()["settings"]["global"]["server"]
    assert changed["allowed_origins"] == ["*"]
    assert json.loads(outcome["b_read_api_key"]["body"]) == {"key": key}
    assert outcome["c_create_mcp_server"]["status"] == 200
    assert outcome["c_start_mcp_servers"]["status"] == 200
    assert outcome["mint_login_link"]["status"] == 200
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists(), "with the token the MCP command runs, so the refusals were real"


def test_hostile_local_process_cannot_start_a_configured_mcp_server(
    manager: Manager, tmp_path: Path
) -> None:
    """The owner configured an MCP stdio server; a forged, credential-less local process
    tries to start it (list its tools, call it) with sign-in on and off. Refused, and
    the server's command never runs."""
    marker = tmp_path / "owner-mcp-started"
    owner = {"servers": {"owner": {"command": "/bin/sh", "args": ["-c", f"touch {marker}"]}}}
    assert manager.admin("PUT", "/mcp/servers", owner).status_code == 200

    def try_to_start(sign_in: bool) -> None:
        before = manager.settings_bytes()
        argv = [sys.executable, "-c", START_CONFIGURED, str(manager.port), "owner"]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, result.stderr
        outcome: dict[str, Any] = json.loads(result.stdout)
        print(f"\nsign-in {'on' if sign_in else 'off'}, configured server, no credential:")
        for name, response in outcome.items():
            print(f"  {name:36} {response['status']} {response['body'][:80]}")
        for name, response in outcome.items():
            assert response["status"] in (401, 403, 404, 405), (name, response)
        for name in ("POST /api/admin/mcp/tools", "POST /api/admin/mcp/call"):
            error = json.loads(outcome[name]["body"])["error"]
            assert error["code"] == "auth_required", (name, error)
        time.sleep(1.0)  # a started server would have run its command by now
        assert not marker.exists(), "the owner's MCP server was started without a credential"
        assert mcp_processes(marker) == []
        assert manager.settings_bytes() == before

    try_to_start(sign_in=True)
    doc = manager.admin("GET", "/settings").json()["settings"]
    doc["global"]["security"]["admin_requires_key"] = False
    assert manager.admin("PUT", "/settings", doc).status_code == 200
    try_to_start(sign_in=False)

    # Positive control: with the CLI token the same listing starts the server.
    listing = manager.admin("POST", "/mcp/tools")
    assert listing.status_code == 200, listing.text
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists(), "with the token the server starts, so the refusals were real"
