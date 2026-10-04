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
