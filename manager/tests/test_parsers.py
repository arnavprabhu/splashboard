"""Validation parity with Splash's own parsers (SPEC §8.3, §20.2)."""

from __future__ import annotations

import json
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from splash_gui.settings import parsers as p

from .conftest import HAVE_SPLASH, SPLASH_PKG, SPLASH_PYTHON

# (our parser name, Splash expression, inputs)
CASES: dict[str, tuple[str, list[str]]] = {
    "parse_max_context": (
        "serve_options.parse_max_context",
        [
            "auto",
            "AUTO",
            " auto ",
            "100K",
            "100k",
            "4K",
            "256K",
            "257K",
            "262144",
            "262145",
            "0",
            "-1",
            "1",
            "abc",
            "",
            "1.5K",
        ],
    ),
    "parse_max_memory": (
        "serve_options.parse_max_memory",
        [
            "auto",
            "32G",
            "32GB",
            "32GiB",
            "32gib",
            "28g",
            "1",
            "0",
            "-5",
            "1.5G",
            "G",
            "128M",
            "9223372036854775807",
            "9223372036854775808",
            "1T",
            " 64 K ",
        ],
    ),
    "parse_max_cache_disk": (
        "serve_options.parse_max_cache_disk",
        [
            "0",
            " 0 ",
            "0G",
            "5G",
            "32G",
            "auto",
            "",
            "-1",
        ],
    ),
    "parse_request_size": ("serve_options.parse_request_size", ["128M", "auto", "0", "1", "1G"]),
    "parse_request_timeout": (
        "serve_options.parse_request_timeout",
        [
            "3600",
            "0",
            "-1",
            "nan",
            "inf",
            "1e3",
            "abc",
            "0.5",
        ],
    ),
    "parse_queue_size": ("serve_options.parse_queue_size", ["32", "0", "-1", "1.5", "abc", "1"]),
    "parse_decode_share": (
        "serve_options.parse_decode_share",
        [
            "0.5",
            "0",
            "-0.1",
            "nan",
            "inf",
            "2",
            "x",
        ],
    ),
    "parse_max_image_pixels": (
        "serve_options.parse_max_image_pixels",
        [
            "65536",
            "65535",
            "4194304",
            "4194305",
            "abc",
            "1000000",
        ],
    ),
    "parse_served_model_name": (
        "serve_options.parse_served_model_name",
        [
            "qwen27",
            "",
            "a b",
            "a%b",
            "a?b",
            "a#b",
            "a\\b",
            "a/b",
            "a//b",
            "./x",
            "x/..",
            "café",
            "tab\tx",
            "-x",
            "org/model:tag",
        ],
    ),
    "parse_reasoning_effort": (
        "serve_options.parse_reasoning_effort",
        [
            "none",
            "minimal",
            "max",
            "xhigh",
            "huge",
            "",
            "High",
        ],
    ),
    "parse_allowed_origin": (
        "serve_options.parse_allowed_origin",
        [
            "*",
            "tauri://localhost",
            "http://localhost:3000",
            "tauri://*",
            "http://*.example.com",
            "localhost",
            "http://x/path",
            "https://EXAMPLE.com",
            "http://[::1]:8080",
            "",
        ],
    ),
    "parse_api_key": ("serve_options.parse_api_key", ["abc", "a b", "", "é", "sk-1"]),
    "parse_port": ("launcher._parse_port", ["8000", "0", "1", "65535", "65536", "x"]),
    "split_model_id": (
        "models.split_model_id",
        [
            "mlx-community/Qwen3.8-27B-4bit",
            "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M",
            "qwen",
            "a/b:",
            "a/b:x..y",
            "a--b/c",
            "a/b.git",
            "incoai/Qwen3.8-27B-Splash",
            "a/b/c",
            "prism-ml/Ternary-Bonsai-2-27B-gguf:PQ2_0",
        ],
    ),
}


def _ours(name: str, value: str) -> list[Any]:
    fn: Callable[[str], Any] = getattr(p, name)
    try:
        result = fn(value)
    except ValueError as error:
        return ["err", str(error)]
    return ["ok", json.loads(json.dumps(result, default=str))]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("parse_max_context", "100K", ["ok", 102400]),
        ("parse_max_context", "auto", ["ok", None]),
        (
            "parse_max_context",
            "257K",
            ["err", "must be 'auto' or a token count up to 256K, such as 100K"],
        ),
        ("parse_max_memory", "32GiB", ["ok", 32 * 1024**3]),
        (
            "parse_max_memory",
            "1.5G",
            ["err", "must be 'auto' or a positive byte count such as 32G"],
        ),
        ("parse_max_cache_disk", "0", ["ok", 0]),
        ("parse_max_cache_disk", "auto", ["err", "use 0 to disable, or a size such as 5G"]),
        (
            "parse_request_timeout",
            "0",
            ["err", "must be a positive number of seconds such as 3600"],
        ),
        ("parse_queue_size", "0", ["err", "must be a positive number of requests such as 32"]),
        ("parse_decode_share", "0", ["ok", 0.0]),
        ("parse_max_image_pixels", "65535", ["err", "must be between 65536 and 4194304 pixels"]),
        (
            "parse_served_model_name",
            "a b",
            ["err", "model alias must be a non-empty name without whitespace or URL delimiters"],
        ),
        (
            "parse_served_model_name",
            "x/..",
            ["err", "model alias must be a non-empty name without whitespace or URL delimiters"],
        ),
        ("parse_allowed_origin", "tauri://localhost", ["ok", ["tauri", "localhost", None]]),
        ("parse_allowed_origin", "http://localhost", ["ok", ["http", "localhost", 80]]),
        ("parse_allowed_origin", "*", ["ok", "*"]),
        (
            "split_model_id",
            "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M",
            ["ok", ["unsloth/Qwen3.8-27B-GGUF", "UD-Q4_K_M"]],
        ),
        (
            "split_model_id",
            "qwen",
            ["err", "model must be a full Hugging Face repository ID (owner/repo)"],
        ),
    ],
)
def test_known_outcomes(name: str, value: str, expected: list[Any]) -> None:
    assert _ours(name, value) == expected


def test_allowed_origin_pattern_message() -> None:
    kind, message = _ours("parse_allowed_origin", "tauri://*")
    assert kind == "err"
    assert "patterns such as tauri://* or http://*.example.com are not supported" in message


def test_legacy_detection() -> None:
    assert p.is_legacy_package("incoai/Qwen3.8-27B-Splash")
    assert p.is_legacy_package("incoai/Qwen3.6-35B-A3B-Splash")
    assert not p.is_legacy_package("mlx-community/Qwen3.8-27B-4bit")
    assert not p.is_legacy_package("incoai/Qwen3.8-27B-DFlash2")


def test_commit_pin_detection() -> None:
    assert p.is_commit("a" * 40)
    assert not p.is_commit("main")
    assert not p.is_commit(None)


def test_bind_hosts() -> None:
    assert p.parse_bind_host("0.0.0.0") == "0.0.0.0"  # noqa: S104
    assert p.is_loopback_host("127.0.0.1")
    assert p.is_loopback_host("::1")
    assert p.is_loopback_host("localhost")
    assert not p.is_loopback_host("0.0.0.0")  # noqa: S104
    with pytest.raises(ValueError):
        p.parse_bind_host("my mac")


_SPLASH_SCRIPT = textwrap.dedent(
    """
    import argparse, json, sys
    from server import serve_options
    from install import launcher, models
    cases = json.load(sys.stdin)
    out = {}
    for name, (expr, values) in cases.items():
        module, attr = expr.split(".")
        fn = getattr({"serve_options": serve_options, "launcher": launcher,
                      "models": models}[module], attr)
        rows = []
        for value in values:
            try:
                rows.append(["ok", json.loads(json.dumps(fn(value), default=str))])
            except (argparse.ArgumentTypeError, models.ModelError) as error:
                rows.append(["err", str(error)])
        out[name] = rows
    json.dump(out, sys.stdout)
    """
)


@pytest.mark.skipif(not HAVE_SPLASH, reason="Splash 1.2.0 is not installed via Homebrew")
def test_parity_with_splash_parsers(tmp_path: Path) -> None:
    """Run every case through Splash's own parsers (bundled Python) and compare."""
    result = subprocess.run(
        [str(SPLASH_PYTHON), "-P", "-c", _SPLASH_SCRIPT],
        input=json.dumps(CASES),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PYTHONPATH": str(SPLASH_PKG), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)},
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    theirs = json.loads(result.stdout)
    mismatches = []
    for name, (_, values) in CASES.items():
        for value, expected in zip(values, theirs[name], strict=True):
            ours = _ours(name, value)
            if ours != expected:
                mismatches.append((name, value, ours, expected))
    assert not mismatches, mismatches
