"""Installed models, the curated catalog and model actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from splash_gui.models import catalog as cat

from .fakeengine import MODEL, MODEL_27B, EngineHarness

GIB = 1024**3


@pytest.fixture
def hub_harness(harness_factory, fake_hub, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", fake_hub)

    def make(**kwargs: Any) -> EngineHarness:
        harness: EngineHarness = harness_factory(env={"HF_ENDPOINT": fake_hub}, **kwargs)
        harness.patch_settings({"global": {"hf": {"endpoint": fake_hub}}})
        harness.state.memory_bytes = lambda: 64 * GIB
        return harness

    return make


def entries(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry["repo_id"]: entry
        for family in payload["families"]
        for group in family["groups"]
        for entry in group["entries"]
    }


# --- Catalog ------------------------------------------------------------------


def test_catalog_rows_are_filled_from_the_hub(hub_harness) -> None:
    h = hub_harness(installed=(MODEL,))
    payload = h.client.get("/api/admin/catalog").json()
    rows = entries(payload)
    assert payload["refreshed_at"] and payload["memory_bytes"] == 64 * GIB
    mlx = rows["mlx-community/Qwen3.6-35B-A3B-4bit"]
    assert mlx["size_bytes"] and mlx["memory_need_bytes"] > mlx["size_bytes"]
    assert mlx["fit"] in ("fits", "tight", "wont_fit") and mlx["license"] == "apache-2.0"
    assert mlx["notes"] and mlx["perf_note"]
    gguf = rows["unsloth/Qwen3.6-35B-A3B-GGUF"]
    assert gguf["installed"] is True, "the installed variant marks its repository"
    names = [v["name"] for v in gguf["variants"]]
    assert "UD-Q4_K_M" in names
    assert gguf["vision"] is True, "a BF16 projector makes the row vision-capable"
    assert gguf["recommended_variant"] in names
    assert sum(v["recommended"] for v in gguf["variants"]) == 1
    # On 64 GB the coding pick is the 27B MLX checkpoint.
    assert rows["mlx-community/Qwen3.8-27B-4bit"]["recommended"] is True
    assert sum(r["recommended"] for r in rows.values()) == 1


def test_catalog_offline_keeps_the_rows_without_hub_data(hub_harness) -> None:
    h = hub_harness()
    h.patch_settings({"global": {"hf": {"offline": True}}})
    payload = h.client.get("/api/admin/catalog").json()
    assert payload["offline"] is True and payload["refreshed_at"] is None
    rows = entries(payload)
    assert len(rows) >= 5
    assert all(r["size_bytes"] is None for r in rows.values())


def test_catalog_lists_no_splash_packages(hub_harness) -> None:
    """Splash 1.3.1 no longer loads Splash packages: an official list an earlier Splash
    left behind is not merged in."""
    h = hub_harness()
    official = h.home / "fake-data" / "catalog" / "official-models.txt"
    official.parent.mkdir(parents=True, exist_ok=True)
    official.write_text("# refreshed\nincoai/Qwen3.8-27B-Splash\nincoai/Qwen3.9-27B-Splash\n")
    payload = h.client.get("/api/admin/catalog").json()
    rows = entries(payload)
    assert not [r for r in rows if r.endswith("-Splash")]
    assert {r["format"] for r in rows.values()} == {"mlx", "gguf"}
    groups = {f["family"]: [g["format"] for g in f["groups"]] for f in payload["families"]}
    # NVIDIA's NVFP4 checkpoint is a safetensors target ("mlx") in a group of its own;
    # Flash-Next loads from GGUFs only, so it has no MLX group.
    assert groups == {
        "Qwen3.8-27B": ["mlx", "nvfp4", "gguf"],
        "Qwen3.6-35B-A3B": ["mlx", "gguf"],
        "Qwen3.8-Flash-Next": ["gguf"],
    }


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Qwen3.8-27B-UD-Q4_K_M.gguf", "UD-Q4_K_M"),
        ("Qwen3.8-27B-UD-Q8_K_XL-00001-of-00002.gguf", "UD-Q8_K_XL"),
        ("mmproj-BF16.gguf", None),
        # A split GGUF in a folder named after its variant is that variant.
        ("BF16/Qwen3.8-27B-BF16-00001-of-00002.gguf", "BF16"),
        ("Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf", "Q2_0"),
        ("a/b/Qwen3.8-27B-Q4_0.gguf", None),
        ("mtp-Qwen3.8-Flash-Next-Q8_0.gguf", None),
        ("README.md", None),
    ],
)
def test_variant_names(filename: str, expected: str | None) -> None:
    assert cat.variant_name("unsloth/Qwen3.8-27B-GGUF", filename) == expected


@pytest.mark.parametrize(
    ("variant", "files", "expected"),
    [
        # One GGUF named for its quant alone: Splash matches no :VARIANT for it.
        ("q4_k_m", ["q4_k_m.gguf", "mmproj.f16.gguf"], "Hcompany/Holo4-27B-GGUF"),
        ("Holo4-27B-Q4_K_M", ["Holo4-27B-Q4_K_M.gguf"], "Hcompany/Holo4-27B-GGUF"),
        # Its -VARIANT ending resolves, so the ID keeps it.
        ("Q4_K_M", ["Holo4-27B-Q4_K_M.gguf"], "Hcompany/Holo4-27B-GGUF:Q4_K_M"),
        # Several GGUFs: the variant picks one.
        ("q4_k_m", ["q4_k_m.gguf", "q8_0.gguf"], "Hcompany/Holo4-27B-GGUF:q4_k_m"),
        # A variant that names another file is left for Splash to refuse.
        ("q8_0", ["q4_k_m.gguf"], "Hcompany/Holo4-27B-GGUF:q8_0"),
        # A split GGUF in its variant's folder is always installed as :VARIANT.
        (
            "Q2_0",
            ["Q2_0/M-Q2_0-00001-of-00002.gguf", "Q2_0/M-Q2_0-00002-of-00002.gguf"],
            "Hcompany/Holo4-27B-GGUF:Q2_0",
        ),
        # Safetensors beside it would make the bare ID an MLX model.
        ("q4_k_m", ["q4_k_m.gguf", "model.safetensors"], "Hcompany/Holo4-27B-GGUF:q4_k_m"),
        (None, ["q4_k_m.gguf"], "Hcompany/Holo4-27B-GGUF"),
    ],
)
def test_installable_id(variant: str | None, files: list[str], expected: str) -> None:
    assert cat.installable_id("Hcompany/Holo4-27B-GGUF", variant, files) == expected


def test_variant_pick_and_fit_rules() -> None:
    files: dict[str, int | None] = {
        "Qwen3.8-27B-UD-IQ3_XXS.gguf": 11 * GIB,
        "Qwen3.8-27B-UD-Q4_K_M.gguf": 17 * GIB,
        "Qwen3.8-27B-Q6_K.gguf": 22 * GIB,
        "UD-Q8_K_XL/Qwen3.8-27B-UD-Q8_K_XL-00001-of-00002.gguf": 15 * GIB,
        "UD-Q8_K_XL/Qwen3.8-27B-UD-Q8_K_XL-00002-of-00002.gguf": 15 * GIB,
        "Qwen3.8-27B-Q8_0-00001-of-00002.gguf": 15 * GIB,
        "Qwen3.8-27B-Q8_0-00002-of-00002.gguf": 15 * GIB,
        "Qwen3.8-27B-BF16.gguf": 54 * GIB,
        "mmproj-F16.gguf": GIB,
        "mmproj-BF16.gguf": GIB,
    }
    variants = cat.gguf_variants("unsloth/Qwen3.8-27B-GGUF", files)
    by = {v.name: v for v in variants}
    # Splash loads UD-Q8_K_XL's BF16 tensors, and a split GGUF from its variant's
    # folder; a whole BF16 model stays refused, and so do split parts at the root.
    assert by["UD-Q8_K_XL"].size == 30 * GIB and not by["UD-Q8_K_XL"].unloadable
    assert by["Q8_0"].size == 30 * GIB and by["Q8_0"].unloadable == cat.ROOT_SPLIT
    assert by["BF16"].unloadable == cat.WHOLE_UNQUANTIZED and not by["UD-Q4_K_M"].unloadable
    assert cat.projector(files) == "mmproj-BF16.gguf", "BF16 preferred, F16 refused"
    big = cat.pick_variant(variants, 64 * GIB, True)
    small = cat.pick_variant(variants, 24 * GIB, True)
    assert big is not None and big.name == "UD-Q4_K_M"
    assert small is not None and small.name == "UD-IQ3_XXS"
    assert cat.pick_variant(variants, 8 * GIB, True) is None
    need = cat.memory_need(17 * GIB, vision=True)
    assert need == 17 * GIB + cat.DRAFT_BYTES + cat.VISION_BYTES + cat.KV_RUNWAY_BYTES + 2 * GIB
    assert cat.fit_for(need, 64 * GIB) == "fits"
    assert cat.fit_for(need, 28 * GIB) == "tight"
    assert cat.fit_for(need, 22 * GIB) == "wont_fit"


# --- Installed models -------------------------------------------------------------


def test_installed_models_list_and_detail(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(installed=(MODEL, MODEL_27B))
    listing = h.client.get("/api/admin/models").json()
    by = {m["id"]: m for m in listing["models"]}
    assert set(by) == {MODEL, MODEL_27B}
    row = by[MODEL]
    assert row["format"] == "gguf" and row["variant"] == "UD-Q4_K_M"
    assert row["family"] == "Qwen3.6-35B-A3B" and row["status"] == "ready"
    assert row["size_bytes"] > 0 and 0 < row["unique_bytes"] <= row["size_bytes"]
    assert listing["disk"]["free_bytes"] > 0
    detail = h.client.get(f"/api/admin/models/{MODEL}").json()
    assert detail["files"] and detail["link_path"]
    assert h.client.get("/api/admin/models/nobody/nothing").status_code == 404


def test_the_active_model_is_marked_and_needs_confirmation_to_delete(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    h = harness_factory(installed=(MODEL,))
    h.load()
    by = {m["id"]: m for m in h.client.get("/api/admin/models").json()["models"]}
    assert by[MODEL]["status"] == "active"
    refused = h.client.delete(f"/api/admin/models/{MODEL}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "model_active"
    deleted = h.client.delete(f"/api/admin/models/{MODEL}", params={"confirm_active": True})
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["engine_stopped"] is True and body["freed_bytes"] > 0
    assert h.engine()["state"] == "stopped"
    assert h.client.get("/api/admin/models").json()["models"] == []


def test_verify_runs_the_installer_check(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(installed=(MODEL,))
    accepted = h.client.post(f"/api/admin/models/{MODEL}/verify", json={"full": True})
    assert accepted.status_code == 202, accepted.text
    deadline = time.monotonic() + 30
    while True:
        job = h.client.get(f"/api/admin/jobs/{accepted.json()['job_id']}").json()
        if job["state"] != "running":
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert job["model"] == MODEL


def test_a_pinned_model_cannot_be_updated(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(installed=(MODEL,))
    h.patch_settings({"models": {MODEL: {"serve": {"revision": "a" * 40}}}})
    row = h.client.get(f"/api/admin/models/{MODEL}").json()
    assert row["pinned"] is True
    response = h.client.post(f"/api/admin/models/{MODEL}/update")
    assert response.status_code == 409 and response.json()["error"]["code"] == "model_pinned"


def test_no_auto_load_can_start_the_engine_while_a_model_is_deleted(
    harness_factory: Callable[..., EngineHarness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete stops the engine, then removes files. A request arriving in between
    used to auto-load the model straight back onto the files being deleted."""
    from splash_gui.models.layout import execute_delete as real

    h = harness_factory(installed=(MODEL,))
    h.load()
    seen: dict[str, Any] = {}

    def deleting(*args: Any) -> int:
        # Runs in a worker thread while the delete holds the engine.
        seen["holds"] = list(h.state.supervisor._holds)
        return int(real(*args))

    monkeypatch.setattr("splash_gui.models.service.execute_delete", deleting)
    deleted = h.client.delete(f"/api/admin/models/{MODEL}", params={"confirm_active": True})
    assert deleted.status_code == 200, deleted.text
    assert seen["holds"] == ["model_delete"]
    assert h.state.supervisor._holds == []


def test_a_held_engine_refuses_loads_and_auto_loads(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    h = harness_factory(installed=(MODEL,))
    h.state.supervisor._holds.append("cache_clear")
    try:
        load = h.client.post("/api/admin/engine/load", json={"model": MODEL})
        assert load.status_code == 503 and load.json()["error"]["code"] == "engine_held"
        auto = h.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert auto.status_code == 503 and auto.headers["retry-after"] == "5"
        assert h.engine()["state"] == "stopped"
    finally:
        h.state.supervisor._holds.clear()
    assert h.client.post("/api/admin/engine/load", json={"model": MODEL}).status_code == 202


def test_catalog_download_bytes_match_the_inspect_download_plan(hub_harness) -> None:
    """Bug 3: a row's size was the target alone ("MLX · 15 GB") while the download
    also fetched the draft (19.93 GB). `download_bytes` is the whole fetch, from the
    same file plan as /inspect's download_plan, before and after an inspection."""
    h = hub_harness(installed=())
    before = entries(h.client.get("/api/admin/catalog").json())

    def plan(model: str) -> dict[str, Any]:
        response = h.client.post(f"/api/admin/inspect?id={model}&refresh=1")
        assert response.status_code == 200, response.text
        return dict(response.json())

    mlx = before["mlx-community/Qwen3.8-27B-4bit"]
    inspected = plan("mlx-community/Qwen3.8-27B-4bit")
    assert mlx["download_bytes"] == inspected["download_plan"]["total_bytes"]
    assert mlx["language_only_download_bytes"] == inspected["language_only_plan"]["total_bytes"]
    drafts = {f["repo_id"] for f in inspected["download_plan"]["files"]}
    assert drafts == {"mlx-community/Qwen3.8-27B-4bit", "incoai/Qwen3.8-27B-DFlash2"}
    assert mlx["download_bytes"] > mlx["size_bytes"], "the draft is part of the download"

    gguf = before["unsloth/Qwen3.6-35B-A3B-GGUF"]
    variant = next(v for v in gguf["variants"] if v["name"] == "UD-Q4_K_M")
    inspected = plan("unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M")
    assert variant["download_bytes"] == inspected["download_plan"]["total_bytes"]
    assert (
        variant["language_only_download_bytes"]
        == inspected["language_only_plan"]["total_bytes"]
        < variant["download_bytes"]
    ), "language-only skips the projector"
    default = next(v for v in gguf["variants"] if v["name"] == gguf["recommended_variant"])
    assert gguf["download_bytes"] == default["download_bytes"]

    # After an inspection the catalog uses Splash's own file set: same totals.
    after = entries(h.client.get("/api/admin/catalog").json())
    assert after["mlx-community/Qwen3.8-27B-4bit"]["download_bytes"] == mlx["download_bytes"]


def test_catalog_download_bytes_are_null_without_hub_data(hub_harness) -> None:
    h = hub_harness()
    h.patch_settings({"global": {"hf": {"offline": True}}})
    rows = entries(h.client.get("/api/admin/catalog").json())
    assert all(r["download_bytes"] is None for r in rows.values())


# Real listings (huggingface.co, 2026-10-04), trimmed to the root GGUFs.
QWEN35_GGUF: dict[str, int | None] = {
    "Qwen3.6-35B-A3B-MXFP4_MOE.gguf": 21706144736,
    "Qwen3.6-35B-A3B-Q8_0.gguf": 36903140320,
    "Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf": 13211155424,
    "Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf": 19500506080,
    "Qwen3.6-35B-A3B-UD-Q2_K_XL.gguf": 12290628576,
    "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf": 22134528992,
    "Qwen3.6-35B-A3B-UD-Q4_K_S.gguf": 20893015008,
    "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf": 22360456160,
    "Qwen3.6-35B-A3B-UD-Q5_K_M.gguf": 26456194016,
    "Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf": 38451182560,
    "mmproj-BF16.gguf": 902822624,
    "mmproj-F16.gguf": 899283680,
}
QWEN27_GGUF: dict[str, int | None] = {
    "Qwen3.8-27B-Q4_0.gguf": 16056478688,
    "Qwen3.8-27B-Q4_1.gguf": 17540705248,
    "Qwen3.8-27B-UD-IQ3_XXS.gguf": 10934860704,
    "Qwen3.8-27B-UD-Q4_K_M.gguf": 16464440224,
    "Qwen3.8-27B-UD-Q4_K_XL.gguf": 17559178144,
    "Qwen3.8-27B-UD-Q8_K_XL.gguf": 31457991680,
    "imatrix_unsloth.gguf": 13642656,
    "mmproj-BF16.gguf": 931146432,
    "BF16/Qwen3.8-27B-BF16-00001-of-00002.gguf": 27_000_000_000,
}
BONSAI_GGUF: dict[str, int | None] = {
    "Ternary-Bonsai-2-27B-F16.gguf": 53808408928,
    "Ternary-Bonsai-2-27B-PQ2_0.gguf": 7206168928,
    "Ternary-Bonsai-2-27B-PTQ1_0.gguf": 5946648928,
    "Ternary-Bonsai-2-27B-mmproj-BF16.gguf": 931145856,
    "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf": 629246976,
}


class _Info:
    def __init__(self, files: dict[str, int | None]) -> None:
        self.files, self.license, self.last_modified = files, "apache-2.0", None


def test_recommended_variant_stops_at_ud_q4_k_m() -> None:
    """UD-Q4_K_XL was recommended because only bits ≤ 4 was checked;
    The rule is "at or below UD-Q4_K_M-class"."""
    for repo, files in (
        ("unsloth/Qwen3.6-35B-A3B-GGUF", QWEN35_GGUF),
        ("unsloth/Qwen3.8-27B-GGUF", QWEN27_GGUF),
    ):
        facts = cat.entry_facts(repo, "gguf", _Info(files), 64 * GIB)
        assert facts["recommended_variant"] == "UD-Q4_K_M", repo
    # Smaller Macs fall back below the ceiling: at 32 GiB the 35B UD-Q4_K_M is only
    # Tight, UD-Q4_K_S fits; at 24 GiB the 3-bit class.
    repo = "unsloth/Qwen3.6-35B-A3B-GGUF"
    assert (
        cat.entry_facts(repo, "gguf", _Info(QWEN35_GGUF), 32 * GIB)["recommended_variant"]
        == "UD-Q4_K_S"
    )
    assert cat.entry_facts(repo, "gguf", _Info(QWEN35_GGUF), 24 * GIB)["recommended_variant"] in (
        "UD-IQ3_XXS",
        "UD-Q2_K_XL",
    )
    assert not cat.within_ceiling("Q4_1", 1, None) and not cat.within_ceiling("UD-Q4_K_XL", 1, None)
    assert cat.within_ceiling("UD-IQ4_NL_XL", 19_500_506_080, 22_134_528_992)
    assert not cat.within_ceiling("UD-IQ4_NL_XL", 19_500_506_080, 18_000_000_000)


def test_one_rule_names_the_variant_to_recommend_and_check_first() -> None:
    """The catalog, `/inspect` and the variant checked first share
    `cat.default_variant`: the preset pick's variant for its repository, else the
    memory rule; a variant Splash refused passes the mark down."""
    repo = "unsloth/Qwen3.6-35B-A3B-GGUF"
    variants = cat.gguf_variants(repo, QWEN35_GGUF)
    vision = cat.projector(QWEN35_GGUF) is not None
    facts = cat.entry_facts(repo, "gguf", _Info(QWEN35_GGUF), 64 * GIB)
    pick = cat.default_variant(repo, variants, 64 * GIB, vision=vision)
    assert pick == facts["recommended_variant"] == "UD-Q4_K_M"
    # 36-47 GB coding preset: the wizard's own variant of this repository wins.
    preset = f"{repo}:UD-Q4_K_M"
    assert cat.default_variant(repo, variants, 40 * GIB, vision=vision, preset_model=preset) == (
        "UD-Q4_K_M"
    )
    assert (
        cat.default_variant(repo, variants, 64 * GIB, vision=vision, preset_model="other/x:Q4")
        == "UD-Q4_K_M"
    )
    # Refused by Splash: the next one down the same rule, never the refused one.
    after = cat.default_variant(repo, variants, 64 * GIB, vision=vision, refused={"UD-Q4_K_M"})
    assert after not in (None, "UD-Q4_K_M")
    assert cat.within_ceiling(after, next(v.size for v in variants if v.name == after), None)
    assert (
        cat.default_variant(
            repo, variants, 64 * GIB, vision=vision, preset_model=preset, refused={"UD-Q4_K_M"}
        )
        == after
    )


def test_projectors_are_named_as_splash_names_them() -> None:
    """Prism's `MODEL-mmproj-BF16.gguf` was a model variant and the row
    said text only; Splash matches `mmproj` anywhere in the stem (upstream.py:62)."""
    facts = cat.entry_facts(
        "prism-ml/Ternary-Bonsai-2-27B-gguf", "gguf", _Info(BONSAI_GGUF), 64 * GIB
    )
    names = {v["name"] for v in facts["variants"]}
    assert names == {"F16", "PQ2_0", "PTQ1_0"}
    assert facts["vision"] is True
    assert cat.projector(BONSAI_GGUF) == "Ternary-Bonsai-2-27B-mmproj-BF16.gguf"
    assert facts["recommended_variant"] == "PQ2_0"
    assert cat.is_projector("mmproj-F16.gguf") and not cat.is_projector("Qwen3.8-27B-Q4_0.gguf")


def test_quality_tiers_follow_the_bit_width_and_dash_what_splash_cannot_load() -> None:
    """Every GGUF variant row carries its quality tier label. A variant
    Splash cannot load, and a name without a bit width, has none (the table shows a dash)."""
    assert cat.quality_tier("UD-IQ3_XXS") == "Compact"
    assert cat.quality_tier("UD-Q2_K_XL") == "Compact"
    assert cat.quality_tier("UD-Q4_K_M") == "Balanced"
    assert cat.quality_tier("UD-Q5_K_XL") == "Higher"
    assert cat.quality_tier("Q8_0") == "Highest"
    assert cat.quality_tier("UD-Q8_K_XL") == "Highest"
    assert cat.quality_tier("BF16") is None
    # MXFP4 and NVFP4 GGUFs are 4-bit.
    assert cat.quality_tier("MXFP4_MOE") == "Balanced"
    assert cat.quality_tier("NVFP4") == "Balanced"
    assert cat.bits("UD-MXFP4_MOE") == 4
    assert cat.quality_tier("imatrix_unsloth", ["imatrix_unsloth.gguf"]) is None
    assert cat.quality_tier("Mystery") is None
    facts = cat.entry_facts("unsloth/Qwen3.8-27B-GGUF", "gguf", _Info(QWEN27_GGUF), 64 * GIB)
    by = {v["name"]: v for v in facts["variants"]}
    assert by["UD-Q4_K_M"]["quality"] == "Balanced"
    assert by["UD-IQ3_XXS"]["quality"] == "Compact"
    assert by["UD-Q8_K_XL"]["quality"] == "Highest"
    assert all("quality" in v for v in facts["variants"])


def test_catalog_variants_carry_what_splash_cannot_load() -> None:
    """Every catalog variant had `loadable: null`, so the imatrix file and F16
    were offered as downloads. UD-Q8_K_XL loads, so /inspect decides it."""
    facts = cat.entry_facts("unsloth/Qwen3.8-27B-GGUF", "gguf", _Info(QWEN27_GGUF), 64 * GIB)
    by = {v["name"]: v for v in facts["variants"]}
    assert by["imatrix_unsloth"]["loadable"] is False
    assert by["imatrix_unsloth"]["reason"] == cat.NOT_A_MODEL
    assert by["UD-Q8_K_XL"]["loadable"] is None and by["UD-Q8_K_XL"]["reason"] is None
    assert "mmproj-BF16" not in by  # the projector is no variant
    # BF16/ holds one part of two: listed, never offered.
    assert by["BF16"]["loadable"] is False
    assert by["BF16"]["reason"] == "the split GGUF in BF16/ is incomplete"
    assert by["UD-Q4_K_M"]["loadable"] is None, "the rest is checked by /inspect"
    assert facts["size_bytes"] != QWEN27_GGUF["imatrix_unsloth.gguf"]
    bonsai = cat.entry_facts(
        "prism-ml/Ternary-Bonsai-2-27B-gguf", "gguf", _Info(BONSAI_GGUF), 64 * GIB
    )
    f16 = next(v for v in bonsai["variants"] if v["name"] == "F16")
    assert f16["loadable"] is False and f16["reason"] == cat.WHOLE_UNQUANTIZED


def test_language_only_selection_drops_prism_s_projector() -> None:
    """Review finding: `selected_files` kept Prism's `MODEL-mmproj-BF16.gguf` in a
    language-only selection (931 MB too many in `language_only_download_bytes`)."""
    files = ["Ternary-Bonsai-2-27B-PQ2_0.gguf", "Ternary-Bonsai-2-27B-mmproj-BF16.gguf"]
    chosen = cat.selected_files("gguf", BONSAI_GGUF, files, language_only=True)
    assert chosen == ["Ternary-Bonsai-2-27B-PQ2_0.gguf"]
    assert cat.selected_files("gguf", BONSAI_GGUF, files, language_only=False) == files


def test_an_f16_projector_serves_images_from_splash_1_3_1() -> None:
    """Splash 1.3.1 takes a BF16, F32 or F16 clip projector, in that order."""
    assert (
        cat.projector({"mmproj-F16.gguf": 1, "Qwen3.8-27B-UD-Q4_K_M.gguf": 2}) == "mmproj-F16.gguf"
    )
    both: dict[str, int | None] = {
        "mmproj-F16.gguf": 1,
        "mmproj-BF16.gguf": 1,
        "mmproj-F32.gguf": 1,
    }
    assert cat.projector(both) == "mmproj-BF16.gguf"
    assert cat.projector({"mmproj-F16.gguf": 1, "mmproj-F32.gguf": 1}) == "mmproj-F32.gguf"
    assert cat.projector({"mmproj-Q8_0.gguf": 1}) is None


def test_an_mlx_repo_with_processor_config_has_vision() -> None:
    """Newer Transformers releases save the image processor in processor_config.json,
    which Splash 1.3.1 reads (install/upstream.py `PROCESSOR_FILES`)."""
    files: dict[str, int | None] = {
        "config.json": 1,
        "processor_config.json": 1,
        "tokenizer.json": 1,
        "model.safetensors": 10,
    }
    facts = cat.entry_facts("someone/Qwen3.8-27B-6bit", "mlx", _Info(files), 64 * GIB)
    assert facts["vision"] is True
    selected = cat.selected_files("mlx", files, None, language_only=False)
    assert "processor_config.json" in selected
    assert "processor_config.json" not in cat.selected_files("mlx", files, None, language_only=True)


def test_mlx_refusals_get_a_plain_line() -> None:
    from splash_gui.models.compat import MLX_QUANTIZATION, MLX_UNQUANTIZED, explain_refusal

    # Splash 1.3.2's wording (runtime/model/ModelDescriptor.mm `requireMlxQuantization`,
    # `requireQuantization`).
    seven = (
        "quantization language_model.model.embed_tokens is affine 7-bit in groups of 64; MLX "
        "weights load as affine 2, 3, 4, 5, 6 or 8 bits in groups of 32, 64 or 128, or as "
        "mxfp4 or nvfp4"
    )
    assert explain_refusal(seven) == (MLX_QUANTIZATION, seven)
    bf16 = (
        "this model requires an MLX checkpoint (affine 2, 3, 4, 5, 6 or 8 bits in groups of "
        "32, 64 or 128, mxfp4 or nvfp4), an NVFP4 or FP8 checkpoint of Model Optimizer or "
        "compressed-tensors, or a supported GGUF"
    )
    assert explain_refusal(bf16) == (MLX_QUANTIZATION, bf16)
    unquantized = "quantization lm_head is unquantized; Splash loads quantized MLX projections"
    assert explain_refusal(unquantized) == (MLX_UNQUANTIZED, unquantized)
    assert explain_refusal("something else") == ("something else", None)
    assert explain_refusal(None) == (None, None)


@pytest.mark.parametrize(
    ("engine", "plain"),
    [
        (
            "quantization_config is INT4_AWQ; Model Optimizer weights load as NVFP4, "
            "W4A16_NVFP4 or FP8",
            "CHECKPOINT_QUANTIZATION",
        ),
        (
            "quantization_config quantized_layers lm_head is NVFP4 in groups of 32; Model "
            "Optimizer NVFP4 loads in groups of 16",
            "CHECKPOINT_QUANTIZATION",
        ),
        (
            "quantization_config config_groups group_0 is pack-quantized: int 4-bit by group "
            "in groups of 128; compressed-tensors weights load as NVFP4 (nvfp4-pack-quantized: "
            "float 4-bit by tensor_group in groups of 16) or FP8 (float-quantized: float 8-bit "
            "by channel or tensor)",
            "CHECKPOINT_QUANTIZATION",
        ),
        (
            "quantization_config config_groups group_0 is asymmetric or ordered by activation "
            "groups; Splash loads symmetric compressed-tensors weights in their stored column "
            "order",
            "CHECKPOINT_QUANTIZATION",
        ),
        ("quantization_config ignore must be an array", "CHECKPOINT_QUANTIZATION"),
        (
            "quantization_config leaves lm_head unquantized; Splash loads quantized projections",
            "CHECKPOINT_UNQUANTIZED",
        ),
        (
            "this GGUF stores every projection and expert unquantized (BF16, F16 or F32): "
            "Splash loads quantized GGUFs, not whole-BF16 or F16 models; choose a quantized "
            "variant of the repository",
            "WHOLE_UNQUANTIZED",
        ),
    ],
)
def test_checkpoint_and_whole_unquantized_refusals_get_a_plain_line(
    engine: str, plain: str
) -> None:
    """Splash 1.3.2's refusals of a Model Optimizer or compressed-tensors checkpoint
    (ModelDescriptor.mm) and of a whole-BF16 GGUF (install/gguf.py `require_loadable`)."""
    from splash_gui.models import compat

    expected = getattr(compat, plain, None) or getattr(cat, plain)
    assert compat.explain_refusal(engine) == (expected, engine)


# --- Streaming from the SSD, and Qwen3.8-Flash-Next -------------------------------

GB = 1000**3
FLASH_NEXT_GGUF: dict[str, int | None] = {
    "Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00001-of-00002.gguf": 37_620_000_000,
    "Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf": 28_800_000_000,
    "IQ2_XS/Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf": 38_700_000_000,
    "IQ2_XS/Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00002-of-00002.gguf": 28_800_000_000,
    "IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf": 55_000_000_000,
    "IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf": 28_800_000_000,
    "mmproj-Qwen3.8-Flash-Next-BF16.gguf": 900_000_000,
}
ISTA = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"


def test_a_moe_model_too_large_to_load_whole_streams() -> None:
    """A MoE model's routed experts, and Flash-Next's PLE table, can stay on the SSD:
    a variant larger than memory "streams" when what stays resident fits. A dense
    model never streams."""
    flash = "Qwen3.8-Flash-Next"
    q2 = 66_420_000_000
    assert cat.fit_of(q2, 64 * GIB, vision=True, family=flash) == "streams"
    assert cat.fit_of(q2, 128 * GIB, vision=True, family=flash) == "fits"
    streamed = cat.streamed_need(flash, q2, vision=True)
    assert streamed is not None
    # The rest of the weights, one layer's experts and a PLE row cache of 512 MiB.
    ple = 28_800_000_000
    experts = int((q2 - ple) * 0.9)
    resident = q2 - experts - ple + experts // 48 + 512 * 1024**2
    assert streamed == cat.memory_need(resident, vision=True, family=flash)
    assert cat.streamed_need("Qwen3.8-27B", q2, vision=True) is None
    assert cat.fit_of(q2, 64 * GIB, vision=True, family="Qwen3.8-27B") == "wont_fit"
    big_35b = 37 * GB
    assert cat.fit_of(big_35b, 24 * GIB, vision=True, family="Qwen3.6-35B-A3B") == "streams"
    assert cat.fit_of(big_35b, 8 * GIB, vision=True, family="Qwen3.6-35B-A3B") == "wont_fit"
    assert cat.fit_for(10 * GIB, 64 * GIB, streamed=1) == "fits", "loading whole comes first"


def test_the_recommendation_streams_only_when_nothing_fits_whole() -> None:
    variants = cat.gguf_variants(ISTA, FLASH_NEXT_GGUF)
    assert [v.name for v in variants] == ["Q2_0", "IQ2_XS", "IQ3_S"]
    assert variants[0].size == 66_420_000_000 and not variants[0].unloadable
    # On 64 GB nothing fits whole: the smallest that streams, which reads the least.
    assert cat.default_variant(ISTA, variants, 64 * GIB, vision=True) == "Q2_0"
    # With room for them whole, the largest that fits, as for any model.
    assert cat.default_variant(ISTA, variants, 128 * GIB, vision=True) == "IQ3_S"
    facts = cat.entry_facts(ISTA, "gguf", _Info(FLASH_NEXT_GGUF), 64 * GIB, "Qwen3.8-Flash-Next")
    by = {v["name"]: v for v in facts["variants"]}
    assert {v["fit"] for v in by.values()} == {"streams"}
    assert by["Q2_0"]["recommended"] and facts["fit"] == "streams"
    assert facts["recommended_variant"] == "Q2_0"
    assert by["Q2_0"]["bits_per_weight"] == 2.0 and by["Q2_0"]["quality"] == "Compact"
    # Without the family the catalog seed gives, the name's guess decides.
    assert cat.family_guess(ISTA) == "Qwen3.8-Flash-Next"


def test_flash_next_s_draft_kv_and_state_terms() -> None:
    flash = "Qwen3.8-Flash-Next"
    assert cat.DRAFT_REPOS[flash] == "incoai/Qwen3.8-Flash-Next-DFlash2"
    assert (
        cat.memory_need(0, vision=False, family=flash) - cat.memory_need(0, vision=False)
        == 1_210_000_000 - cat.DRAFT_BYTES
    )
    # 12 attention layers of 2 KV heads of 256 in INT8, plus 768 bytes of indexer keys.
    assert cat.kv_bytes(flash, 1) == 12 * 2 * 2 * (256 + 4) + 768
    assert cat.kv_bytes(flash, 1, bf16=True) == 12 * 2 * 2 * 512 + 768
    assert cat.state_staging_bytes(flash, 16 * GIB) == 113 * 1024**2
    assert cat.FAMILY_LABELS[flash] == "Qwen3.8-Flash-Next MoE"
    assert any(row[0] == flash for row in cat.SEED)


def test_a_folder_a_root_variant_shadows_is_not_listed() -> None:
    files: dict[str, int | None] = {
        "M-Q8_0.gguf": 5,
        "Q8_0/M-Q8_0-00001-of-00002.gguf": 7,
        "Q8_0/M-Q8_0-00002-of-00002.gguf": 7,
        "q4_0.gguf": 3,
        "Q4_0/M-Q4_0-00001-of-00002.gguf": 4,
        "Q4_0/M-Q4_0-00002-of-00002.gguf": 4,
    }
    by = {v.name: v for v in cat.gguf_variants("o/M-GGUF", files)}
    assert by["Q8_0"].files == ["M-Q8_0.gguf"] and by["Q8_0"].size == 5
    assert "Q4_0" not in by and by["q4_0"].files == ["q4_0.gguf"]
