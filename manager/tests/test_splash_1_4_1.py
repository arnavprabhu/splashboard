"""Splash 1.4.1 in the manager: Flint and Ornith-1.5-122B in the catalog, the
compatibility check and the inventory, Flint's context bound and reasoning, the
new `/status` fields, and the start failures that now report their reason."""

from __future__ import annotations

from typing import Any

import pytest

from splash_gui.engine import status as st
from splash_gui.engine.startup import ErrorEvent, NoticeEvent, StartupParser, classify_error
from splash_gui.metrics.live import MetricsHub
from splash_gui.models import catalog as cat
from splash_gui.models import compat
from splash_gui.schemas import InstalledModel
from splash_gui.settings import presets
from splash_gui.settings.validation import validate_document

from .test_compatibility import hub_harness, inspect  # noqa: F401  (fixture)

FLINT = "unsloth/flint-GGUF:UD-Q4_K_XL"
FLINT_REPO = "unsloth/flint-GGUF"
ORNITH_REPO = "unsloth/Ornith-1.5-122B-GGUF"
GIB = 1024**3


def feed_all(text: str) -> list[object]:
    parser = StartupParser()
    events: list[object] = []
    for line in text.splitlines():
        events += parser.feed(line)
    return events + parser.finish()


# --- families and the catalog -------------------------------------------------


def test_the_new_families_have_their_drafts_labels_and_guesses() -> None:
    assert cat.DRAFT_REPOS["Flint"] == "incoai/Flint-DFlash2"
    assert cat.DRAFT_REPOS["Qwen3.5-122B-A10B"] == "incoai/Ornith-1.5-122B-DFlash2"
    assert cat.family_guess("unsloth/flint-GGUF") == "Flint"
    assert cat.family_guess("ornith-ai/Ornith-1.5-122B-MLX-4bit") == "Qwen3.5-122B-A10B"
    # Ornith-1.5-35B-A3B is the 35B family, as Splash finds it by its sizes.
    assert cat.family_guess("ornith-ai/Ornith-1.5-35B-A3B-GGUF") == "Qwen3.6-35B-A3B"
    assert cat.FAMILY_LABELS["Qwen3.5-122B-A10B"] == "Ornith-1.5-122B MoE"


def test_flint_kv_counts_its_full_layers_and_the_windows_it_keeps() -> None:
    # 14 full layers of 4 KV heads of 128 in INT8 (128 values and a 4-byte scale for
    # K and V): 14,784 bytes a token, plus the 35 windowed layers' Full pages.
    per_token = 14 * 2 * 4 * (128 + 4)
    windowed = 35 * 2 * 4 * (128 + 4) * (16 + 64 + 1) * 32
    assert cat.kv_bytes("Flint", 100_000) == per_token * 100_000 + windowed
    # Short contexts keep every windowed layer: no Slim pages yet.
    assert cat.kv_bytes("Flint", 1000) == 49 * 2 * 4 * (128 + 4) * 1000
    assert cat.kv_bytes("Qwen3.5-122B-A10B", 1000) == 12 * 2 * 2 * (256 + 4) * 1000


def test_flint_is_text_only_and_streams_on_a_24_gb_mac() -> None:
    files = {"flint-UD-Q4_K_XL.gguf": 25_050_000_000, "mmproj-BF16.gguf": 900_000_000}
    facts = cat.entry_facts(FLINT_REPO, "gguf", _Info(files), 64 * GIB, family="Flint")
    assert facts["vision"] is False
    assert facts["variants"][0]["files"] == ["flint-UD-Q4_K_XL.gguf"]
    assert facts["fit"] == "fits"
    assert cat.fit_of(25_050_000_000, 24 * GIB, vision=False, family="Flint") == "streams"


def test_ornith_streams_its_experts_on_a_64_gb_mac() -> None:
    assert cat.fit_of(79_710_000_000, 64 * GIB, vision=True, family="Qwen3.5-122B-A10B") == (
        "streams"
    )
    assert cat.fit_of(44_530_000_000, 64 * GIB, vision=True, family="Qwen3.5-122B-A10B") == ("fits")


class _Info:
    def __init__(self, files: dict[str, int]) -> None:
        self.files = files
        self.license = "apache-2.0"
        self.last_modified = None


def test_the_catalog_lists_flint_ornith_and_the_pruned_flash_next(hub_harness) -> None:  # noqa: F811
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * GIB
    catalog = harness.client.get("/api/admin/catalog").json()
    families = {f["family"]: f for f in catalog["families"]}
    flint = families["Flint"]
    assert flint["label"] == "Flint MoE" and [g["format"] for g in flint["groups"]] == ["gguf"]
    entry = flint["groups"][0]["entries"][0]
    assert entry["id"] == FLINT_REPO and entry["vision"] is False
    assert "between the 35B-A3B and the 27B" in entry["notes"]
    ornith = families["Qwen3.5-122B-A10B"]
    assert {g["format"] for g in ornith["groups"]} == {"gguf", "mlx"}
    rows = {e["id"]: e for g in ornith["groups"] for e in g["entries"]}
    assert set(rows) == {ORNITH_REPO, "ornith-ai/Ornith-1.5-122B-MLX-4bit"}
    flash = {e["id"] for g in families["Qwen3.8-Flash-Next"]["groups"] for e in g["entries"]}
    assert "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF" in flash


def test_flint_checks_compatible_text_only(hub_harness) -> None:  # noqa: F811
    result = inspect(hub_harness(), FLINT)
    assert result["compatible"] is True and result["badge"] == "text_only"
    assert result["family"] == "Flint" and result["draft"] == "incoai/Flint-DFlash2"
    assert result["vision"]["available"] is False


def test_flint_s_own_safetensors_get_the_gguf_only_line(hub_harness) -> None:  # noqa: F811
    result = inspect(hub_harness(), "RecursiveSI/flint")
    assert result["compatible"] is False
    assert result["reason"] == compat.GGUF_ONLY.format(family="Flint")
    assert result["reason_detail"].endswith("Flint loads from a GGUF only")


def test_ornith_checks_as_the_122b_family(hub_harness) -> None:  # noqa: F811
    result = inspect(hub_harness(), f"{ORNITH_REPO}:UD-Q4_K_XL")
    assert result["compatible"] is True and result["family"] == "Qwen3.5-122B-A10B"
    assert result["draft"] == "incoai/Ornith-1.5-122B-DFlash2"


def test_an_installed_flint_is_listed_text_only(harness_factory) -> None:
    harness = harness_factory(installed=(FLINT,))
    models = {m["id"]: m for m in harness.client.get("/api/admin/models").json()["models"]}
    flint = models[FLINT]
    assert flint["family"] == "Flint"
    assert flint["vision"] is False and flint["language_only"] is False


def test_a_family_this_release_does_not_know_does_not_break_the_inventory() -> None:
    model = InstalledModel.model_validate(
        {
            "id": "a/b",
            "repo_id": "a/b",
            "family": "Qwen9-Future",
            "format": "gguf",
            "size_bytes": 1,
            "unique_bytes": 1,
            "status": "ready",
        }
    )
    assert model.family is None


# --- startup ------------------------------------------------------------------


def test_flint_past_its_window_is_a_settings_error_with_its_fix() -> None:
    events = feed_all(
        "error: MAX_CONTEXT: Flint serves at most 262144 tokens of context\n"
        "usage: splash MODEL_ROOT MAX_CONTEXT MAX_MEMORY_BYTES MAX_CACHE_DISK_BYTES\n"
        "10:24:11 Error · native protocol reached EOF"
    )
    errors = [e.error for e in events if isinstance(e, ErrorEvent)]
    first = errors[0]
    assert first.code == "invalid_setting"
    assert "Flint serves at most 262,144 tokens" in first.message
    fix = next(s for s in first.suggestions if s.action == "lower_max_context")
    assert fix.patch == {"serve.max_context": "262144"}


def test_a_start_failure_s_reason_from_the_server_is_a_settings_error_too() -> None:
    # 1.4.1: the server ends the start with the engine's reason.
    error = classify_error(
        "Error · --expert-memory of 512 MiB is less than its smallest store, 1024 MiB"
    )
    assert error is not None and error.code == "invalid_setting"
    assert error.suggestions[0].patch == {"serve.expert_memory": "auto"}


def test_the_budget_refusal_stays_first_when_the_server_repeats_its_reason() -> None:
    events = feed_all(
        "error: runtime bootstrap failed [resource_assembly]: hard budget cannot fit one "
        "lane's state and the KV runway\n"
        "engine memory budget validation failed [kv_pool_does_not_fit]: hard budget\n"
        "hard budget: 7516192768 bytes (7168.00 MiB)\n"
        "\n"
        "10:24:11 Error · hard budget cannot fit one lane's state and the KV runway"
    )
    errors = [e.error for e in events if isinstance(e, ErrorEvent)]
    assert errors[0].kind == "budget_refusal"
    assert [e.kind for e in errors[1:]] == ["other"]


def test_an_assembly_from_an_earlier_release_is_not_prepared() -> None:
    error = classify_error(
        "Error · unsloth/flint-GGUF:UD-Q4_K_XL is not prepared as a model of a family this "
        "release serves: prepare it again"
    )
    assert error is not None and error.code == "model_not_prepared"


def test_missing_vision_shards_suggest_language_only() -> None:
    error = classify_error(
        "error: cannot install ornith-ai/Ornith-1.5-122B-MLX-4bit: checkpoint has missing or "
        "unsupported shards: model-00009-of-00009.safetensors; use --language-only to serve "
        "text only"
    )
    assert error is not None and error.kind == "incompatible"
    assert any(s.action == "language_only" for s in error.suggestions)


def test_waiting_for_memory_is_a_notice() -> None:
    events = feed_all(
        "10:00:00 Waiting for sufficient available memory to start; the macOS reserve remains "
        "protected..."
    )
    assert [e.kind for e in events if isinstance(e, NoticeEvent)] == ["memory_wait"]


def test_flint_starts_from_the_fake_and_reports_no_reasoning_switch(harness_factory) -> None:
    harness = harness_factory(installed=(FLINT,))
    harness.load(FLINT)
    view = harness.wait_state("ready")
    assert view["reasoning_switch"] is False and view["vision"] is False


def test_flint_past_its_window_fails_with_the_fix(harness_factory) -> None:
    harness = harness_factory(installed=(FLINT,))
    # The settings refuse it as Flint's own override; a global one only warns.
    harness.patch_settings({"global": {"serve": {"max_context": "300K"}}})
    harness.load(FLINT)
    view = harness.wait_state("failed")
    assert view["error"]["code"] == "invalid_setting"
    assert view["error"]["suggestions"][0]["patch"] == {"serve.max_context": "262144"}


# --- status -------------------------------------------------------------------


def test_reasoning_switch_and_kv_window_come_from_status(app: Any) -> None:
    from .test_metrics_live import status as base_status

    status = base_status(
        reasoning_switch=False,
        kv__window={
            "trim": True,
            "full_pages": 20,
            "slim_pages": 10,
            "slim_bytes": 4_730_880,
            "trimmed_pages": 10,
        },
    )
    assert st.reasoning_switch(status) is False
    assert st.reasoning_switch({}) is None
    hub = app.state.manager.metrics
    assert isinstance(hub, MetricsHub)
    kv = hub.derive(status, 100.0).kv
    assert (kv.window_trim, kv.window_full_pages, kv.window_slim_pages) == (True, 20, 10)
    assert kv.window_slim_bytes == 4_730_880 and kv.window_trimmed_pages == 10
    plain = hub.derive(base_status(), 101.0).kv
    assert plain.window_slim_pages is None and plain.window_trim is None


# --- settings and presets -----------------------------------------------------


def test_a_flint_context_past_its_window_is_refused_and_reasoning_settings_warn() -> None:
    result = validate_document(
        {
            "version": 4,
            "models": {FLINT: {"serve": {"max_context": "300K", "thinking_token_budget": 2048}}},
        }
    )
    assert [(i.key, i.code) for i in result.errors] == [("serve.max_context", "family_context")]
    assert ("serve.thinking_token_budget", "ignored") in [(i.key, i.code) for i in result.warnings]


def test_a_global_context_past_flint_s_window_only_warns_on_flint() -> None:
    result = validate_document(
        {"version": 4, "global": {"serve": {"max_context": "512K"}}, "models": {FLINT: {}}}
    )
    assert not result.errors
    assert ("serve.max_context", "family_context") in [(i.key, i.code) for i in result.warnings]


@pytest.mark.parametrize("preset", ["coding", "chat", "speed"])
def test_presets_keep_their_picks_and_offer_flint_beside_them(preset: str) -> None:
    rec = presets.recommend(preset, 64 * GIB)  # type: ignore[arg-type]
    assert rec.primary is not None and "flint" not in rec.primary.model
    assert presets.FLINT_PICK in rec.alternatives
    assert "depends a lot on your use" in presets.FLINT_PICK.note
    assert presets.FLINT_PICK not in presets.recommend(preset, 24 * GIB).alternatives  # type: ignore[arg-type]


@pytest.mark.parametrize("language_only", [False, True])
def test_flint_downloads_with_or_without_language_only(hub_harness, language_only) -> None:  # noqa: F811
    """A text-only family has no vision to refuse or skip: the download runs either way
    and installs one selection, never a language-only one."""
    from .test_downloads import queue, wait_for

    harness = hub_harness()
    started = queue(harness, FLINT, language_only=language_only)
    assert started["language_only"] is False
    done = wait_for(harness, started["id"], "done", "failed")
    assert done["state"] == "done", done
    models = {m["id"]: m for m in harness.client.get("/api/admin/models").json()["models"]}
    assert models[FLINT]["family"] == "Flint" and models[FLINT]["language_only"] is False
