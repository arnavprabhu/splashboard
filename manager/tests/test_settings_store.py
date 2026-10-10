"""Effective settings with provenance, persistence, migrations."""

from __future__ import annotations

import json
from typing import Any

import pytest

from splash_gui.paths import Paths
from splash_gui.settings.effective import (
    effective_profiles,
    effective_serve,
    effective_values,
    profile_reasoning_effort,
    sampling_defaults,
)
from splash_gui.settings.model import SETTINGS_VERSION, SettingsDocument
from splash_gui.settings.presets import GIB, apply_preset, recommend
from splash_gui.settings.store import (
    SettingsReadOnlyError,
    SettingsStore,
    diff,
    migrate,
    repair,
)
from splash_gui.settings.validation import ValidationContext, validate_document

from .conftest import mode

MLX = "mlx-community/Qwen3.8-27B-4bit"


def doc(raw: dict[str, Any]) -> SettingsDocument:
    base = SettingsDocument().to_json_dict()
    for section, values in raw.get("global", {}).items():
        base["global"][section].update(values)
    base["models"] = raw.get("models", {})
    return SettingsDocument.model_validate(base)


# Provenance --------------------------------------------------------------------


def test_provenance_default_global_model() -> None:
    d = doc(
        {
            "global": {"serve": {"max_context": "64K", "kv_format": "int8"}},
            "models": {MLX: {"serve": {"kv_format": "bf16"}}},
        }
    )
    values = effective_values(d, MLX)
    assert (values["serve.max_context"].value, values["serve.max_context"].source) == (
        "64K",
        "global",
    )
    assert (values["serve.kv_format"].value, values["serve.kv_format"].source) == ("bf16", "model")
    assert values["serve.kv_format"].global_value == "int8"
    assert values["serve.queue_size"].source == "default"
    assert values["serve.revision"].source == "default"
    # Without a model, there is no model provenance.
    assert effective_values(d)["serve.kv_format"].source == "default"


def test_equivalent_sizes_count_as_default() -> None:
    d = doc({"global": {"serve": {"max_request_size": "128MiB"}}})
    assert effective_values(d)["serve.max_request_size"].source == "default"


def test_explicit_null_reasoning_effort_overrides_global() -> None:
    d = doc(
        {
            "global": {"serve": {"default_reasoning_effort": "high"}},
            "models": {MLX: {"serve": {"default_reasoning_effort": None}}},
        }
    )
    value = effective_values(d, MLX)["serve.default_reasoning_effort"]
    assert (value.value, value.source) == (None, "model")
    other = effective_values(d, "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M")
    assert other["serve.default_reasoning_effort"].value == "high"


def test_reset_to_global_by_removing_the_key() -> None:
    d = doc({"models": {MLX: {"serve": {"max_context": "32K", "language_only": None}}}})
    entry = d.to_json_dict()["models"][MLX]
    assert entry["serve"] == {"max_context": "32K"}  # null means inherit, so not stored
    entry["serve"].pop("max_context")
    d2 = doc({"models": {MLX: entry}})
    assert effective_values(d2, MLX)["serve.max_context"].source == "default"


def test_every_dump_keeps_per_model_overrides_sparse() -> None:
    """Internal read-modify-write (storage move, reset, CLI `config set`) dumps the
    document; per-model defaults must not come back as explicit overrides."""
    d = doc({"models": {MLX: {"serve": {"max_context": "64K"}, "sampling_defaults": {}}}})
    dumped = d.model_dump(mode="json", by_alias=True)
    assert dumped == d.to_json_dict()
    assert dumped["models"][MLX]["serve"] == {"max_context": "64K"}
    again = SettingsDocument.model_validate(dumped)
    assert again.models[MLX].serve.model_fields_set == {"max_context"}


def test_effective_serve() -> None:
    d = doc(
        {
            "global": {
                "serve": {"max_cache_disk": "32G", "persistent_cache": True},
                "hf": {"offline": True},
            },
            "models": {MLX: {"serve": {"served_model_names": ["qwen27"], "max_context": "128K"}}},
        }
    )
    serve = effective_serve(d, MLX)
    assert serve.offline and serve.persistent_cache and serve.max_cache_disk == "32G"
    assert serve.served_model_names == ("qwen27",) and serve.max_context == "128K"


def test_profile_reasoning_effort_reads_the_request_overlay() -> None:
    """What `splash launch` hands the client for `<id>:<profile>`."""
    d = doc(
        {
            "models": {
                MLX: {
                    "profiles": {"quick": {"reasoning_effort": "low"}, "cold": {"temperature": 0}},
                    "sampling_defaults": {"reasoning_effort": "high"},
                }
            }
        }
    )
    assert profile_reasoning_effort(d, f"{MLX}:no-think") == "none"
    assert profile_reasoning_effort(d, f"{MLX}:quick") == "low"
    assert profile_reasoning_effort(d, f"{MLX}:cold") == "high", "the model's default"
    assert profile_reasoning_effort(d, MLX) is None, "no profile, nothing to pass"
    assert profile_reasoning_effort(d, f"{MLX}:nope") is None
    assert profile_reasoning_effort(d, "alias:no-think", active=MLX) == "none"
    assert profile_reasoning_effort(d, None) is None


def test_builtin_profiles_and_overrides() -> None:
    d = doc(
        {
            "models": {
                MLX: {
                    "profiles": {
                        "no-think": None,
                        "deterministic": {"temperature": 0, "seed": 42},
                        "fast": {"max_tokens": 256},
                    },
                    "sampling_defaults": {"temperature": 0.6},
                }
            }
        }
    )
    profiles = effective_profiles(d, MLX)
    assert "no-think" not in profiles
    assert profiles["default"].overlay == {} and profiles["default"].builtin
    assert profiles["deterministic"].overlay == {"temperature": 0, "seed": 42}
    assert profiles["deterministic"].modified
    assert profiles["qwen-nonthinking"].overlay == {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
        "reasoning_effort": "none",
    }
    assert profiles["fast"].builtin is False
    assert sampling_defaults(d, MLX) == {"temperature": 0.6}
    untouched = effective_profiles(d, "other/model")
    assert set(untouched) == {"default", "no-think", "deterministic", "qwen-nonthinking"}


# Persistence --------------------------------------------------------------------


def test_load_missing_file_gives_defaults(paths: Paths) -> None:
    store = SettingsStore(paths)
    assert store.load() == SettingsDocument()
    assert not paths.settings_file.exists()


def test_save_is_atomic_and_private(paths: Paths) -> None:
    store = SettingsStore(paths)
    raw = SettingsDocument().to_json_dict()
    raw["global"]["serve"]["max_context"] = "64K"
    result, changes = store.save(raw)
    assert result.ok
    assert mode(paths.settings_file) == 0o600
    on_disk = json.loads(paths.settings_file.read_text())
    assert on_disk["version"] == SETTINGS_VERSION
    assert on_disk["global"]["serve"]["max_context"] == "64K"
    assert [c.key for c in changes] == ["serve.max_context"]
    assert changes[0].applies == "restart"
    assert [p.name for p in paths.base.iterdir() if p.name.startswith(".settings")] == []
    assert SettingsStore(paths).load().global_.serve.max_context == "64K"


def test_invalid_save_writes_nothing(paths: Paths) -> None:
    store = SettingsStore(paths)
    raw = SettingsDocument().to_json_dict()
    raw["global"]["serve"]["persistent_cache"] = True
    result, changes = store.save(raw)
    assert not result.ok and changes == []
    assert not paths.settings_file.exists()


def test_loose_file_mode_is_tightened(paths: Paths) -> None:
    paths.settings_file.write_text(json.dumps({"version": 1}))
    paths.settings_file.chmod(0o644)
    SettingsStore(paths).load()
    assert mode(paths.settings_file) == 0o600


def test_migrate_from_unversioned() -> None:
    data, newer = migrate({"global": {"serve": {"offline": True}}})
    assert not newer
    assert data["version"] == 2
    assert data["global"]["hf"]["offline"] is True
    assert "offline" not in data["global"]["serve"]


def test_unversioned_file_is_rewritten(paths: Paths) -> None:
    paths.settings_file.write_text(json.dumps({"global": {"ui": {"theme": "dark"}}}))
    doc_ = SettingsStore(paths).load()
    assert doc_.global_.ui.theme == "dark"
    assert json.loads(paths.settings_file.read_text())["version"] == 2


def test_v1_turns_admin_sign_in_on_once(paths: Paths) -> None:
    """Version 1 stored the whole global section, so `false` may only be the old
    default. It becomes `true` once; the user's later choice is kept."""
    v1 = {
        "version": 1,
        "global": {"security": {"api_key_required": False, "admin_requires_key": False}},
    }
    paths.settings_file.write_text(json.dumps(v1))
    store = SettingsStore(paths)
    assert store.load().global_.security.admin_requires_key is True
    on_disk = json.loads(paths.settings_file.read_text())
    assert on_disk["version"] == 2 and on_disk["global"]["security"]["admin_requires_key"] is True
    raw = store.current.to_json_dict()
    raw["global"]["security"]["admin_requires_key"] = False
    result, _ = store.save(raw, ValidationContext(api_key_present=True))
    assert result.ok
    assert SettingsStore(paths).load().global_.security.admin_requires_key is False


def test_repair_drops_only_bad_values(paths: Paths) -> None:
    paths.settings_file.write_text(
        json.dumps(
            {
                "version": 1,
                "global": {"serve": {"max_context": "999K", "queue_size": 8}, "bogus": {"x": 1}},
            }
        )
    )
    store = SettingsStore(paths)
    loaded = store.load()
    assert loaded.global_.serve.max_context == "auto"
    assert loaded.global_.serve.queue_size == 8
    assert any("max_context" in w for w in store.load_warnings)
    assert any("bogus" in w for w in store.load_warnings)


def test_repair_function() -> None:
    fixed, dropped = repair({"version": 1, "models": {"bad id": {}}})
    assert fixed.models == {} and dropped


def test_unreadable_file_is_moved_aside(paths: Paths) -> None:
    paths.settings_file.write_text("{not json")
    store = SettingsStore(paths)
    assert store.load() == SettingsDocument()
    assert any(p.name.startswith("settings.json.invalid-") for p in paths.base.iterdir())


def test_newer_version_is_read_only(paths: Paths) -> None:
    paths.settings_file.write_text(json.dumps({"version": 99, "global": {}}))
    store = SettingsStore(paths)
    store.load()
    assert store.read_only
    with pytest.raises(SettingsReadOnlyError):
        store.save(SettingsDocument().to_json_dict())
    assert json.loads(paths.settings_file.read_text())["version"] == 99


def test_diff_reports_model_changes() -> None:
    old = doc({})
    new = doc(
        {
            "models": {MLX: {"serve": {"language_only": True}}},
            "global": {"routing": {"auto_load": False}},
        }
    )
    changes = {(c.key, c.model, c.applies) for c in diff(old, new)}
    assert ("serve.language_only", MLX, "restart") in changes
    assert ("routing.auto_load", None, "immediate") in changes


def test_resolved_dirs_follow_settings(paths: Paths, tmp_path: Any) -> None:
    store = SettingsStore(paths)
    assert store.models_dir() == paths.models_dir
    assert store.tmp_dir() == paths.cache_dir / "tmp"
    raw = SettingsDocument().to_json_dict()
    raw["global"]["storage"]["cache_dir"] = str(tmp_path / "elsewhere")
    store.save(raw)
    assert store.tmp_dir() == tmp_path / "elsewhere" / "tmp"


# Presets ----------------------------------------------------------------


def test_coding_preset_is_valid_and_complete() -> None:
    raw = apply_preset(SettingsDocument(), "coding")
    result = validate_document(raw)
    assert result.ok and result.document is not None
    s = result.document.global_.serve
    assert (s.max_context, s.max_cache_disk, s.persistent_cache) == ("128K", "32G", True)
    assert result.document.global_.wizard.preset == "coding"


@pytest.mark.parametrize("preset", ["chat", "speed"])
def test_other_presets_valid(preset: Any) -> None:
    assert validate_document(apply_preset(SettingsDocument(), preset)).ok


@pytest.mark.parametrize(
    ("preset", "gb", "primary"),
    [
        ("coding", 64, "mlx-community/Qwen3.8-27B-4bit"),
        ("coding", 36, "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"),
        ("coding", 24, "unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS"),
        ("chat", 48, "mlx-community/Qwen3.6-35B-A3B-4bit"),
        ("chat", 24, "prism-ml/Ternary-Bonsai-2-27B-gguf:PQ2_0"),
        ("speed", 36, "mlx-community/Qwen3.6-35B-A3B-4bit"),
        ("speed", 32, "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL"),
    ],
)
def test_recommendations(preset: Any, gb: int, primary: str) -> None:
    rec = recommend(preset, gb * GIB)
    assert rec.primary is not None and rec.primary.model == primary


def test_small_mac_gets_no_recommendation() -> None:
    assert recommend("coding", 16 * GIB).primary is None


def test_coding_24gb_applies_language_only() -> None:
    raw = apply_preset(
        SettingsDocument(), "coding", "unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS", 24 * GIB
    )
    assert raw["models"]["unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS"]["serve"] == {"language_only": True}
    assert validate_document(raw).ok


# The coding preset's context (128K, or the largest that fits) --------------

MLX_27B = "mlx-community/Qwen3.8-27B-4bit"


def test_the_coding_context_is_128k_on_every_shipped_tier() -> None:
    from splash_gui.settings.presets import preset_settings

    for gb in (24, 36, 48, 64, 128):
        assert preset_settings("coding", gb * GIB)["serve.max_context"] == "128K", gb
    assert preset_settings("coding")["serve.max_context"] == "128K", "unknown memory keeps 128K"
    assert preset_settings("chat", 64 * GIB)["serve.max_context"] == "auto"
    raw = apply_preset(SettingsDocument(), "coding", None, 64 * GIB)
    assert raw["global"]["serve"]["max_context"] == "128K"


def test_the_kv_cache_the_context_takes_follows_the_engine_layout() -> None:
    """runtime/ops/PagedKv.hpp: one INT8 byte per value plus a float scale per token and
    head, for keys and values, on the full-attention layers only (every fourth layer)."""
    from splash_gui.models import catalog as cat

    assert cat.kv_bytes("Qwen3.8-27B", 128 * 1024) == 16 * 2 * 4 * (256 + 4) * 128 * 1024
    assert cat.kv_bytes("Qwen3.8-27B", 128 * 1024, bf16=True) == 8 * GIB
    assert cat.kv_bytes("Qwen3.6-35B-A3B", 128 * 1024) < cat.kv_bytes("Qwen3.8-27B", 128 * 1024)
    small = cat.memory_need(10 * GIB, vision=False, kv=cat.kv_bytes("Qwen3.8-27B", 1024))
    assert small == cat.memory_need(10 * GIB, vision=False), "a tiny context keeps the runway"


def test_the_largest_context_step_that_is_not_wont_fit() -> None:
    """On 29 GiB the 27B MLX pick's 128K estimate (27.7 GiB) is above `memsize − 3 GB`
    but its 64K estimate (25.6 GiB) is not, so the preset steps down to 64K."""
    from splash_gui.settings.presets import ModelPick, coding_context

    pick = ModelPick(MLX_27B, "test")
    assert coding_context(40 * GIB, pick) == "128K"
    assert coding_context(29 * GIB, pick) == "64K"
    assert coding_context(1 * GIB, pick) == "8K", "when no step fits, the smallest one"


def test_each_step_is_the_largest_that_fits() -> None:
    from splash_gui.models import catalog as cat
    from splash_gui.settings.presets import CODING_CONTEXTS, ModelPick, coding_context

    pick = ModelPick(MLX_27B, "test")
    # Below about 27 GiB no step fits, and the smallest one is the answer (tested above).
    for memory in (28 * GIB, 29 * GIB, 31 * GIB, 34 * GIB):
        label = coding_context(memory, pick)
        tokens = dict(CODING_CONTEXTS)[label]
        need = cat.memory_need(20 * GIB, vision=True, kv=cat.kv_bytes("Qwen3.8-27B", tokens))
        assert cat.fit_for(need, memory) != "wont_fit"
        larger = [t for _, t in CODING_CONTEXTS if t > tokens]
        for bigger in larger:
            wider = cat.memory_need(20 * GIB, vision=True, kv=cat.kv_bytes("Qwen3.8-27B", bigger))
            assert cat.fit_for(wider, memory) == "wont_fit", (memory, bigger)
