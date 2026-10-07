"""Compatibility checking end to end (SPEC §9.2, D7).

The real check is a helper subprocess that imports the engine's own
`install.families` / `install.upstream` and screens every GGUF variant. Here the
"engine" is scripts/fake_splash/pkg and the Hub is the fake HTTP API, so this
exercises the whole chain: the manager's HfClient, the helper subprocess, the
fake engine's module surface, and the badge/fit/recommendation rules on the way
back out.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from .fakeengine import FAKE, MODEL, MODEL_27B

GGUF_35B = "unsloth/Qwen3.6-35B-A3B-GGUF"
GGUF_27B = "prism-ml/Ternary-Bonsai-2-27B-gguf"
MLX_35B = "mlx-community/Qwen3.6-35B-A3B-4bit"
MLX_8BIT = "mlx-community/Qwen3.6-35B-A3B-8bit"
LEGACY = "incoai/Qwen3.6-35B-A3B-Splash"


def hub_module() -> Any:
    """scripts/fake_splash/hub.py, which is a script rather than a package."""
    if str(FAKE) not in sys.path:
        sys.path.insert(0, str(FAKE))
    return importlib.import_module("hub")


@pytest.fixture
def hub_harness(harness_factory, fake_hub, monkeypatch):
    """A harness whose manager *and* engine-side helper both talk to the fake Hub.

    The manager reads `hf.endpoint`; the helper subprocess reads `HF_ENDPOINT`.
    Setting only one of them would send half the chain to the real Hub.
    """
    monkeypatch.setenv("HF_ENDPOINT", fake_hub)

    def make(**kwargs):
        harness = harness_factory(installed=(), env={"HF_ENDPOINT": fake_hub}, **kwargs)
        harness.patch_settings({"global": {"hf": {"endpoint": fake_hub}}})
        assert harness.state.settings.current.global_.hf.endpoint == fake_hub
        return harness

    return make


def inspect(harness, model: str, refresh: bool = True) -> dict[str, Any]:
    response = harness.client.post(f"/api/admin/inspect?id={model}&refresh={1 if refresh else 0}")
    assert response.status_code == 200, response.text
    return dict(response.json())


def point_at_hub(client, fake_hub: str) -> None:
    current = client.get("/api/admin/settings")
    assert current.status_code == 200, current.text
    document = dict(current.json()["settings"])
    document.setdefault("global", {}).setdefault("hf", {})["endpoint"] = fake_hub
    saved = client.put("/api/admin/settings", json=document)
    assert saved.status_code == 200, saved.text


def test_gguf_variant_is_compatible_and_vision_capable(hub_harness):
    harness = hub_harness()
    result = inspect(harness, f"{GGUF_35B}:UD-Q4_K_M")
    assert result["compatible"] is True
    assert result["badge"] == "compatible"
    assert result["format"] == "gguf"
    assert result["family"] == "Qwen3.6-35B-A3B"
    assert result["vision"]["available"] is True
    assert result["reason"] is None
    # A paired DFlash2 draft is reported so the model detail view can show it.
    assert result["draft"] == "incoai/Qwen3.6-35B-A3B-DFlash2"


def test_mlx_repo_is_compatible_with_a_draft(hub_harness):
    harness = hub_harness()
    result = inspect(harness, MLX_35B)
    assert result["compatible"] is True
    assert result["format"] == "mlx"
    assert result["family"] == "Qwen3.6-35B-A3B"
    assert result["draft"] == "incoai/Qwen3.6-35B-A3B-DFlash2"


def test_8bit_mlx_is_rejected_with_the_reason(hub_harness):
    """Splash only has kernels for affine 4-bit group 64, so this must not be
    offered as downloadable (D7)."""
    harness = hub_harness()
    result = inspect(harness, MLX_8BIT)
    assert result["compatible"] is False
    assert result["badge"] == "incompatible"
    # D53: a plain line first; the engine's model-check wording stays as the detail.
    assert result["reason"] == "Splash runs MLX models only at 4-bit, group size 64."
    assert result["reason_detail"].endswith("bits mismatch: MLX 8, runtime 4")


def test_a_refused_mlx_download_leads_with_the_plain_line(hub_harness):
    """POST /downloads refuses the 8-bit checkpoint with the plain line, then
    Splash's own words, so the CLI and web toasts show both (D53)."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": MLX_8BIT})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "incompatible"
    assert error["message"].startswith(
        "Splash runs MLX models only at 4-bit, group size 64. Splash's check: quantization "
    )
    assert error["details"]["reason_detail"].endswith("bits mismatch: MLX 8, runtime 4")


def test_the_dense_family_is_distinguished_from_the_moe(hub_harness):
    """Support is decided by the architecture in the config, never the repo
    name (CLAUDE.md, SPEC §3.2)."""
    harness = hub_harness()
    assert inspect(harness, GGUF_27B)["family"] == "Qwen3.8-27B"
    assert inspect(harness, MODEL_27B)["family"] == "Qwen3.8-27B"


def test_legacy_package_is_compatible_and_reports_the_legacy_format(hub_harness):
    harness = hub_harness()
    result = inspect(harness, LEGACY)
    assert result["compatible"] is True
    assert result["format"] == "legacy"
    assert result["family"] == "Qwen3.6-35B-A3B"


def test_variant_table_reports_loadability_and_a_recommendation(hub_harness):
    """SPEC §9.1: a variant table with sizes, loadability, a fit badge and one
    recommended pick."""
    harness = hub_harness()
    result = inspect(harness, GGUF_35B)
    assert result["variants"], "the variant table must not be empty"
    for variant in result["variants"]:
        assert variant["size_bytes"], "every variant needs a size for the fit badge"
        assert variant["fit"] in ("fits", "tight", "wont_fit")
        assert variant["loadable"] in (True, False)
    recommended = [v for v in result["variants"] if v["recommended"]]
    assert len(recommended) <= 1, "at most one Recommended mark"


def test_only_loadable_variants_are_ever_marked_recommended(hub_harness):
    harness = hub_harness()
    result = inspect(harness, GGUF_35B)
    for variant in result["variants"]:
        if variant["recommended"]:
            assert variant["loadable"] is True
            assert variant["fit"] == "fits"
            assert variant["name"] == result["recommended_variant"]


def test_results_are_cached_per_repo_sha(hub_harness):
    """SPEC §9.2 caches by repo@sha for 24 h."""
    harness = hub_harness()
    first = inspect(harness, MODEL)
    assert first["cached"] is False
    second = inspect(harness, MODEL, refresh=False)
    assert second["cached"] is True
    assert second["commit"] == first["commit"]
    assert second["compatible"] == first["compatible"]


def test_refresh_re_runs_the_check(hub_harness):
    harness = hub_harness()
    inspect(harness, MODEL, refresh=False)
    assert inspect(harness, MODEL, refresh=True)["cached"] is False


def test_search_returns_full_ids_from_the_fake_hub(hub_harness):
    """D22: no short aliases, only full Hugging Face IDs."""
    harness = hub_harness()
    response = harness.client.get("/api/admin/search?q=Qwen3.6&sort=downloads")
    assert response.status_code == 200, response.text
    payload = dict(response.json())
    assert payload["sort"] == "downloads"
    ids = [row["id"] for row in payload["results"]]
    assert GGUF_35B in ids
    assert MLX_35B in ids
    assert all("/" in repo_id for repo_id in ids)
    guesses = {row["id"]: row["format_guess"] for row in payload["results"]}
    assert guesses[GGUF_35B] == "gguf"
    assert guesses[MLX_35B] == "mlx"


def test_catalog_is_grouped_by_family_and_format(hub_harness):
    """SPEC §9.1: grouped by family, then by format."""
    harness = hub_harness()
    response = harness.client.get("/api/admin/catalog")
    assert response.status_code == 200, response.text
    payload = dict(response.json())
    assert payload["families"], "the bundled catalog seed must not be empty (Appendix C)"
    assert payload["memory_bytes"] > 0
    for family in payload["families"]:
        assert family["family"] in ("Qwen3.8-27B", "Qwen3.6-35B-A3B")
        assert family["label"]
        groups = family["groups"]
        assert groups, "SPEC §9.1 groups each family by format"
        for group in groups:
            ids = [entry["id"] for entry in group["entries"]]
            assert ids
            assert all("/" in repo_id for repo_id in ids), "D22: full Hugging Face IDs only"
            assert all(entry["format"] in ("mlx", "gguf", "legacy") for entry in group["entries"])


def test_model_card_comes_from_the_hub(hub_harness):
    harness = hub_harness()
    response = harness.client.get(f"/api/admin/card?id={GGUF_35B}")
    assert response.status_code == 200, response.text
    card = dict(response.json())
    assert "Fixture model" in card["markdown"]
    assert card["license"] == "apache-2.0"
    assert card["files"], "the detail view lists files with sizes (SPEC §9.3)"


def test_a_missing_engine_refuses_rather_than_guessing(client, fake_hub):
    """SPEC §9.2: the API never imports Splash into the manager process, so with
    no engine python it must answer 503 instead of screening by itself."""
    point_at_hub(client, fake_hub)
    response = client.post(f"/api/admin/inspect?id={MODEL}&refresh=1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"


def test_a_model_id_without_a_repository_is_rejected(client, fake_hub):
    point_at_hub(client, fake_hub)
    response = client.post("/api/admin/inspect?id=qwen3.6-35b&refresh=1")
    assert response.status_code == 400, response.text


def test_offline_mode_reports_the_hub_as_unreachable(client, fake_hub):
    point_at_hub(client, fake_hub)
    current = client.get("/api/admin/settings").json()["settings"]
    current["global"]["hf"]["offline"] = True
    saved = client.put("/api/admin/settings", json=current)
    assert saved.status_code == 200, saved.text
    response = client.post(f"/api/admin/inspect?id={MODEL}&refresh=1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "hub_unreachable"


def test_an_unknown_repository_is_a_404(client, fake_hub):
    point_at_hub(client, fake_hub)
    response = client.get("/api/admin/card?id=owner/nothing-here")
    assert response.status_code == 404, response.text


def test_the_hub_advertises_the_files_the_installer_downloads(fake_hub, isolated_home):
    """The fake Hub is only meaningful if its file set equals the installer's.
    Guards the two stand-ins against drifting apart."""
    import json
    import urllib.request

    from .fakeengine import install

    install(isolated_home, MODEL)  # the real code path the manager drives
    _, gguf_files, _ = hub_module().repository(GGUF_35B)
    assert any(name.endswith("UD-Q4_K_M.gguf") for name in gguf_files)
    # Splash only accepts a BF16 or F32 clip projector; F16 has already rounded.
    assert "mmproj-BF16.gguf" in gguf_files

    _, mlx_files, config = hub_module().repository(MLX_35B)
    assert "config.json" in mlx_files
    assert config["quantization"] == {"bits": 4, "group_size": 64, "mode": "affine"}

    # And the Hub really serves the metadata the manager reads.
    with urllib.request.urlopen(f"{fake_hub}/api/models/{GGUF_35B}?blobs=true") as response:
        listing = json.load(response)
    assert set(gguf_files) <= {sibling["rfilename"] for sibling in listing["siblings"]}
    assert listing["sha"]


def test_the_helper_subprocess_really_runs_the_engines_modules(hub_harness):
    """Assert the fake engine's screening did the work rather than the manager
    guessing: a variant it cannot load comes back with the engine's own reason."""
    harness = hub_harness()
    result = inspect(harness, MLX_8BIT)
    assert result["reason"]
    assert result["family"] is None, "an unreadable quantization yields no family"


def test_inspect_records_the_files_it_would_download(hub_harness):
    """The download pipeline reuses this file set, so it must be non-empty."""
    harness = hub_harness()
    inspect(harness, MODEL)
    files = harness.state.models.file_sets.get(MODEL) or []
    assert files
    assert any(name.endswith(".gguf") for name in files)
    language = harness.state.models.language_file_sets.get(MODEL) or []
    assert set(language) <= set(files)


def test_engine_discovery_exposes_what_the_helper_needs(hub_harness):
    engine = hub_harness().state.engine_cached()
    assert engine.found
    assert engine.python is not None and Path(engine.python).exists()
    assert engine.pkg is not None and Path(engine.pkg, "install", "upstream.py").exists()


def test_acceptance_search_verdicts(hub_harness):
    """SPEC §21: HF search marks `mlx-community/Qwen3.8-27B-8bit` incompatible with
    the reason, and `unsloth/Qwen3.8-27B-GGUF` compatible with a recommended
    variant (on the owner's 64 GB Mac)."""
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    found = harness.client.get("/api/admin/search", params={"q": "Qwen3.8-27B"}).json()
    ids = {r["id"]: r for r in found["results"]}
    assert "mlx-community/Qwen3.8-27B-8bit" in ids and "unsloth/Qwen3.8-27B-GGUF" in ids
    assert ids["unsloth/Qwen3.8-27B-GGUF"]["format_guess"] == "gguf"

    eight = inspect(harness, "mlx-community/Qwen3.8-27B-8bit")
    assert eight["compatible"] is False and eight["badge"] == "incompatible"
    assert eight["family"] in (None, "Qwen3.8-27B")
    assert eight["reason"] == "Splash runs MLX models only at 4-bit, group size 64."
    assert "bits mismatch: MLX 8" in eight["reason_detail"], eight["reason_detail"]

    gguf = inspect(harness, "unsloth/Qwen3.8-27B-GGUF")
    assert gguf["compatible"] is True and gguf["family"] == "Qwen3.8-27B"
    assert gguf["recommended_variant"], gguf
    picked = next(v for v in gguf["variants"] if v["recommended"])
    assert picked["name"] == gguf["recommended_variant"]
    assert picked["loadable"] is True and picked["fit"] == "fits"


def test_variant_labels_resolve_with_splash_s_own_selector(tmp_path):
    """A stray `imatrix_*.gguf` leaves the root files no shared model name, so
    Splash's prefix rule names nothing; the label must still be the short
    VARIANT that `install/upstream.py select_gguf` resolves (checked on the live
    unsloth/Qwen3.8-27B-GGUF, 2026-10-04)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    reference = Path(__file__).resolve().parents[2] / "splash"
    pkg = reference if (reference / "install" / "upstream.py").exists() else None
    if pkg is None:
        pytest.skip("no Splash reference clone to resolve against")
    helper = Path(__file__).resolve().parents[1] / "splash_gui" / "helpers"
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(helper)!r})\n"
        "from install import upstream\n"
        "from inspect_model import variant_label\n"
        "names = json.loads(sys.argv[1])\n"
        "print(json.dumps([variant_label(sys.argv[2], n, names, upstream) for n in names]))\n"
    )
    names = [
        "Qwen3.8-27B-Q4_0.gguf",
        "Qwen3.8-27B-UD-Q4_K_M.gguf",
        "Qwen3.8-27B-UD-Q4_K_XL.gguf",
        "imatrix_unsloth.gguf",
    ]
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(names), "unsloth/Qwen3.8-27B-GGUF"],
        env={"PYTHONPATH": str(pkg), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["Q4_0", "UD-Q4_K_M", "UD-Q4_K_XL", "imatrix_unsloth"]
