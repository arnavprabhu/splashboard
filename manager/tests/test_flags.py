"""`splash serve` argv and environment for every Appendix A row (SPEC §6.2, §20.2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from splash_gui.engine.flags import LaunchError, LaunchSpec, build_launch_for_model, serve_flags
from splash_gui.paths import Paths
from splash_gui.secrets import REDACTED, MemoryBackend, SecretName, SecretStore
from splash_gui.settings.effective import effective_serve
from splash_gui.settings.model import ExtraFlag, SettingsDocument
from splash_gui.settings.store import SettingsStore

from .conftest import HAVE_SPLASH, SPLASH_PKG, SPLASH_PYTHON

MLX = "mlx-community/Qwen3.8-27B-4bit"
GGUF = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
LEGACY = "incoai/Qwen3.8-27B-Splash"
CLI = "/opt/homebrew/opt/splash/bin/splash"
KEY = "splash-internal-test-key"
PORT = 18123
BASE = [CLI, "serve", "--model", MLX, "--port", str(PORT), "--host", "127.0.0.1", "--no-webui"]


def launch(
    paths: Paths,
    glob: dict[str, dict[str, Any]] | None = None,
    model_serve: dict[str, Any] | None = None,
    *,
    model: str = MLX,
    hf_token: str | None = None,
    base_env: dict[str, str] | None = None,
) -> LaunchSpec:
    raw = SettingsDocument().to_json_dict()
    for section, values in (glob or {}).items():
        node = raw["global"][section]
        for key, value in values.items():
            node[key] = value
    if model_serve is not None:
        raw["models"][model] = {"serve": model_serve, "sampling_defaults": {}, "profiles": {}}
    store = SettingsStore(paths)
    result, _ = store.save(raw)
    assert result.ok, result.errors
    secrets = SecretStore(MemoryBackend())
    if hf_token:
        secrets.set(SecretName.HF_TOKEN, hf_token)
    return build_launch_for_model(
        store,
        secrets,
        model,
        cli=CLI,
        internal_port=PORT,
        internal_key=KEY,
        base_env=base_env if base_env is not None else {"PATH": "/usr/bin:/bin"},
    )


def extra(spec: LaunchSpec, model: str = MLX) -> list[str]:
    base = [*BASE[:3], model, *BASE[4:]]
    assert list(spec.argv[: len(base)]) == base
    return list(spec.argv[len(base) :])


def test_defaults_give_the_minimal_command(paths: Paths) -> None:
    spec = launch(paths)
    assert extra(spec) == []
    assert spec.env["HF_HUB_CACHE"] == str(paths.models_dir)
    assert spec.env["TMPDIR"] == str(paths.cache_dir / "tmp")
    assert spec.env["SPLASH_API_KEY"] == KEY
    for absent in ("HF_TOKEN", "HF_HUB_OFFLINE", "HF_ENDPOINT", "SPLASH_CRASH_TRACE"):
        assert absent not in spec.env


# One row per Appendix A option: (global patch, model serve patch, expected extra argv).
ROWS: list[tuple[str, dict[str, Any], dict[str, Any] | None, list[str]]] = [
    ("--revision", {}, {"revision": "main"}, ["--revision", "main"]),
    ("--revision pin", {}, {"revision": "a" * 40}, ["--revision", "a" * 40]),
    (
        "--draft-model",
        {},
        {"draft_model": "incoai/Qwen3.8-27B-DFlash2"},
        ["--draft-model", "incoai/Qwen3.8-27B-DFlash2"],
    ),
    ("--language-only", {}, {"language_only": True}, ["--language-only"]),
    ("--offline", {"hf": {"offline": True}}, None, ["--offline"]),
    (
        "--served-model-name",
        {},
        {"served_model_names": ["qwen27", "-dash"]},
        ["--served-model-name", "qwen27", "--served-model-name=-dash"],
    ),
    (
        "--announce-served-name",
        {},
        {"served_model_names": ["q"], "announce_served_name": True},
        ["--served-model-name", "q", "--announce-served-name"],
    ),
    (
        "--default-reasoning-effort global",
        {"serve": {"default_reasoning_effort": "high"}},
        None,
        ["--default-reasoning-effort", "high"],
    ),
    (
        "--default-reasoning-effort model",
        {},
        {"default_reasoning_effort": "none"},
        ["--default-reasoning-effort", "none"],
    ),
    (
        "--default-reasoning-effort model default",
        {"serve": {"default_reasoning_effort": "max"}},
        {"default_reasoning_effort": None},
        [],
    ),
    ("--kv-format", {"serve": {"kv_format": "bf16"}}, None, ["--kv-format", "bf16"]),
    ("--kv-format model", {}, {"kv_format": "bf16"}, ["--kv-format", "bf16"]),
    ("--kv-format default", {"serve": {"kv_format": "int8"}}, None, []),
    ("--max-memory", {"serve": {"max_memory": "28G"}}, None, ["--max-memory", "28G"]),
    ("--max-memory auto", {"serve": {"max_memory": "AUTO"}}, None, []),
    ("--max-cache-disk", {"serve": {"max_cache_disk": "16G"}}, None, ["--max-cache-disk", "16G"]),
    ("--max-context global", {"serve": {"max_context": "100K"}}, None, ["--max-context", "100K"]),
    (
        "--max-context model",
        {"serve": {"max_context": "100K"}},
        {"max_context": "128K"},
        ["--max-context", "128K"],
    ),
    ("--max-context auto", {"serve": {"max_context": "auto"}}, None, []),
    ("--decode-share", {"serve": {"decode_share": 0.75}}, None, ["--decode-share", "0.75"]),
    ("--decode-share zero", {}, {"decode_share": 0}, ["--decode-share", "0"]),
    ("--decode-share default", {"serve": {"decode_share": 0.5}}, None, []),
    ("--allowed-host never passed", {"server": {"allowed_hosts": ["mymac.local"]}}, None, []),
    ("--allowed-origin never passed", {"server": {"allowed_origins": ["*"]}}, None, []),
    ("--host is the manager's", {"server": {"host": "127.0.0.1", "port": 9000}}, None, []),
    (
        "--max-request-size",
        {"serve": {"max_request_size": "256M"}},
        None,
        ["--max-request-size", "256M"],
    ),
    ("--max-request-size default", {"serve": {"max_request_size": "128MiB"}}, None, []),
    (
        "--max-image-pixels",
        {"serve": {"max_image_pixels": 1048576}},
        None,
        ["--max-image-pixels", "1048576"],
    ),
    ("--max-image-pixels model", {}, {"max_image_pixels": 65536}, ["--max-image-pixels", "65536"]),
    (
        "--request-timeout",
        {"serve": {"request_timeout": 3600}},
        None,
        ["--request-timeout", "3600"],
    ),
    (
        "--request-timeout fractional",
        {"serve": {"request_timeout": 2.5}},
        None,
        ["--request-timeout", "2.5"],
    ),
    ("--queue-size", {"serve": {"queue_size": 64}}, None, ["--queue-size", "64"]),
    ("--idle-release", {"serve": {"idle_release": "30m"}}, None, ["--idle-release", "30m"]),
    ("--idle-release off", {"serve": {"idle_release": "off"}}, None, ["--idle-release", "off"]),
    ("--idle-release default", {"serve": {"idle_release": "600"}}, None, []),
    ("--disable-ane", {"serve": {"disable_ane": True}}, None, ["--disable-ane"]),
    ("--disable-ane model", {}, {"disable_ane": True}, ["--disable-ane"]),
    ("--disable-ane model off", {"serve": {"disable_ane": True}}, {"disable_ane": False}, []),
    ("--allow-idle-sleep", {"serve": {"allow_idle_sleep": True}}, None, ["--allow-idle-sleep"]),
    (
        "extra_flags",
        {"engine": {"extra_flags": [{"flag": "--future", "value": "x"}, {"flag": "--switch"}]}},
        None,
        ["--future", "x", "--switch"],
    ),
]


@pytest.mark.parametrize(
    ("name", "glob", "model_serve", "expected"), ROWS, ids=[r[0] for r in ROWS]
)
def test_appendix_a_rows(
    paths: Paths,
    name: str,
    glob: dict[str, Any],
    model_serve: dict[str, Any] | None,
    expected: list[str],
) -> None:
    assert extra(launch(paths, glob, model_serve)) == expected


def test_extra_flag_abbreviating_a_managed_flag_is_refused_at_launch(paths: Paths) -> None:
    # A hand-edited settings.json skips validation; the launch must still refuse
    # `--hos 0.0.0.0`, which Splash's argparse reads as `--host 0.0.0.0`.
    serve = effective_serve(SettingsDocument(), MLX)
    with pytest.raises(LaunchError, match="overrides --host"):
        serve_flags(
            serve,
            cache_dir=paths.cache_dir,
            extra_flags=[ExtraFlag(flag="--hos", value="0.0.0.0")],  # noqa: S104
        )
    assert serve_flags(
        serve, cache_dir=paths.cache_dir, extra_flags=[ExtraFlag(flag="--future")]
    ) == ["--future"]


def test_persistent_cache_passes_cache_dir(paths: Paths) -> None:
    spec = launch(paths, {"serve": {"max_cache_disk": "32G", "persistent_cache": True}})
    assert extra(spec) == [
        "--max-cache-disk",
        "32G",
        "--persistent-cache",
        "--cache-dir",
        str(paths.cache_dir),
    ]


def test_moved_storage_dirs(paths: Paths, tmp_path: Path) -> None:
    models, cache = tmp_path / "m", tmp_path / "c"
    spec = launch(
        paths,
        {
            "storage": {"models_dir": str(models), "cache_dir": str(cache)},
            "serve": {"max_cache_disk": "1G", "persistent_cache": True},
        },
    )
    assert extra(spec)[-2:] == ["--cache-dir", str(cache)]
    assert spec.env["HF_HUB_CACHE"] == str(models)
    assert spec.env["TMPDIR"] == str(cache / "tmp")
    assert spec.directories == (models, cache, cache / "tmp")


def test_api_key_only_in_environment(paths: Paths) -> None:
    spec = launch(paths)
    assert "--api-key" not in spec.argv
    assert all(KEY not in arg for arg in spec.argv)
    assert spec.env["SPLASH_API_KEY"] == KEY
    assert KEY not in spec.display()
    assert spec.redacted_env()["SPLASH_API_KEY"] == REDACTED


def test_hf_token_override(paths: Paths) -> None:
    spec = launch(paths, hf_token="hf_abcdefghijklmnopqrstuvwxyz")
    assert spec.env["HF_TOKEN"] == "hf_abcdefghijklmnopqrstuvwxyz"
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in spec.display()


def test_inherited_hf_token_kept_without_override(paths: Paths) -> None:
    spec = launch(paths, base_env={"HF_TOKEN": "hf_inherited"})
    assert spec.env["HF_TOKEN"] == "hf_inherited"
    assert "HF_TOKEN" not in spec.set_env


def test_offline_sets_env(paths: Paths) -> None:
    spec = launch(paths, {"hf": {"offline": True}})
    assert spec.env["HF_HUB_OFFLINE"] == "1"


def test_hf_endpoint_and_crash_trace(paths: Paths) -> None:
    spec = launch(
        paths, {"hf": {"endpoint": "https://hf-mirror.com"}, "advanced": {"crash_trace": True}}
    )
    assert spec.env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert spec.env["SPLASH_CRASH_TRACE"] == "1"


def test_inherited_variables_are_scrubbed(paths: Paths) -> None:
    spec = launch(
        paths,
        base_env={
            "SPLASH_API_KEY": "user-key",
            "SPLASH_DEFAULT_REASONING_EFFORT": "max",
            "SPLASH_PORT": "9999",
            "SPLASH_CRASH_TRACE": "1",
            "HF_HUB_OFFLINE": "1",
            "TMPDIR": "/var/folders/x",
            "PYTHONPATH": "/somewhere",
            "HOME": "/Users/me",
        },
    )
    assert spec.env["SPLASH_API_KEY"] == KEY
    for gone in (
        "SPLASH_DEFAULT_REASONING_EFFORT",
        "SPLASH_PORT",
        "SPLASH_CRASH_TRACE",
        "HF_HUB_OFFLINE",
        "PYTHONPATH",
    ):
        assert gone not in spec.env
    assert spec.env["TMPDIR"] == str(paths.cache_dir / "tmp")
    assert spec.env["HOME"] == "/Users/me"


def test_full_command_order_matches_spec(paths: Paths) -> None:
    spec = launch(
        paths,
        {
            "hf": {"offline": True},
            "serve": {
                "default_reasoning_effort": "low",
                "kv_format": "bf16",
                "max_memory": "40G",
                "max_cache_disk": "32G",
                "persistent_cache": True,
                "max_context": "128K",
                "decode_share": 1,
                "max_request_size": "256M",
                "max_image_pixels": 65536,
                "request_timeout": 600,
                "queue_size": 8,
                "idle_release": "2h",
                "disable_ane": True,
                "allow_idle_sleep": True,
            },
        },
        {
            "revision": "main",
            "draft_model": "incoai/Qwen3.8-27B-DFlash2",
            "language_only": True,
            "served_model_names": ["q"],
            "announce_served_name": True,
        },
    )
    flags = [a for a in extra(spec) if a.startswith("--")]
    assert flags == [
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--served-model-name",
        "--announce-served-name",
        "--default-reasoning-effort",
        "--kv-format",
        "--max-memory",
        "--max-cache-disk",
        "--persistent-cache",
        "--cache-dir",
        "--max-context",
        "--decode-share",
        "--max-request-size",
        "--max-image-pixels",
        "--request-timeout",
        "--queue-size",
        "--idle-release",
        "--disable-ane",
        "--allow-idle-sleep",
    ]


def test_gguf_model_id_passes_through(paths: Paths) -> None:
    spec = launch(paths, model=GGUF)
    assert extra(spec, GGUF) == []


def test_legacy_package_minimal(paths: Paths) -> None:
    assert extra(launch(paths, model=LEGACY), LEGACY) == []


def test_legacy_package_refuses_source_options() -> None:
    from splash_gui.engine.flags import serve_flags
    from splash_gui.settings.effective import effective_serve

    raw = SettingsDocument().to_json_dict()
    raw["models"][LEGACY] = {"serve": {"language_only": True}}
    doc = SettingsDocument.model_validate(raw)  # bypasses cross-field validation on purpose
    with pytest.raises(LaunchError, match="legacy"):
        serve_flags(effective_serve(doc, LEGACY), cache_dir=Path("/c"))


def test_persistent_without_disk_refused() -> None:
    from splash_gui.engine.flags import serve_flags
    from splash_gui.settings.effective import effective_serve

    raw = SettingsDocument().to_json_dict()
    raw["global"]["serve"]["persistent_cache"] = True
    doc = SettingsDocument.model_validate(raw)
    with pytest.raises(LaunchError, match="--persistent-cache needs --max-cache-disk"):
        serve_flags(effective_serve(doc, MLX), cache_dir=Path("/c"))


def test_display_is_shell_quoted(paths: Paths) -> None:
    text = launch(paths).display()
    assert text.startswith("HF_HUB_CACHE=")
    assert f"SPLASH_API_KEY={REDACTED}" in text
    assert text.endswith(f"--model {MLX} --port {PORT} --host 127.0.0.1 --no-webui")


@pytest.mark.skipif(not HAVE_SPLASH, reason="Splash 1.3.0 is not installed via Homebrew")
def test_splash_launcher_accepts_our_argv(paths: Paths, tmp_path: Path) -> None:
    """Splash's own launcher parser reads the generated command as intended."""
    spec = launch(
        paths,
        {
            "hf": {"offline": True},
            "serve": {
                "default_reasoning_effort": "low",
                "kv_format": "bf16",
                "max_memory": "40G",
                "max_cache_disk": "32G",
                "persistent_cache": True,
                "max_context": "100K",
                "decode_share": 0,
                "max_request_size": "256M",
                "max_image_pixels": 65536,
                "request_timeout": 2.5,
                "queue_size": 8,
                "idle_release": "off",
                "disable_ane": True,
                "allow_idle_sleep": True,
            },
        },
        {
            "revision": "main",
            "language_only": True,
            "served_model_names": ["q", "-x"],
            "announce_served_name": True,
        },
    )
    script = (
        "import json, sys\n"
        "from install import launcher\n"
        "a = launcher.parse_args(json.loads(sys.argv[1]))\n"
        "print(json.dumps({k: v for k, v in vars(a).items()}, default=str))\n"
    )
    result = subprocess.run(
        [str(SPLASH_PYTHON), "-P", "-c", script, json.dumps(list(spec.argv[1:]))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(SPLASH_PKG), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["model"] == MLX and parsed["port"] == PORT and parsed["host"] == "127.0.0.1"
    assert parsed["no_webui"] and parsed["offline"] and parsed["language_only"]
    assert parsed["revision"] == "main"
    assert parsed["served_model_name"] == ["q", "-x"] and parsed["announce_served_name"]
    assert parsed["default_reasoning_effort"] == "low" and parsed["kv_format"] == "bf16"
    assert parsed["max_memory"] == 40 * 1024**3 and parsed["max_cache_disk"] == 32 * 1024**3
    assert parsed["persistent_cache"] and parsed["cache_dir"] == str(paths.cache_dir)
    assert parsed["max_context"] == 102400 and parsed["decode_share"] == 0.0
    assert parsed["max_request_size"] == 256 * 1024**2 and parsed["max_image_pixels"] == 65536
    assert parsed["request_timeout"] == 2.5 and parsed["queue_size"] == 8
    assert parsed["idle_release"] == float("inf")
    assert parsed["disable_ane"] and parsed["allow_idle_sleep"]
    assert parsed["api_key"] is None
