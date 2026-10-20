"""Compatibility checking end to end.

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
MLX_7BIT = "mlx-community/Qwen3.6-35B-A3B-7bit"
LEGACY = "incoai/Qwen3.6-35B-A3B-Splash"
PLAIN_MLX = (
    "Splash runs MLX models quantized as affine 2, 3, 4, 5, 6 or 8 bits "
    "(groups of 32, 64 or 128), mxfp4 or nvfp4, and NVFP4 or FP8 checkpoints "
    "from Model Optimizer or compressed-tensors."
)


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


def test_8bit_mlx_is_compatible(hub_harness):
    """Splash 1.3.1 loads MLX checkpoints at 2, 3, 4, 5, 6 and 8 bits."""
    result = inspect(hub_harness(), MLX_8BIT)
    assert result["compatible"] is True and result["format"] == "mlx"
    assert result["family"] == "Qwen3.6-35B-A3B"


@pytest.mark.parametrize(
    ("repo", "label"),
    [
        ("nvidia/Qwen3.8-27B-NVFP4", "NVFP4 · Model Optimizer"),
        ("unsloth/Qwen3.6-35B-A3B-NVFP4", "NVFP4 · compressed-tensors"),
    ],
)
def test_nvfp4_checkpoints_are_safetensors_targets_labelled_apart(hub_harness, repo, label):
    """Splash checks a Model Optimizer or compressed-tensors checkpoint as it
    does an MLX one; the label from the Hub's tags keeps it from showing as MLX."""
    harness = hub_harness()
    result = inspect(harness, repo)
    assert result["compatible"] is True and result["format"] == "mlx", result
    assert result["checkpoint"] == label
    hits = harness.client.get("/api/admin/search?q=NVFP4").json()["results"]
    hit = next(r for r in hits if r["id"] == repo)
    assert hit["format_guess"] == "mlx" and hit["checkpoint"] == label


def test_the_catalog_lists_nvidia_s_nvfp4_checkpoint(hub_harness):
    catalog = hub_harness().client.get("/api/admin/catalog").json()
    rows = {e["id"]: e for f in catalog["families"] for g in f["groups"] for e in g["entries"]}
    row = rows["nvidia/Qwen3.8-27B-NVFP4"]
    assert row["format"] == "mlx" and row["checkpoint"] == "NVFP4 · Model Optimizer"
    assert row["size_bytes"] and row["download_bytes"]
    assert rows["mlx-community/Qwen3.6-35B-A3B-nvfp4"]["checkpoint"] is None


def test_7bit_mlx_is_rejected_with_the_reason(hub_harness):
    """Splash has no kernels for affine 7-bit, so this must not be offered as
    downloadable."""
    harness = hub_harness()
    result = inspect(harness, MLX_7BIT)
    assert result["compatible"] is False
    assert result["badge"] == "incompatible"
    # A plain line first; the engine's model-check wording stays as the detail.
    assert result["reason"] == PLAIN_MLX
    detail = result["reason_detail"]
    assert "quantization is affine 7-bit in groups of 64; MLX weights load as" in detail


def test_a_refused_mlx_download_leads_with_the_plain_line(hub_harness):
    """POST /downloads refuses the 7-bit checkpoint with the plain line, then
    Splash's own words, so the CLI and web toasts show both."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": MLX_7BIT})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "incompatible"
    assert error["message"].startswith(f"{PLAIN_MLX} Splash's check: quantization is affine 7-bit")
    assert "MLX weights load as" in error["details"]["reason_detail"]


def test_the_dense_family_is_distinguished_from_the_moe(hub_harness):
    """Support is decided by the architecture in the config, never the repo
    name."""
    harness = hub_harness()
    assert inspect(harness, GGUF_27B)["family"] == "Qwen3.8-27B"
    assert inspect(harness, MODEL_27B)["family"] == "Qwen3.8-27B"


def test_a_splash_package_is_refused_with_its_mlx_model(hub_harness):
    """Splash 1.3.1 no longer loads Splash packages; its refusal names the MLX model."""
    result = inspect(hub_harness(), LEGACY)
    assert result["compatible"] is False and result["badge"] == "incompatible"
    assert result["reason"] == (
        f"{LEGACY} is a Splash package, which Splash no longer loads; serve the MLX model of "
        "its family instead: splash serve --model mlx-community/Qwen3.6-35B-A3B-4bit"
    )


def test_variant_table_reports_loadability_and_a_recommendation(hub_harness):
    """A variant table with sizes, loadability, a fit badge and one
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


def test_inspect_rows_carry_their_quality_tier_and_the_projector_is_labelled(hub_harness):
    """A variant row has its quality tier (a dash for one Splash cannot load).
    The vision projector is named on `vision` and never listed as a variant; an MLX
    repository has none."""
    harness = hub_harness()
    result = inspect(harness, MANY)
    tiers = {v["name"]: v["quality"] for v in result["variants"]}
    assert tiers["UD-Q4_K_M"] == "Balanced"
    assert tiers["UD-IQ2_XXS"] == "Compact"
    assert tiers["Q8_0"] == "Highest"
    assert tiers["BF16"] is None
    projector = result["vision"]["projector"]
    assert projector and "mmproj" in projector, result["vision"]
    assert result["vision"]["projector_bytes"] > 0
    assert not [name for name in tiers if "mmproj" in name.lower()], tiers
    mlx = inspect(harness, MLX_35B)
    assert mlx["vision"]["projector"] is None and mlx["vision"]["projector_bytes"] is None


def test_only_loadable_variants_are_ever_marked_recommended(hub_harness):
    harness = hub_harness()
    result = inspect(harness, GGUF_35B)
    for variant in result["variants"]:
        if variant["recommended"]:
            assert variant["loadable"] is True
            assert variant["fit"] == "fits"
            assert variant["name"] == result["recommended_variant"]


def test_results_are_cached_per_repo_sha(hub_harness):
    """Results are cached by repo@sha for 24 h."""
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
    """No short aliases, only full Hugging Face IDs."""
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
    """Grouped by family, then by format."""
    harness = hub_harness()
    response = harness.client.get("/api/admin/catalog")
    assert response.status_code == 200, response.text
    payload = dict(response.json())
    assert payload["families"], "the bundled catalog seed must not be empty"
    assert payload["memory_bytes"] > 0
    for family in payload["families"]:
        assert family["family"] in ("Qwen3.8-27B", "Qwen3.6-35B-A3B", "Qwen3.8-Flash-Next")
        assert family["label"]
        groups = family["groups"]
        assert groups, "each family is grouped by format"
        for group in groups:
            ids = [entry["id"] for entry in group["entries"]]
            assert ids
            assert all("/" in repo_id for repo_id in ids), "Full Hugging Face IDs only"
            assert all(entry["format"] in ("mlx", "gguf") for entry in group["entries"])


def test_model_card_comes_from_the_hub(hub_harness):
    harness = hub_harness()
    response = harness.client.get(f"/api/admin/card?id={GGUF_35B}")
    assert response.status_code == 200, response.text
    card = dict(response.json())
    assert "Fixture model" in card["markdown"]
    assert card["license"] == "apache-2.0"
    assert card["files"], "the detail view lists files with sizes"


def test_a_missing_engine_refuses_rather_than_guessing(client, fake_hub):
    """The API never imports Splash into the manager process, so with
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
    result = inspect(harness, MLX_7BIT)
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
    """HF search finds `mlx-community/Qwen3.8-27B-8bit` compatible (Splash 1.3.1
    loads 8-bit MLX), and `unsloth/Qwen3.8-27B-GGUF` compatible with a recommended
    variant (on the user's 64 GB Mac)."""
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    found = harness.client.get("/api/admin/search", params={"q": "Qwen3.8-27B"}).json()
    ids = {r["id"]: r for r in found["results"]}
    assert "mlx-community/Qwen3.8-27B-8bit" in ids and "unsloth/Qwen3.8-27B-GGUF" in ids
    assert ids["unsloth/Qwen3.8-27B-GGUF"]["format_guess"] == "gguf"

    eight = inspect(harness, "mlx-community/Qwen3.8-27B-8bit")
    assert eight["compatible"] is True and eight["badge"] == "compatible", eight
    assert eight["family"] == "Qwen3.8-27B"

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


def test_a_lone_root_gguf_beside_split_folders_gets_a_variant_splash_resolves():
    """With a split folder beside it, a lone root GGUF can't be asked for by the
    bare ID, and its whole stem matches nothing: the label is the -VARIANT ending
    Splash's `select_gguf` takes for it."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    reference = Path(__file__).resolve().parents[2] / "splash"
    if not (reference / "install" / "upstream.py").exists():
        pytest.skip("no Splash reference clone to resolve against")
    helper = Path(__file__).resolve().parents[1] / "splash_gui" / "helpers"
    files = [
        "Model-Q4_K_M.gguf",
        "mmproj-BF16.gguf",
        "Q8_0/Model-Q8_0-00001-of-00002.gguf",
        "Q8_0/Model-Q8_0-00002-of-00002.gguf",
    ]
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(helper)!r})\n"
        "from install import upstream\n"
        "from inspect_model import variant_label\n"
        "files = json.loads(sys.argv[1])\n"
        "label = variant_label('owner/Model-GGUF', files[0], [files[0]], upstream, files)\n"
        "print(json.dumps([label, upstream.select_gguf(files, label)[0]]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(files)],
        env={"PYTHONPATH": str(reference), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["Q4_K_M", ["Model-Q4_K_M.gguf"]]


def test_an_mtp_head_inside_a_variant_folder_makes_splash_refuse_it():
    """Splash's `split_gguf` leaves out only projectors, so an MTP head in Q8_0/
    is a part that is no part; a folder of MTP heads alone is no variant."""
    from splash_gui.models import catalog as cat

    files: dict[str, int | None] = {
        "Q8_0/Model-Q8_0-00001-of-00002.gguf": 10,
        "Q8_0/Model-Q8_0-00002-of-00002.gguf": 10,
        "Q8_0/mtp-Model-Q8_0.gguf": 1,
        "MTP/mtp-Model-Q8_0.gguf": 1,
        "Q4_0/Model-Q4_0-00001-of-00001.gguf": 5,
    }
    variants = {v.name: v for v in cat.gguf_variants("owner/Model-GGUF", files)}
    assert set(variants) == {"Q8_0", "Q4_0"}
    assert "mtp-Model-Q8_0.gguf" in (variants["Q8_0"].unloadable or "")
    assert variants["Q4_0"].unloadable is None


def test_a_one_gguf_repository_installs_an_id_splash_resolves():
    """Hcompany/Holo4-27B-GGUF has one GGUF, `q4_k_m.gguf`, whose variant the
    table names `q4_k_m`. Splash's `select_gguf` matches no :q4_k_m there, so the
    download is queued under the ID it does resolve (checked on the live
    repository, 2026-10-13)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    from splash_gui.models import catalog as cat

    reference = Path(__file__).resolve().parents[2] / "splash"
    if not (reference / "install" / "upstream.py").exists():
        pytest.skip("no Splash reference clone to resolve against")
    script = (
        "import json, sys\n"
        "from install import upstream\n"
        "files, variant = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n"
        "print(upstream.select_gguf(files, variant)[0][0])\n"
    )
    for files, variant in [
        (["q4_k_m.gguf", "mmproj.f16.gguf"], "q4_k_m"),
        (["Holo4-27B-Q4_K_M.gguf"], "Holo4-27B-Q4_K_M"),
        (["Holo4-27B-Q4_K_M.gguf"], "Q4_K_M"),
    ]:
        model = cat.installable_id("Hcompany/Holo4-27B-GGUF", variant, files)
        chosen = model.partition(":")[2] or None
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(files), json.dumps(chosen)],
            env={"PYTHONPATH": str(reference), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (model, result.stderr)
        assert result.stdout.strip() == files[0]


# --- The likely pick's verdict first, the rest as they come ------------

MANY = "unsloth/Qwen3.8-27B-GGUF"  # the fake Hub lists 8 root GGUFs for it


def stream_inspect(
    harness, model: str, refresh: bool = True
) -> list[tuple[float, str, dict[str, Any]]]:
    """`POST /inspect/stream` as `(seconds since the request, event, data)`."""
    import json
    import time

    start = time.monotonic()
    events: list[tuple[float, str, dict[str, Any]]] = []
    url = f"/api/admin/inspect/stream?id={model}&refresh={1 if refresh else 0}"
    with harness.client.stream("POST", url) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                events.append((time.monotonic() - start, name, json.loads(line[6:])))
    return events


def checked(data: dict[str, Any]) -> dict[str, bool]:
    return {v["name"]: v["loadable"] for v in data["variants"] if v["loadable"] is not None}


def test_the_likely_recommended_variant_is_checked_and_shown_first(hub_harness, monkeypatch):
    """The variant the GUI will recommend for this Mac (here UD-Q4_K_M
    at 64 GB) arrives first, even when its header is the slowest to read, and the
    others fill in after it; the last event is the complete InspectResult."""
    monkeypatch.setenv("FAKE_SPLASH_INSPECT_SECONDS", "0.3,UD-Q4_K_M=0.8")
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    events = stream_inspect(harness, MANY)

    names = [e[1] for e in events]
    assert names[0] == "inspect.progress" and names[-1] == "inspect.result", names
    assert set(names[:-1]) == {"inspect.progress"}, names
    # The snapshot: the whole table at once, nothing checked yet.
    _, _, snapshot = events[0]
    assert snapshot["badge"] == "checking" and snapshot["first_variant"] == "UD-Q4_K_M"
    assert len(snapshot["variants"]) == 9 and checked(snapshot) == {}
    assert sorted(snapshot["pending"]) == sorted(v["name"] for v in snapshot["variants"])

    # The first verdict is the likely pick's, alone, and it already decides the badge.
    progress = [e for e in events if e[1] == "inspect.progress" and checked(e[2])]
    first = progress[0][2]
    assert checked(first) == {"UD-Q4_K_M": True}, checked(first)
    assert first["badge"] == "compatible" and first["recommended_variant"] == "UD-Q4_K_M"
    assert len(first["pending"]) == 8 and "UD-Q4_K_M" not in first["pending"]

    # The rest fill in afterwards, and the stream ends complete.
    assert len(progress) >= 2, "the other variants must arrive as further events"
    result = events[-1][2]
    assert result["pending"] == [] and result["badge"] == "compatible"
    assert result["recommended_variant"] == "UD-Q4_K_M"
    verdicts = checked(result)
    assert len(verdicts) == 9
    # Splash 1.3.2 refuses only a whole unquantized model: UD-Q8_K_XL loads.
    assert verdicts["BF16"] is False and verdicts["imatrix_unsloth"] is False
    assert all(verdicts[n] for n in verdicts if n not in ("BF16", "imatrix_unsloth"))
    # The download plan is for the variant that was checked first.
    assert result["download_plan"]["variant"] is None
    assert any(f["name"].endswith("-UD-Q4_K_M.gguf") for f in result["download_plan"]["files"])

    # GET /inspect now answers from the stored verdicts, with the same content.
    again = inspect(harness, MANY, refresh=False)
    assert again["cached"] is True
    assert checked(again) == verdicts and again["recommended_variant"] == "UD-Q4_K_M"


def test_the_first_verdict_is_delivered_before_the_others_finish(hub_harness, monkeypatch):
    """Incremental delivery, timed where it is produced: the stream's events as the
    manager yields them (TestClient buffers an HTTP stream, so it can't time it)."""
    import time

    monkeypatch.setenv("FAKE_SPLASH_INSPECT_SECONDS", "0.6,UD-Q4_K_M=0.3")
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3

    async def collect() -> list[tuple[float, str, Any]]:
        start = time.monotonic()
        events = await harness.state.models.inspect_stream(MANY, True)
        return [(time.monotonic() - start, name, data) async for name, data in events]

    events = harness.client.portal.call(collect)
    timed = [(t, checked(d.model_dump())) for t, n, d in events if hasattr(d, "variants")]
    at_first = next(t for t, verdicts in timed if verdicts)
    assert next(v for _, v in timed if v) == {"UD-Q4_K_M": True}
    at_end = events[-1][0]
    assert events[-1][1] == "inspect.result"
    # The others take 0.6 s each after the first: the first verdict did not wait.
    assert at_end - at_first >= 0.5, (at_first, at_end)


def test_a_stream_of_a_checked_repository_is_one_result_event(hub_harness):
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    inspect(harness, MANY)
    events = stream_inspect(harness, MANY, refresh=False)
    assert [e[1] for e in events] == ["inspect.result"]
    assert events[0][2]["cached"] is True and events[0][2]["pending"] == []


def test_verdicts_are_stored_per_variant_and_reused(hub_harness, monkeypatch):
    """Checking `REPO:VARIANT` (as a download does) stores that variant's verdict;
    a later check of the whole repository shows it at once and runs Splash only for
    the others."""
    monkeypatch.setenv("FAKE_SPLASH_INSPECT_SECONDS", "0.2")
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    one = inspect(harness, f"{MANY}:Q8_0", refresh=False)
    assert one["compatible"] is True and checked(one) == {"Q8_0": True}

    events = stream_inspect(harness, MANY, refresh=False)
    assert checked(events[0][2]) == {"Q8_0": True}, "the stored verdict is in the snapshot"
    result = events[-1][2]
    assert result["cached"] is False and len(checked(result)) == 9
    # The catalog shows Splash's own verdicts once they are stored.
    catalog = harness.client.get("/api/admin/catalog").json()
    rows = [e for f in catalog["families"] for g in f["groups"] for e in g["entries"]]
    row = next(r for r in rows if r["id"] == MANY)
    by_name = {v["name"]: v["loadable"] for v in row["variants"]}
    assert by_name["Q8_0"] is True and by_name["BF16"] is False


def test_a_waiting_request_and_a_stream_share_one_check(hub_harness, monkeypatch):
    """Two callers of the same `model@sha` join one helper run."""
    import threading

    monkeypatch.setenv("FAKE_SPLASH_INSPECT_SECONDS", "0.3")
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    models = harness.state.models
    seen: list[int] = []
    out: dict[str, Any] = {}

    def wait() -> None:
        out["get"] = harness.client.post(f"/api/admin/inspect?id={MANY}").json()

    waiter = threading.Thread(target=wait)
    waiter.start()
    events = []
    for _ in range(50):
        if models.runs:
            seen.append(len(models.runs))
            events = stream_inspect(harness, MANY, refresh=False)
            break
        import time

        time.sleep(0.05)
    waiter.join(30)
    assert seen == [1], seen
    assert events[0][1] == "inspect.progress"
    assert checked(out["get"]) == checked(events[-1][2])


def test_the_stream_reports_hub_errors_before_it_starts(client, fake_hub):
    point_at_hub(client, fake_hub)
    response = client.post("/api/admin/inspect/stream?id=owner/missing-repo")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "hub_unreachable"


def test_inspect_fit_uses_the_catalog_estimate(hub_harness):
    """`/inspect`'s per-variant and selection fit use the catalog's
    estimate (`cat.memory_need`), not `size + 4 GiB`, so one rule decides fit
    everywhere. Literal values: draft 0.7 GiB + vision 0.9 GiB + KV 0.5 GiB +
    reserve 2 GiB = 4_402_341_477 bytes on top of the weights."""
    from splash_gui.models import catalog as cat

    assert cat.memory_need(16_464_440_224, vision=True) == 20_866_781_701
    assert cat.memory_need(16_464_440_224, vision=False) == 19_900_414_060
    assert cat.fit_for(20_866_781_701, 64 * 1024**3) == "fits"
    assert cat.fit_for(20_866_781_701, 22 * 1024**3) == "wont_fit"  # > 20_401_094_656
    assert cat.fit_for(20_866_781_701, 24 * 1024**3) == "tight"  # > 17_179_869_184
    # Where the rules part: the old `size + 4 GiB` (20_759_407_520) fits at this memory,
    # the catalog's estimate is Tight.
    assert cat.fit_for(20_759_407_520, 29_456_716_292) == "fits"
    assert cat.fit_for(20_866_781_701, 29_456_716_292) == "tight"

    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    result = inspect(harness, MANY)
    sizes = {v["name"]: v["size_bytes"] for v in result["variants"]}
    q4 = sizes["UD-Q4_K_M"]
    assert result["memory_need_bytes"] == q4 + 4_402_341_477, "weights + the catalog's terms"
    # Memory where the two rules disagree: the old `q4 + 4 GiB` fits under
    # `memsize − 8 GiB`, the catalog's estimate does not (it is 0.1 GiB larger).
    harness.state.memory_bytes = lambda: q4 + 12 * 1024**3 + 1
    again = inspect(harness, MANY, refresh=False)
    assert again["cached"] is True
    row = next(v for v in again["variants"] if v["name"] == "UD-Q4_K_M")
    assert row["fit"] == "tight" and again["fit"] == "tight"
    for v in again["variants"]:
        need = v["size_bytes"] + 4_402_341_477
        memory = q4 + 12 * 1024**3 + 1
        expected = (
            "fits"
            if need <= memory - 8 * 1024**3
            else "tight"
            if need <= memory - 3 * 1024**3
            else "wont_fit"
        )
        assert v["fit"] == expected, v
    # The catalog says the same for the same repository at the same memory.
    catalog = harness.client.get("/api/admin/catalog").json()
    rows = [e for f in catalog["families"] for g in f["groups"] for e in g["entries"]]
    listed = {v["name"]: v["fit"] for v in next(r for r in rows if r["id"] == MANY)["variants"]}
    assert all(listed[v["name"]] == v["fit"] for v in again["variants"] if v["name"] in listed)


def test_only_an_explicit_ssd_cache_size_costs_state_staging() -> None:
    """The automatic SSD cache sets its state staging aside only where the model and the
    context already fit, so the estimate leaves it out; a size such as 16G always takes it
    (64 MiB for 35B, 153 MiB for 27B, about half with bf16 states)."""
    from splash_gui.models import catalog as cat

    assert cat.state_staging_bytes("Qwen3.8-27B", None) == 0
    assert cat.state_staging_bytes("Qwen3.8-27B", 0) == 0
    assert cat.state_staging_bytes("Qwen3.8-27B", 16 * 1024**3) == 153 * 1024**2
    assert cat.state_staging_bytes("Qwen3.6-35B-A3B", 16 * 1024**3) == 64 * 1024**2
    assert cat.state_staging_bytes("Qwen3.6-35B-A3B", 16 * 1024**3, "bf16") == 32 * 1024**2
    base = cat.memory_need(16 * 1024**3, vision=False)
    staged = cat.memory_need(16 * 1024**3, vision=False, staging=153 * 1024**2)
    assert staged - base == 153 * 1024**2


# --- Qwen3.8-Flash-Next: split GGUFs in variant folders, GGUF only ------------

FLASH_NEXT = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"
FLASH_NEXT_UNSLOTH = "unsloth/Qwen3.8-Flash-Next-GGUF"
FLASH_NEXT_MLX = "rapid-mlx/Qwen3.8-Flash-Next-4bit"


def test_a_split_repository_lists_each_variant_folder_with_its_parts(hub_harness):
    """Every Flash-Next GGUF on the Hub is split into a folder named after its
    variant; each folder is one variant, checked as :VARIANT, and its download
    plan names every part."""
    harness = hub_harness()
    model = f"{FLASH_NEXT}:Q2_0"
    result = inspect(harness, FLASH_NEXT)
    assert result["compatible"] is True and result["family"] == "Qwen3.8-Flash-Next"
    assert result["format"] == "gguf" and result["draft"] == "incoai/Qwen3.8-Flash-Next-DFlash2"
    by = {v["name"]: v for v in result["variants"]}
    assert set(by) == {"Q2_0", "IQ2_XS"}
    assert by["Q2_0"]["files"] == [
        "Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00001-of-00002.gguf",
        "Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf",
    ]
    assert all(v["loadable"] is True for v in by.values())
    listing = hub_module().repository(FLASH_NEXT)[1]
    assert by["Q2_0"]["size_bytes"] == sum(listing[f].size for f in by["Q2_0"]["files"])
    planned = inspect(harness, model)
    names = [f["name"] for f in planned["download_plan"]["files"]]
    assert by["Q2_0"]["files"][0] in names and by["Q2_0"]["files"][1] in names
    assert "mmproj-BF16.gguf" in names and "mmproj-BF16.gguf" not in [
        f["name"] for f in planned["language_only_plan"]["files"]
    ]


def test_a_root_mtp_head_is_no_variant_and_shadows_its_folder(hub_harness):
    """unsloth keeps `mtp-Qwen3.8-Flash-Next-Q8_0.gguf` at its root, which Splash's
    selection takes for :Q8_0 before the Q8_0/ folder: the head is never listed,
    and the folder is reported unloadable with the file Splash would take."""
    result = inspect(hub_harness(), FLASH_NEXT_UNSLOTH)
    by = {v["name"]: v for v in result["variants"]}
    assert set(by) == {"UD-Q2_K_XL", "Q8_0"}
    assert by["UD-Q2_K_XL"]["loadable"] is True
    assert by["Q8_0"]["loadable"] is False
    assert by["Q8_0"]["reason"] == (
        "Splash takes mtp-Qwen3.8-Flash-Next-Q8_0.gguf for :Q8_0, not the files in Q8_0/"
    )
    assert result["recommended_variant"] == "UD-Q2_K_XL"


def test_a_flash_next_safetensors_checkpoint_is_refused_with_the_plain_line(hub_harness):
    from splash_gui.models import compat

    result = inspect(hub_harness(), FLASH_NEXT_MLX)
    assert result["compatible"] is False and result["badge"] == "incompatible"
    assert result["reason"] == compat.GGUF_ONLY
    assert result["reason_detail"] == "Qwen3.8-Flash-Next loads from a GGUF only"


def test_the_catalog_lists_flash_next_as_gguf_only(hub_harness):
    harness = hub_harness()
    harness.state.memory_bytes = lambda: 64 * 1024**3
    catalog = harness.client.get("/api/admin/catalog").json()
    family = next(f for f in catalog["families"] if f["family"] == "Qwen3.8-Flash-Next")
    assert family["label"] == "Qwen3.8-Flash-Next MoE"
    assert [g["format"] for g in family["groups"]] == ["gguf"]
    rows = {e["id"]: e for g in family["groups"] for e in g["entries"]}
    assert set(rows) == {FLASH_NEXT, FLASH_NEXT_UNSLOTH}
    ista = rows[FLASH_NEXT]
    assert {v["name"] for v in ista["variants"]} == {"Q2_0", "IQ2_XS"}
    assert ista["download_bytes"], "the draft's size is known, so the total is"
    unsloth = {v["name"]: v for v in rows[FLASH_NEXT_UNSLOTH]["variants"]}
    assert unsloth["Q8_0"]["loadable"] is False and "mtp-" in unsloth["Q8_0"]["reason"]


def test_the_catalog_s_selection_rules_match_splash_s(tmp_path):
    """`catalog.root_choice` and the folder rule mirror `install/upstream.py
    select_gguf`, checked against the Splash reference clone."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    from splash_gui.models import catalog as cat

    reference = Path(__file__).resolve().parents[2] / "splash"
    if not (reference / "install" / "upstream.py").exists():
        pytest.skip("no Splash reference clone to resolve against")
    unsloth = [
        "mtp-Qwen3.8-Flash-Next-Q8_0.gguf",
        "mmproj-BF16.gguf",
        "Q8_0/Qwen3.8-Flash-Next-Q8_0-00001-of-00002.gguf",
        "Q8_0/Qwen3.8-Flash-Next-Q8_0-00002-of-00002.gguf",
        "UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00002.gguf",
        "UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00002-of-00002.gguf",
    ]
    cases = [(unsloth, "Q8_0"), (unsloth, "UD-Q2_K_XL"), (unsloth[2:], "Q8_0")]
    script = (
        "import json, sys\n"
        "from install import upstream\n"
        "files, variant = json.loads(sys.argv[1]), sys.argv[2]\n"
        "print(json.dumps(upstream.select_gguf(files, variant)[0]))\n"
    )
    for files, variant in cases:
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(files), variant],
            env={"PYTHONPATH": str(reference), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        chosen = json.loads(result.stdout)
        root = cat.root_choice(files, variant)
        folder = sorted(f for f in files if f.startswith(variant + "/"))
        assert chosen == ([root] if root else folder), (files, variant)
        listed = {v.name: v for v in cat.gguf_variants(FLASH_NEXT_UNSLOTH, dict.fromkeys(files, 1))}
        assert (listed[variant].unloadable is None) == (chosen == folder)


def test_a_gguf_only_refusal_gets_a_plain_line():
    from splash_gui.models import compat

    reason, detail = compat.explain_refusal("Qwen3.8-Flash-Next loads from a GGUF only")
    assert reason == compat.GGUF_ONLY and detail == "Qwen3.8-Flash-Next loads from a GGUF only"


def test_a_split_repository_gets_time_for_every_part():
    from splash_gui.models.service import inspect_timeout

    files: dict[str, int | None] = {
        f"{v}/M-{v}-{k:05d}-of-00003.gguf": 1 for v in ("Q2_0", "Q4_0") for k in (1, 2, 3)
    }
    files["mtp-M-Q8_0.gguf"] = 1
    files["mmproj-BF16.gguf"] = 1
    # Two variants, 4 s for the second, 2 s for each part beyond a variant's first.
    assert inspect_timeout(files, None) == 20.0 + 4.0 + 2.0 * 4
    assert inspect_timeout(files, "Q2_0") == 20.0 + 2.0 * 2
    assert inspect_timeout({"a.gguf": 1, "b.gguf": 1}, None) == 24.0
