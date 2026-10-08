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


def test_inspect_rows_carry_their_quality_tier_and_the_projector_is_labelled(hub_harness):
    """SPEC §9.1 (D98): a variant row has its quality tier (a dash for one Splash cannot load).
    The vision projector is named on `vision` and never listed as a variant; an MLX
    repository has none."""
    harness = hub_harness()
    result = inspect(harness, MANY)
    tiers = {v["name"]: v["quality"] for v in result["variants"]}
    assert tiers["UD-Q4_K_M"] == "Balanced"
    assert tiers["UD-IQ2_XXS"] == "Compact"
    assert tiers["Q8_0"] == "Highest"
    assert tiers["UD-Q8_K_XL"] is None
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


# --- D59 (Q16): the likely pick's verdict first, the rest as they come ------------

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
    """Q16: the variant the GUI will recommend for this Mac (SPEC §9.1, here UD-Q4_K_M
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
    assert len(snapshot["variants"]) == 8 and checked(snapshot) == {}
    assert sorted(snapshot["pending"]) == sorted(v["name"] for v in snapshot["variants"])

    # The first verdict is the likely pick's, alone, and it already decides the badge.
    progress = [e for e in events if e[1] == "inspect.progress" and checked(e[2])]
    first = progress[0][2]
    assert checked(first) == {"UD-Q4_K_M": True}, checked(first)
    assert first["badge"] == "compatible" and first["recommended_variant"] == "UD-Q4_K_M"
    assert len(first["pending"]) == 7 and "UD-Q4_K_M" not in first["pending"]

    # The rest fill in afterwards, and the stream ends complete.
    assert len(progress) >= 2, "the other variants must arrive as further events"
    result = events[-1][2]
    assert result["pending"] == [] and result["badge"] == "compatible"
    assert result["recommended_variant"] == "UD-Q4_K_M"
    verdicts = checked(result)
    assert len(verdicts) == 8
    assert verdicts["UD-Q8_K_XL"] is False and verdicts["imatrix_unsloth"] is False
    assert all(verdicts[n] for n in verdicts if n not in ("UD-Q8_K_XL", "imatrix_unsloth"))
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
    assert result["cached"] is False and len(checked(result)) == 8
    # The catalog shows Splash's own verdicts once they are stored.
    catalog = harness.client.get("/api/admin/catalog").json()
    rows = [e for f in catalog["families"] for g in f["groups"] for e in g["entries"]]
    row = next(r for r in rows if r["id"] == MANY)
    by_name = {v["name"]: v["loadable"] for v in row["variants"]}
    assert by_name["Q8_0"] is True and by_name["UD-Q8_K_XL"] is False


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
    """D59 (b): `/inspect`'s per-variant and selection fit use the catalog's §9.1
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
