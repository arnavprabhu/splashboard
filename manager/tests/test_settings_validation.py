"""Settings defaults and validation (SPEC §8.2, §8.3, §15.1, Appendix A)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from splash_gui.settings.metadata import FIELDS, FIELDS_BY_KEY, SECTION_IDS
from splash_gui.settings.model import CLAUDE_DESKTOP_SLOTS, SettingsDocument
from splash_gui.settings.validation import ValidationContext, validate_document

MLX = "mlx-community/Qwen3.8-27B-4bit"
LEGACY = "incoai/Qwen3.8-27B-Splash"


def doc_with(path: str, value: Any, model: str | None = None) -> dict[str, Any]:
    data = SettingsDocument().to_json_dict()
    if model is None:
        node = data["global"]
    else:
        node = data["models"].setdefault(
            model, {"serve": {}, "sampling_defaults": {}, "profiles": {}}
        )
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return data


def errors(raw: Any, context: ValidationContext | None = None) -> list[tuple[str, str]]:
    result = validate_document(raw, context)
    return [(i.key, i.message) for i in result.errors]


def test_defaults_match_spec() -> None:
    g = SettingsDocument().global_
    assert g.server.host == "127.0.0.1" and g.server.port == 8000
    assert g.server.allowed_hosts == [] and g.server.allowed_origins == []
    assert g.security.api_key_required is False and g.security.admin_requires_key is False
    assert g.engine.path is None and g.engine.internal_port == "auto"
    assert g.engine.extra_flags == []
    # Appendix A defaults are Splash's own.
    s = g.serve
    assert (s.max_memory, s.max_context, s.kv_format) == ("auto", "auto", "int8")
    assert (s.max_cache_disk, s.persistent_cache) == ("0", False)
    assert (s.decode_share, s.max_request_size, s.max_image_pixels) == (0.5, "128M", 4194304)
    assert (s.request_timeout, s.queue_size, s.default_reasoning_effort) == (None, 32, None)
    assert g.hf.offline is False and g.hf.endpoint is None
    r = g.routing
    assert (r.auto_load, r.switch_when_busy, r.default_model) == (True, "reject", None)
    assert (r.unknown_model_fallback, r.load_timeout) == (False, 120)
    lc = g.lifecycle
    assert (lc.launch_at_login, lc.stop_on_quit, lc.auto_restart) == (True, True, True)
    assert (lc.idle_unload, lc.idle_unload_minutes) == (False, 30)
    assert not any(g.menubar.model_dump().values())
    assert all(g.notifications.model_dump().values())
    assert g.advanced.crash_trace is False and g.ui.theme == "light"
    assert g.integrations.claude_desktop.port == 18435
    assert list(g.integrations.claude_desktop.slots) == list(CLAUDE_DESKTOP_SLOTS)
    assert g.downloads.parallel == 1


def test_spec_example_document_is_valid() -> None:
    """SPEC §15.1's example (with its `serve.offline`) loads after normalization."""
    from splash_gui.settings.store import normalize

    example = {
        "version": 1,
        "global": {
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "allowed_hosts": [],
                "allowed_origins": [],
            },
            "security": {"api_key_required": False, "admin_requires_key": False},
            "engine": {"path": None, "internal_port": "auto", "extra_flags": []},
            "serve": {
                "max_memory": "auto",
                "max_context": "auto",
                "kv_format": "int8",
                "max_cache_disk": "32G",
                "persistent_cache": True,
                "decode_share": 0.5,
                "max_request_size": "128M",
                "max_image_pixels": 4194304,
                "request_timeout": None,
                "queue_size": 32,
                "offline": False,
                "default_reasoning_effort": None,
            },
            "storage": {"models_dir": "~/.splash/models", "cache_dir": "~/.splash/cache"},
            "hf": {"endpoint": None},
            "routing": {
                "auto_load": True,
                "switch_when_busy": "reject",
                "default_model": None,
                "unknown_model_fallback": False,
                "load_timeout": 120,
            },
            "lifecycle": {
                "launch_at_login": True,
                "stop_on_quit": True,
                "auto_restart": True,
                "idle_unload": False,
                "idle_unload_minutes": 30,
            },
            "menubar": {
                "show_tokps": False,
                "show_memory": False,
                "show_gpu": False,
                "show_switcher": False,
            },
            "notifications": {
                "download_done": True,
                "engine_failed": True,
                "memory_critical": True,
                "update_available": True,
                "disk_cache_errors": True,
            },
            "advanced": {"crash_trace": False},
            "ui": {"theme": "light"},
            "wizard": {"completed": True, "preset": "coding"},
        },
        "models": {
            MLX: {
                "serve": {
                    "max_context": "128K",
                    "served_model_names": ["qwen27"],
                    "announce_served_name": False,
                },
                "sampling_defaults": {"temperature": 0.6, "top_p": 0.95},
                "profiles": {
                    "no-think": {"reasoning_effort": "none"},
                    "deterministic": {"temperature": 0, "seed": 0},
                },
            }
        },
    }
    result = validate_document(normalize(copy.deepcopy(example)))
    assert result.ok, result.errors
    assert result.document is not None
    assert result.document.global_.hf.offline is False


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("serve.max_context", "300K", "must be 'auto' or a token count up to 256K, such as 100K"),
        ("serve.max_context", "0", "must be 'auto' or a token count up to 256K, such as 100K"),
        ("serve.max_memory", "lots", "must be 'auto' or a positive byte count such as 32G"),
        ("serve.max_cache_disk", "auto", "use 0 to disable, or a size such as 5G"),
        ("serve.max_request_size", "0", "must be a positive byte count such as 128M"),
        ("serve.decode_share", -0.5, "must be a nonnegative number such as 0.5"),
        ("serve.queue_size", 0, "must be a positive number of requests such as 32"),
        ("serve.request_timeout", 0, "must be a positive number of seconds such as 3600"),
        ("serve.max_image_pixels", 4194305, "must be between 65536 and 4194304 pixels"),
        ("server.allowed_origins", ["tauri://*"], "tauri://* is not an origin"),
        ("server.port", 70000, "port must be between 1 and 65535"),
        ("lifecycle.idle_unload_minutes", 4, "Input should be greater than or equal to 5"),
        ("lifecycle.idle_unload_minutes", 241, "Input should be less than or equal to 240"),
        ("downloads.parallel", 4, "Input should be less than or equal to 3"),
        ("routing.default_model", "qwen", "full Hugging Face repository ID"),
        ("hf.endpoint", "ftp://mirror", "must be an http(s) URL"),
        ("engine.extra_flags", [{"flag": "-x"}], "must be a long option"),
    ],
)
def test_field_errors(path: str, value: Any, message: str) -> None:
    found = errors(doc_with(path, value))
    assert any(message in m for _, m in found), found


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("serve.max_context", "100K"),
        ("serve.max_context", 131072),
        ("serve.max_memory", "28G"),
        ("serve.max_cache_disk", "5G"),
        ("serve.decode_share", 0),
        ("serve.request_timeout", 3600),
        ("serve.kv_format", "bf16"),
        ("server.allowed_origins", ["*", "tauri://localhost"]),
        ("server.allowed_hosts", ["mymac.local"]),
        ("server.host", "0.0.0.0"),  # noqa: S104 (valid once a key exists, see below)
    ],
)
def test_field_accepts(path: str, value: Any) -> None:
    raw = doc_with(path, value)
    if path == "server.host":
        raw["global"]["security"]["api_key_required"] = True
    assert errors(raw) == []


def test_numbers_are_stored_as_text_for_sizes() -> None:
    result = validate_document(doc_with("serve.max_context", 131072))
    assert result.document is not None
    assert result.document.global_.serve.max_context == "131072"


def test_persistent_cache_requires_disk_tier() -> None:
    raw = doc_with("serve.persistent_cache", True)
    assert ("serve.persistent_cache", "--persistent-cache needs --max-cache-disk") in errors(raw)
    raw["global"]["serve"]["max_cache_disk"] = "32G"
    assert errors(raw) == []


def test_small_disk_tier_warns() -> None:
    result = validate_document(doc_with("serve.max_cache_disk", "100M"))
    assert result.ok
    assert any(w.key == "serve.max_cache_disk" for w in result.warnings)


def test_lan_bind_requires_key() -> None:
    raw = doc_with("server.host", "0.0.0.0")  # noqa: S104
    assert any(code == "security.api_key_required" for code, _ in errors(raw))
    raw["global"]["security"]["api_key_required"] = True
    assert errors(raw, ValidationContext(api_key_present=False)) == [
        ("security.api_key_required", "Generate an API key before binding to the local network")
    ]
    assert errors(raw, ValidationContext(api_key_present=True)) == []


def test_admin_protection_requires_key() -> None:
    raw = doc_with("security.admin_requires_key", True)
    assert errors(raw, ValidationContext(api_key_present=False))
    assert errors(raw, ValidationContext(api_key_present=True)) == []


def test_wildcard_origin_without_key_warns() -> None:
    result = validate_document(doc_with("server.allowed_origins", ["*"]))
    assert result.ok and result.warnings[0].code == "open_origin"


def test_port_conflicts() -> None:
    assert errors(doc_with("engine.internal_port", 8000))
    assert errors(doc_with("integrations.claude_desktop.port", 8000))


def test_extra_flags_cannot_repeat_managed_flags() -> None:
    raw = doc_with("engine.extra_flags", [{"flag": "--max-context", "value": "1K"}])
    assert errors(raw) == [
        ("engine.extra_flags.0.flag", "--max-context is set through its own setting")
    ]
    assert errors(doc_with("engine.extra_flags", [{"flag": "--future-thing", "value": "1"}])) == []


@pytest.mark.parametrize(
    ("flag", "managed"),
    [
        ("--hos", "--host"),
        ("--api-k", "--api-key"),
        ("--cache-d", "--cache-dir"),
        ("--h", "--help"),
    ],
)
def test_extra_flags_cannot_abbreviate_managed_flags(flag: str, managed: str) -> None:
    # Splash's argparse keeps allow_abbrev=True: `--hos 0.0.0.0` would rebind the engine.
    raw = doc_with("engine.extra_flags", [{"flag": flag, "value": "0.0.0.0"}])  # noqa: S104
    assert errors(raw) == [
        (
            "engine.extra_flags.0.flag",
            f"{flag} abbreviates {managed}, which is set through its own setting",
        )
    ]


def test_announce_requires_alias() -> None:
    raw = doc_with("serve", {"announce_served_name": True}, model=MLX)
    assert errors(raw) == [
        ("serve.announce_served_name", "--announce-served-name needs --served-model-name")
    ]
    raw["models"][MLX]["serve"]["served_model_names"] = ["qwen27"]
    assert errors(raw) == []


@pytest.mark.parametrize("alias", ["", "a b", "a%b", "x/..", "tab\tx"])
def test_alias_rules(alias: str) -> None:
    raw = doc_with("serve", {"served_model_names": [alias]}, model=MLX)
    assert any("model alias must be" in m for _, m in errors(raw))


@pytest.mark.parametrize(
    ("field", "value"),
    [("revision", "main"), ("language_only", True), ("draft_model", "incoai/Qwen3.8-27B-DFlash2")],
)
def test_legacy_packages_refuse_source_options(field: str, value: Any) -> None:
    raw = doc_with("serve", {field: value}, model=LEGACY)
    assert errors(raw) == [(f"serve.{field}", "Not available for legacy Splash packages")]


def test_legacy_variant_rejected() -> None:
    raw = doc_with("serve", {}, model=LEGACY + ":Q4")
    assert any("no variants" in m for _, m in errors(raw))


def test_invalid_model_key() -> None:
    raw = SettingsDocument().to_json_dict()
    raw["models"]["not-a-repo"] = {}
    assert any("full Hugging Face repository ID" in m for _, m in errors(raw))


def test_unknown_keys_are_rejected() -> None:
    raw = doc_with("serve.max_contxt", "1K")
    assert any(code.endswith("max_contxt") for code, _ in errors(raw))


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({"temperature": 2.5}, "temperature must be a number in [0, 2]"),
        ({"top_p": 0}, "top_p must be a number in (0, 1]"),
        ({"top_k": -2}, "top_k must be 0 or -1"),
        ({"min_p": 1.5}, "min_p must be a number in [0, 1]"),
        ({"presence_penalty": 3}, "presence_penalty must be a number in [-2, 2]"),
        ({"frequency_penalty": -3}, "frequency_penalty must be a number in [-2, 2]"),
        # Splash refuses booleans and strings where pydantic would coerce them.
        ({"temperature": True}, "temperature must be a number in [0, 2]"),
        ({"top_p": "0.9"}, "top_p must be a number in (0, 1]"),
        ({"top_k": True}, "top_k must be 0 or -1"),
        ({"top_k": "20"}, "top_k must be 0 or -1"),
        ({"top_k": 20.0}, "top_k must be 0 or -1"),
        ({"seed": False}, "seed must be an unsigned 64-bit integer"),
        ({"ignore_eos": 1}, "ignore_eos must be a boolean"),
        ({"temperature": float("inf")}, "temperature must be a number in [0, 2]"),
        ({"repetition_penalty": 0}, "repetition_penalty must be a positive number"),
        ({"seed": -1}, "seed must be an unsigned 64-bit integer"),
        ({"stop": ["a", "b", "c", "d", "e"]}, "stop must be a string or up to four strings"),
        ({"priority": "urgent"}, "Input should be 'foreground', 'normal' or 'background'"),
        ({"reasoning_effort": "huge"}, "Input should be"),
        ({"timeout": 0}, "timeout must be positive"),
        (
            {"thinking": {"type": "disabled", "display": "omitted"}},
            "thinking.display requires enabled or adaptive thinking",
        ),
    ],
)
def test_profile_ranges(overlay: dict[str, Any], message: str) -> None:
    raw = doc_with("profiles", {"custom": overlay}, model=MLX)
    assert any(message in m for _, m in errors(raw)), errors(raw)


def test_profile_errors_point_at_the_field() -> None:
    raw = doc_with("profiles", {"custom": {"top_k": True}}, model=MLX)
    assert ("models", MLX, "profiles", "custom", "top_k") in [
        tuple(i.path) for i in validate_document(raw).errors
    ]


def test_profile_ranges_accept_splash_edges() -> None:
    overlay = {"temperature": 0, "top_p": 1, "top_k": -1, "min_p": 0, "seed": 2**64 - 1}
    assert errors(doc_with("profiles", {"edge": overlay}, model=MLX)) == []


def test_profile_names_and_default_profile() -> None:
    assert errors(doc_with("profiles", {"Bad Name": {}}, model=MLX))
    assert errors(doc_with("profiles", {"default": None}, model=MLX))
    assert errors(doc_with("profiles", {"no-think": None}, model=MLX)) == []


def test_builtin_profiles_validate() -> None:
    from splash_gui.settings.effective import BUILTIN_PROFILES

    raw = doc_with("profiles", dict(BUILTIN_PROFILES), model=MLX)
    assert errors(raw) == []


def test_metadata_covers_every_settings_field() -> None:
    """Every leaf of the global section has metadata, and every key resolves."""
    data = SettingsDocument().to_json_dict()["global"]
    leaves: set[str] = set()
    for section, values in data.items():
        for name, value in values.items():
            if section == "integrations":
                leaves.update(f"integrations.{name}.{k}" for k in value)
            else:
                leaves.add(f"{section}.{name}")
    model_only = {f.key for f in FIELDS if f.scope == "M"}
    keychain = {f.key for f in FIELDS if f.storage == "keychain"}
    assert leaves == set(FIELDS_BY_KEY) - model_only - keychain
    assert all(f.section in SECTION_IDS for f in FIELDS)


def test_every_appendix_a_flag_has_metadata() -> None:
    flags = {f.flag for f in FIELDS if f.flag}
    for flag in (
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--host",
        "--port",
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
        "--allowed-host",
        "--allowed-origin",
        "--max-request-size",
        "--max-image-pixels",
        "--request-timeout",
        "--queue-size",
        "--api-key",
    ):
        assert flag in flags, flag
    envs = {f.env for f in FIELDS if f.env}
    assert {"HF_TOKEN", "HF_HUB_CACHE", "HF_ENDPOINT", "SPLASH_CRASH_TRACE"} <= envs
    restart = {f.key for f in FIELDS if f.flag and f.key.startswith("serve.")}
    assert all(FIELDS_BY_KEY[k].applies == "restart" for k in restart)
