"""Installed models, the curated catalog and model actions (SPEC §9.1, §9.3, §9.5)."""

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


# --- Catalog (SPEC §9.1) ------------------------------------------------------------------


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
    # SPEC §8.6: on 64 GB the coding pick is the 27B MLX checkpoint.
    assert rows["mlx-community/Qwen3.8-27B-4bit"]["recommended"] is True
    assert sum(r["recommended"] for r in rows.values()) == 1


def test_catalog_offline_keeps_the_rows_without_hub_data(hub_harness) -> None:
    h = hub_harness()
    h.patch_settings({"global": {"hf": {"offline": True}}})
    payload = h.client.get("/api/admin/catalog").json()
    assert payload["offline"] is True and payload["refreshed_at"] is None
    rows = entries(payload)
    assert len(rows) >= 7
    assert all(r["size_bytes"] is None for r in rows.values())


def test_catalog_merges_splash_s_official_list(hub_harness) -> None:
    h = hub_harness()
    official = h.home / "fake-data" / "catalog" / "official-models.txt"
    official.parent.mkdir(parents=True, exist_ok=True)
    official.write_text("# refreshed\nincoai/Qwen3.8-27B-Splash\nincoai/Qwen3.9-27B-Splash\n")
    rows = entries(h.client.get("/api/admin/catalog").json())
    assert rows["incoai/Qwen3.9-27B-Splash"]["format"] == "legacy"
    assert rows["incoai/Qwen3.9-27B-Splash"]["family"] == "Qwen3.8-27B"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Qwen3.8-27B-UD-Q4_K_M.gguf", "UD-Q4_K_M"),
        ("Qwen3.8-27B-UD-Q8_K_XL-00001-of-00002.gguf", "UD-Q8_K_XL"),
        ("mmproj-BF16.gguf", None),
        ("BF16/Qwen3.8-27B-BF16-00001-of-00002.gguf", None),
        ("README.md", None),
    ],
)
def test_variant_names(filename: str, expected: str | None) -> None:
    assert cat.variant_name("unsloth/Qwen3.8-27B-GGUF", filename) == expected


def test_variant_pick_and_fit_rules() -> None:
    files: dict[str, int | None] = {
        "Qwen3.8-27B-UD-IQ3_XXS.gguf": 11 * GIB,
        "Qwen3.8-27B-UD-Q4_K_M.gguf": 17 * GIB,
        "Qwen3.8-27B-Q6_K.gguf": 22 * GIB,
        "Qwen3.8-27B-UD-Q8_K_XL-00001-of-00002.gguf": 15 * GIB,
        "Qwen3.8-27B-UD-Q8_K_XL-00002-of-00002.gguf": 15 * GIB,
        "Qwen3.8-27B-BF16.gguf": 54 * GIB,
        "mmproj-F16.gguf": GIB,
        "mmproj-BF16.gguf": GIB,
    }
    variants = cat.gguf_variants("unsloth/Qwen3.8-27B-GGUF", files)
    by = {v.name: v for v in variants}
    assert by["UD-Q8_K_XL"].size == 30 * GIB and by["UD-Q8_K_XL"].unloadable
    assert by["BF16"].unloadable and not by["UD-Q4_K_M"].unloadable
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


# --- Installed models (SPEC §9.5) -------------------------------------------------------------


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
    """QA row 13: UD-Q4_K_XL was recommended because only bits ≤ 4 was checked;
    SPEC §9.1 says "at or below UD-Q4_K_M-class"."""
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


def test_projectors_are_named_as_splash_names_them() -> None:
    """QA row 14: Prism's `MODEL-mmproj-BF16.gguf` was a model variant and the row
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


def test_catalog_variants_carry_what_splash_cannot_load() -> None:
    """QA row 15: every catalog variant had `loadable: null`, so the imatrix file,
    F16 and UD-Q8_K_XL were offered as downloads."""
    facts = cat.entry_facts("unsloth/Qwen3.8-27B-GGUF", "gguf", _Info(QWEN27_GGUF), 64 * GIB)
    by = {v["name"]: v for v in facts["variants"]}
    assert by["imatrix_unsloth"]["loadable"] is False
    assert by["imatrix_unsloth"]["reason"] == cat.NOT_A_MODEL
    assert by["UD-Q8_K_XL"]["loadable"] is False and by["UD-Q8_K_XL"]["reason"]
    assert "mmproj-BF16" not in by and "BF16" not in by  # projector; BF16/ is a subfolder
    assert by["UD-Q4_K_M"]["loadable"] is None, "the rest is checked by /inspect"
    assert facts["size_bytes"] != QWEN27_GGUF["imatrix_unsloth.gguf"]
    bonsai = cat.entry_facts(
        "prism-ml/Ternary-Bonsai-2-27B-gguf", "gguf", _Info(BONSAI_GGUF), 64 * GIB
    )
    f16 = next(v for v in bonsai["variants"] if v["name"] == "F16")
    assert f16["loadable"] is False and "F16" in f16["reason"]


def test_language_only_selection_drops_prism_s_projector() -> None:
    """Review finding: `selected_files` kept Prism's `MODEL-mmproj-BF16.gguf` in a
    language-only selection (931 MB too many in `language_only_download_bytes`)."""
    files = ["Ternary-Bonsai-2-27B-PQ2_0.gguf", "Ternary-Bonsai-2-27B-mmproj-BF16.gguf"]
    chosen = cat.selected_files("gguf", BONSAI_GGUF, files, language_only=True)
    assert chosen == ["Ternary-Bonsai-2-27B-PQ2_0.gguf"]
    assert cat.selected_files("gguf", BONSAI_GGUF, files, language_only=False) == files
