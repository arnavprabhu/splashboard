"""Contract tests against the installed Homebrew Splash (SPEC §20.2, `make test-real`).

These check the assumptions the rest of the suite takes on faith:

  * the engine is 1.3.x, inside the range v1 supports (SPEC §6.1);
  * `install/serve_options.py` still matches Appendix A, including that no
    option has appeared that we do not map (SPEC §8.4);
  * `helpers/inspect_model.py` really runs under Splash's own bundled Python and
    its screening agrees with ours on live repositories — an MLX 4-bit group-64
    checkpoint is compatible, an 8-bit one is not, and an unsupported
    architecture is refused (SPEC §9.2);
  * `install/models.py` still takes the command line we build (SPEC §9.4).

Screening a repository reads `config.json` and the shard index over HTTP; it
never downloads weights, so this stays cheap. Nothing here starts an engine, so
no GPU and no model are needed. Tests that *do* need weights are marked and
skipped unless `SPLASH_GUI_DOWNLOAD=1` is set.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import HAVE_SPLASH, SPLASH_PKG, SPLASH_PYTHON

# `make test` collects these but they all skip; `make test-real` runs them.
pytestmark = pytest.mark.real

# One of each outcome, all in families Splash supports (SPEC §3.2).
MLX_4BIT = "mlx-community/Qwen3.6-35B-A3B-4bit"
MLX_8BIT = "mlx-community/Qwen3.6-35B-A3B-8bit"
GGUF_REPO = "unsloth/Qwen3.6-35B-A3B-GGUF"
# Open, so the check reaches the architecture screen rather than an auth wall.
UNSUPPORTED = "mistralai/Mistral-7B-Instruct-v0.3"

needs_engine = pytest.mark.skipif(
    not HAVE_SPLASH, reason="Splash 1.3.0 is not installed via Homebrew"
)
needs_network = pytest.mark.usefixtures("allow_network")


@pytest.fixture
def engine_python() -> Path:
    assert SPLASH_PYTHON.exists(), SPLASH_PYTHON
    return SPLASH_PYTHON


def inspect(repo_id: str, engine_python: Path, variant: str | None = None) -> dict[str, Any]:
    """Run our compatibility helper exactly as the manager does, on a live repo."""

    files = _listing(repo_id)
    sha = _sha(repo_id)
    proc = subprocess.run(
        [
            str(engine_python),
            str(Path(__file__).resolve().parents[1] / "splash_gui/helpers/inspect_model.py"),
        ],
        input=json.dumps(
            {"repo": repo_id, "sha": sha, "files": files, "variant": variant}
        ).encode(),
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(SPLASH_PKG)},
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
    # One JSON line for the table, then one per verdict (D59).
    lines = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    return {
        "variants": lines[0]["variants"],
        "first": lines[0]["first"],
        "results": [line["result"] for line in lines[1:]],
    }


def _listing(repo_id: str) -> dict[str, int | None]:
    import httpx

    response = httpx.get(
        f"https://huggingface.co/api/models/{repo_id}", params={"blobs": "true"}, timeout=30
    )
    response.raise_for_status()
    out: dict[str, int | None] = {}
    for sibling in response.json().get("siblings") or []:
        name = sibling.get("rfilename")
        if isinstance(name, str):
            out[name] = sibling.get("size")
    return out


def _sha(repo_id: str) -> str:
    import httpx

    response = httpx.get(f"https://huggingface.co/api/models/{repo_id}", timeout=30)
    response.raise_for_status()
    return str(response.json()["sha"])


# --- engine identity and serve options -------------------------------------------


@needs_engine
def test_the_installed_engine_is_in_the_supported_range() -> None:
    from splash_gui.engine import discovery as d

    info = d.discover(env={"PATH": "/usr/bin:/bin"})
    assert info.found and info.source == "brew"
    assert info.version is not None and info.version.startswith("1.3."), info.version
    assert info.support == "supported", info.version
    assert info.pkg is not None and info.pkg.resolve() == SPLASH_PKG.resolve()
    assert info.python is not None and info.python.exists()


@needs_engine
def test_serve_options_still_match_appendix_a() -> None:
    """SPEC §8.4: an option Appendix A does not know must surface, not vanish."""
    from splash_gui.engine.discovery import discover
    from splash_gui.engine.serve_options import EngineOptionsCache

    options = EngineOptionsCache().get(discover(env={"PATH": "/usr/bin:/bin"}))
    assert options.available, options.errors
    assert options.unknown() == [], f"Appendix A is missing: {options.unknown()}"


@needs_engine
def test_the_installer_takes_the_command_line_we_build() -> None:
    """SPEC §9.4: the argv must be the real installer's, flags and all."""
    result = subprocess.run(
        [str(SPLASH_PYTHON), str(SPLASH_PKG / "install" / "models.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--model", "--revision", "--draft-model", "--language-only"):
        assert flag in result.stdout, f"{flag} is missing from install/models.py"
    for action in ("prepare", "verify", "link"):
        assert action in result.stdout, f"the {action} action is missing"


@needs_engine
def test_the_bundle_carries_the_modules_the_helper_imports() -> None:
    """`helpers/inspect_model.py` imports these; a rename must fail here, not in use."""
    for module in (
        "install/families.py",
        "install/upstream.py",
        "install/hub.py",
        "install/models.py",
    ):
        assert (SPLASH_PKG / module).is_file(), module
    for module in ("server/serve_options.py", "server/server.py"):
        assert (SPLASH_PKG / module).is_file(), module


# --- compatibility screening against live repositories ---------------------------


@needs_engine
@needs_network
def test_a_real_4bit_mlx_checkpoint_is_compatible(engine_python: Path) -> None:
    result = inspect(MLX_4BIT, engine_python)
    assert result["results"], result
    compatible = [r for r in result["results"] if r["compatible"]]
    assert compatible, [r.get("reason") for r in result["results"]]
    assert compatible[0]["family"] == "Qwen3.6-35B-A3B"
    assert compatible[0]["format"] == "mlx"


@needs_engine
@needs_network
def test_a_real_8bit_checkpoint_is_refused_with_the_engine_s_reason(engine_python: Path) -> None:
    """SPEC §3.2: MLX must be affine 4-bit group 64. This is the demo checklist item."""
    result = inspect(MLX_8BIT, engine_python)
    assert not any(r["compatible"] for r in result["results"]), result
    reasons = [r.get("reason") or "" for r in result["results"]]
    # Splash 1.3.0's engine model-check names the first tensor whose bits differ.
    assert any(reason.endswith("bits mismatch: MLX 8, runtime 4") for reason in reasons), reasons


@needs_engine
@needs_network
def test_an_unsupported_architecture_is_refused(engine_python: Path) -> None:
    result = inspect(UNSUPPORTED, engine_python)
    assert not any(r["compatible"] for r in result["results"]), result
    reasons = [r.get("reason") or "" for r in result["results"]]
    assert any("supported" in reason or "architecture" in reason for reason in reasons), reasons


@needs_engine
@needs_network
def test_a_real_gguf_repo_lists_its_variants_and_a_loadable_one(engine_python: Path) -> None:
    """SPEC §9.1: every root .gguf is a variant row, at least one of them loadable."""
    result = inspect(GGUF_REPO, engine_python)
    assert result["variants"], result
    loadable = [r for r in result["results"] if r["compatible"]]
    assert loadable, [r.get("reason") for r in result["results"]]
    assert loadable[0]["format"] == "gguf"
    assert loadable[0]["vision"] is True, "the draft repo's projector must be usable"
    names = {v["name"] for v in result["variants"]}
    assert "UD-Q2_K_XL" in names, f"expected the recommended small variant in {sorted(names)}"
    # Every reported variant must be an actual file in the repository.
    listing = _listing(GGUF_REPO)
    for variant in result["variants"]:
        assert any(name.endswith(variant["files"][0]) for name in listing), variant


@needs_engine
@needs_network
def test_a_bf16_gguf_variant_is_reported_unloadable(engine_python: Path) -> None:
    """SPEC §3.2 / §9.1: Splash has no BF16 kernels; such a row must be disabled."""
    repo = "unsloth/Qwen3.8-27B-GGUF"
    result = inspect(repo, engine_python, variant="BF16")
    assert not any(r["compatible"] for r in result["results"]), result


@needs_engine
@needs_network
def test_ud_q8_k_xl_is_reported_unloadable(engine_python: Path) -> None:
    """SPEC §3.2: `UD-Q8_K_XL` is explicitly not supported."""
    result = inspect(GGUF_REPO, engine_python, variant="UD-Q8_K_XL")
    assert not any(r["compatible"] for r in result["results"]), result


@needs_engine
@needs_network
def test_a_single_gguf_repository_needs_no_variant(engine_python: Path) -> None:
    """The case fixed in `helpers/inspect_model.py`: one root GGUF, no :VARIANT."""
    listing = _listing("prism-ml/Ternary-Bonsai-2-27B-gguf")
    roots = [
        name
        for name in listing
        if "/" not in name and name.endswith(".gguf") and "mmproj" not in name.lower()
    ]
    if len(roots) != 1:
        pytest.skip(f"that repository now has {len(roots)} root GGUFs")
    result = inspect("prism-ml/Ternary-Bonsai-2-27B-gguf", engine_python)
    assert any(r["compatible"] for r in result["results"]), result


# --- the manager's own view, over the real Hub ------------------------------------


@needs_network
def test_the_managers_search_and_card_agree_with_the_hub(tmp_path: Path) -> None:
    """D7/D22: search returns full IDs; the card is the repository's README."""
    import asyncio

    from splash_gui.models.hf import HfClient, HubError, RepoInfo
    from splash_gui.paths import Paths
    from splash_gui.secrets import MemoryBackend, SecretStore
    from splash_gui.settings.store import SettingsStore

    paths = Paths(tmp_path / "home").ensure()
    store = SettingsStore(paths)
    store.load()
    from splash_gui.state import ManagerState

    state = ManagerState(
        paths=paths, settings=store, secrets=SecretStore(MemoryBackend()), web_dist=tmp_path
    )
    client = HfClient(state)

    async def search_and_card() -> tuple[list[dict[str, Any]], str, RepoInfo]:
        return (
            await client.search("Qwen3.6-35B-A3B-GGUF", "downloads", 10),
            await client.readme(GGUF_REPO),
            await client.repo_info(GGUF_REPO),
        )

    async def missing() -> None:
        await client.repo_info("owner/definitely-not-a-real-repository-xyz")

    with pytest.raises(HubError) as caught:
        asyncio.run(missing())
    # Hugging Face answers 401 rather than 404 when it will not confirm whether a
    # private repository exists; either way the manager must not say "compatible".
    assert caught.value.status in (401, 404)

    rows, readme, info = asyncio.run(search_and_card())
    assert rows, "search must return something for a supported model"
    assert all("/" in str(row.get("id")) for row in rows), "D22: full IDs only"
    assert any(row.get("id") == GGUF_REPO for row in rows), [row.get("id") for row in rows]
    assert readme.strip(), "the model card must not be empty"
    assert info.sha and len(info.sha) >= 7
    assert info.files, "the variant table is built from these"
    assert any(name.endswith(".gguf") for name in info.files), sorted(info.files)[:8]
    assert info.license, "SPEC §9.1 shows the license from the model card"


# --- SPEC §21 acceptance, through the manager's own API ---------------------------


@needs_engine
@needs_network
def test_acceptance_search_verdicts_on_the_live_hub(
    paths: Any, secrets: Any, web_dist: Path
) -> None:
    """HF search marks `mlx-community/Qwen3.8-27B-8bit` incompatible with the reason
    and `unsloth/Qwen3.8-27B-GGUF` compatible with a recommended variant, using the
    installed Splash and the live Hub (64 GB, the owner's Mac)."""
    from fastapi.testclient import TestClient

    from splash_gui.app import AppConfig, create_app

    app = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    app.state.manager.memory_bytes = lambda: 64 * 1024**3
    with TestClient(app, client=("127.0.0.1", 5)) as client:
        headers = {"Authorization": f"Bearer {app.state.manager.auth.cli_token()}"}
        found = client.get(
            "/api/admin/search", params={"q": "Qwen3.8-27B", "limit": 100}, headers=headers
        )
        assert found.status_code == 200, found.text
        eight = client.post(
            "/api/admin/inspect", params={"id": "mlx-community/Qwen3.8-27B-8bit"}, headers=headers
        )
        if eight.status_code == 404:
            pytest.skip("mlx-community/Qwen3.8-27B-8bit is not on the Hub")
        assert eight.status_code == 200, eight.text
        body = eight.json()
        assert body["compatible"] is False and body["badge"] == "incompatible"
        assert body["reason"] == "Splash runs MLX models only at 4-bit, group size 64.", body
        assert body["reason_detail"].endswith("bits mismatch: MLX 8, runtime 4"), body
        response = client.post(
            "/api/admin/inspect", params={"id": "unsloth/Qwen3.8-27B-GGUF"}, headers=headers
        )
        assert response.status_code == 200, response.text
        gguf = response.json()
        assert gguf["compatible"] is True and gguf["family"] == "Qwen3.8-27B", gguf
        assert gguf["recommended_variant"], [v["name"] for v in gguf["variants"]]
        for variant in gguf["variants"]:
            if variant["name"] in ("UD-Q8_K_XL", "BF16"):
                assert variant["loadable"] is False, variant


@needs_engine
def test_discovery_reaches_the_client_configurator_in_the_homebrew_package() -> None:
    """`GET /integrations/{claude,codex,opencode}/print` is `exact` only when discovery
    finds `install/clients.py` and the bundled Python (SPEC §11.2). The real-manager
    Playwright test sees `exact: false` because the *fake* engine ships no
    clients.py; this pins that the Homebrew layout is found and the helper runs."""
    from splash_gui.engine.discovery import discover

    info = discover(env={"PATH": "/usr/bin:/bin"})
    assert info.found and info.source == "brew", info
    assert info.pkg is not None and info.python == SPLASH_PYTHON
    assert info.install_dir is not None and (info.install_dir / "clients.py").is_file()
    helper = Path(__file__).parents[1] / "splash_gui" / "helpers" / "launch_client.py"
    spec = {
        "client": "claude",
        "model": f"{MLX_4BIT}:no-think",
        "url": "http://127.0.0.1:8000",
        "context": 262144,
        "modalities": ["text"],
        "args": [],
        "print": True,
        "format": "json",
    }
    result = subprocess.run(
        [str(info.python), str(helper)],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "PYTHONPATH": str(info.pkg),
            "SPLASH_GUI_CLIENT_SPEC": json.dumps(spec),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["argv"][0] == "claude"
    assert data["env"]["ANTHROPIC_MODEL"] == spec["model"]
