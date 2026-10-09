"""The fake install/models.py: layout, progress, SIGTERM/partials, failures."""

from __future__ import annotations

import json
import os
import re
import signal
import time
from pathlib import Path

from harness import run_installer

GGUF = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
MLX = "mlx-community/Qwen3.8-27B-4bit"


def install(base: Path, *args: str, env: dict | None = None, timeout: float = 60) -> tuple[int, str, str]:
    process = run_installer(base, ["--models", str(base / "links"), *args], env=env)
    out, err = process.communicate(timeout=timeout)
    return process.returncode, out, err


def repo_dir(base: Path, repo: str) -> Path:
    return base / "models" / ("models--" + repo.replace("/", "--"))


def test_prepare_writes_hub_layout_and_assembly(tmp_path):
    code, out, err = install(tmp_path, "--model", MLX, "prepare")
    assert code == 0, err
    assert (
        "Installing mlx-community/Qwen3.8-27B-4bit as Qwen3.8-27B (mlx-affine); "
        "draft incoai/Qwen3.8-27B-DFlash2; vision enabled." in out
    )
    assert "Fetching 7 file(s), " in out
    folder = repo_dir(tmp_path, MLX)
    commit = (folder / "refs" / "main").read_text()
    assert len(commit) == 40
    snapshot = folder / "snapshots" / commit
    config = json.loads((snapshot / "config.json").read_text())
    assert config["quantization"] == {"group_size": 64, "bits": 4, "mode": "affine"}
    assert config["text_config"]["model_type"] == "qwen3_5_text"
    assert all((snapshot / n).is_symlink() for n in ("config.json", "model-00001-of-00002.safetensors"))
    assert not list((folder / "blobs").glob("*.incomplete"))
    link = tmp_path / "links" / "mlx-community" / "Qwen3.8-27B-4bit"
    assert link.is_symlink() and link.resolve().parent.name == ".resolved"
    record = json.loads((link / "model.json").read_text())
    assert record["sources"]["target"] == {"repo": MLX, "revision": commit}
    assert record["sources"]["draft"]["repo"] == "incoai/Qwen3.8-27B-DFlash2"
    assert record["target_format"] == "mlx-affine" and record["vision_format"] == "safetensors"
    assert set(record["files"]["draft/model.safetensors"]) == {"path", "bytes", "mtime_ns", "ctime_ns", "digest"}
    assert (repo_dir(tmp_path, "incoai/Qwen3.8-27B-DFlash2") / "refs" / "main").exists()


def test_link_verify_and_reinstall_messages(tmp_path):
    code, out, _ = install(tmp_path, "--model", GGUF, "link")
    assert code == 0 and out.strip() == str(
        (tmp_path / "links").resolve() / "unsloth" / "Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
    )
    code, _, err = install(tmp_path, "--model", GGUF, "verify")
    assert code == 1 and err.strip() == f"error: {GGUF} is not installed in {tmp_path / 'links'}"
    assert install(tmp_path, "--model", GGUF, "prepare")[0] == 0
    code, out, _ = install(tmp_path, "--model", GGUF, "verify", "--full")
    assert code == 0 and out.strip() == f"Splash model {GGUF} preflight passed (full)."
    code, out, _ = install(tmp_path, "--model", GGUF, "prepare")
    assert code == 0 and out.startswith(f"Splash model {GGUF} is already installed in ")
    code, out, _ = install(tmp_path, "--model", GGUF, "--language-only", "link")
    assert "/.selections/" in out


def test_update_moves_commit(tmp_path):
    assert install(tmp_path, "--model", GGUF, "prepare")[0] == 0
    code, out, _ = install(tmp_path, "--model", GGUF, "prepare", env={"FAKE_SPLASH_DL_COMMIT_SALT": "v2"})
    assert code == 0 and "unsloth/Qwen3.6-35B-A3B-GGUF moved from " in out
    # Only the changed weight file is fetched; mmproj and the draft are reused.
    assert "Fetching 1 file(s), 0.00 GB, from unsloth/Qwen3.6-35B-A3B-GGUF@" in out
    assert "from incoai/Qwen3.6-35B-A3B-DFlash2@" not in out
    assert len(list((repo_dir(tmp_path, "unsloth/Qwen3.6-35B-A3B-GGUF") / "snapshots").iterdir())) == 2


def test_failed_update_keeps_installed_commit(tmp_path):
    """upstream.py _keeping_installation: the old commit stays and serves."""
    assert install(tmp_path, "--model", GGUF, "prepare")[0] == 0
    link = tmp_path / "links" / GGUF
    before = (link / "model.json").read_text()
    code, out, err = install(
        tmp_path,
        "--model",
        GGUF,
        "prepare",
        env={"FAKE_SPLASH_DL_COMMIT_SALT": "v2", "FAKE_SPLASH_DL_FAIL": "network_mid"},
    )
    assert code == 0, err
    assert " moved from " in out
    assert re.search(
        r"^Warning: keeping the installed unsloth/Qwen3\.6-35B-A3B-GGUF@[0-9a-f]{12}; cannot install "
        r"unsloth/Qwen3\.6-35B-A3B-GGUF@[0-9a-f]{40}: cannot install .*peer closed connection",
        err,
        re.MULTILINE,
    )
    assert (link / "model.json").read_text() == before
    code, _, err = install(
        tmp_path,
        "--model",
        GGUF,
        "prepare",
        env={"FAKE_SPLASH_DL_COMMIT_SALT": "v3", "FAKE_SPLASH_DL_FAIL": "incompatible"},
    )
    assert code == 0 and "Warning: keeping the installed " in err


def _sigterm_mid_file(tmp_path: Path, env: dict) -> tuple[Path, Path, int]:
    """Start `prepare`, watch one partial blob grow, SIGTERM the process group."""
    process = run_installer(tmp_path, ["--models", str(tmp_path / "links"), "--model", GGUF, "prepare"], env=env)
    blobs = repo_dir(tmp_path, "unsloth/Qwen3.6-35B-A3B-GGUF") / "blobs"
    sizes: list[int] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(sizes) < 3:
        partial = list(blobs.glob("*.incomplete")) if blobs.exists() else []
        if partial:
            sizes.append(partial[0].stat().st_size)
        time.sleep(0.25)
    assert len(sizes) == 3 and sizes[0] < sizes[1] < sizes[2], sizes
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(5)
    assert process.returncode == -signal.SIGTERM
    partial = list(blobs.glob("*.incomplete"))
    assert len(partial) == 1
    kept = partial[0].stat().st_size
    assert 0 < kept < 4 << 20
    return blobs, partial[0], kept


def test_progress_sigterm_keeps_a_uuid_partial_that_is_never_resumed(tmp_path):
    """huggingface_hub 1.28 (Splash 1.2.0): `<hash>.<uuid8>.incomplete`, a new one per run."""
    env = {"FAKE_SPLASH_DL_BPS": "2M", "FAKE_SPLASH_DL_SHARD_BYTES": "4M"}
    blobs, stale, kept = _sigterm_mid_file(tmp_path, env)
    assert re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{8}\.incomplete", stale.name), stale.name
    code, _, err = install(tmp_path, "--model", GGUF, "prepare", env={**env, "FAKE_SPLASH_DL_BPS": "64M"})
    assert code == 0, err
    final = blobs / stale.name.split(".")[0]
    assert final.stat().st_size == 4 << 20
    assert stale.exists() and stale.stat().st_size == kept, "the killed run's partial is left behind"
    assert list(blobs.glob("*.incomplete")) == [stale]


def test_legacy_hub_resumes_the_incomplete_blob(tmp_path):
    env = {"FAKE_SPLASH_DL_BPS": "2M", "FAKE_SPLASH_DL_SHARD_BYTES": "4M", "FAKE_SPLASH_DL_HUB": "legacy"}
    blobs, partial, _ = _sigterm_mid_file(tmp_path, env)
    assert re.fullmatch(r"[0-9a-f]{64}\.incomplete", partial.name), partial.name
    started = time.monotonic()
    code, _, err = install(tmp_path, "--model", GGUF, "prepare", env={**env, "FAKE_SPLASH_DL_BPS": "64M"})
    assert code == 0, err
    assert not list(blobs.glob("*.incomplete"))
    assert (blobs / partial.name.removesuffix(".incomplete")).stat().st_size == 4 << 20
    assert time.monotonic() - started < 5
    assert install(tmp_path, "--model", GGUF, "verify", "--full", env=env)[0] == 0


def test_sigint_exits_130(tmp_path):
    env = {"FAKE_SPLASH_DL_BPS": "1M", "FAKE_SPLASH_DL_SHARD_BYTES": "8M"}
    process = run_installer(tmp_path, ["--models", str(tmp_path / "links"), "--model", GGUF, "prepare"], env=env)
    time.sleep(0.8)
    os.killpg(process.pid, signal.SIGINT)
    assert process.wait(5) == 130


def test_gated_needs_token(tmp_path):
    code, _, err = install(tmp_path, "--model", MLX, "prepare", env={"FAKE_SPLASH_DL_FAIL": "gated"})
    assert code == 1
    assert err.startswith(f"error: cannot resolve {MLX}: 401 Client Error.")
    assert "set HF_TOKEN or run 'hf auth login' with access to this repository" in err
    assert "neither this installation nor the Hub cache records a commit for the default branch" in err
    code, _, err = install(
        tmp_path, "--model", MLX, "prepare", env={"FAKE_SPLASH_DL_FAIL": "gated", "HF_TOKEN": "hf_x"}
    )
    assert code == 0, err


def test_network_failure_before_and_after_install(tmp_path):
    code, _, err = install(tmp_path, "--model", GGUF, "prepare", env={"FAKE_SPLASH_DL_FAIL": "network"})
    assert code == 1 and err.startswith("error: cannot resolve unsloth/Qwen3.6-35B-A3B-GGUF: [Errno 8]")
    assert install(tmp_path, "--model", GGUF, "prepare")[0] == 0
    code, out, _ = install(tmp_path, "--model", GGUF, "prepare", env={"FAKE_SPLASH_DL_FAIL": "network"})
    assert code == 0
    assert (
        "Could not reach the Hub ([Errno 8] nodename nor servname provided, or not known); using the installed " in out
    )


def test_network_mid_download_and_disk_full(tmp_path):
    code, _, err = install(tmp_path, "--model", GGUF, "prepare", env={"FAKE_SPLASH_DL_FAIL": "network_mid"})
    assert code == 1 and "peer closed connection without sending complete message body" in err
    code, _, err = install(tmp_path / "b", "--model", GGUF, "prepare", env={"FAKE_SPLASH_DL_FAIL": "disk_full"})
    assert code == 1 and err.strip() == f"error: cannot install {GGUF}: [Errno 28] No space left on device"
    blobs = repo_dir(tmp_path / "b", "unsloth/Qwen3.6-35B-A3B-GGUF") / "blobs"
    # huggingface_hub 1.28 deletes its per-run partial when the download fails;
    # older hubs kept `<hash>.incomplete` for the next run.
    assert not list(blobs.glob("*.incomplete"))
    legacy = {"FAKE_SPLASH_DL_FAIL": "disk_full", "FAKE_SPLASH_DL_HUB": "legacy"}
    code, _, _ = install(tmp_path / "c", "--model", GGUF, "prepare", env=legacy)
    assert code == 1
    assert list((repo_dir(tmp_path / "c", "unsloth/Qwen3.6-35B-A3B-GGUF") / "blobs").glob("*.incomplete"))


def test_incompatible(tmp_path):
    code, _, err = install(tmp_path, "--model", "meta-llama/Llama-3.1-8B", "prepare")
    assert code == 1
    assert "no supported model has this architecture" in err
    assert "supported: Qwen3.8-27B, Qwen3.6-35B-A3B" in err
    code, _, err = install(tmp_path, "--model", MLX, "prepare", env={"FAKE_SPLASH_DL_FAIL": "incompatible"})
    assert code == 1


def test_offline(tmp_path):
    code, _, err = install(tmp_path, "--model", GGUF, "prepare", env={"HF_HUB_OFFLINE": "1"})
    assert code == 1 and "HF_HUB_OFFLINE is set" in err
    assert install(tmp_path, "--model", GGUF, "prepare")[0] == 0
    code, out, _ = install(tmp_path, "--model", GGUF, "prepare", env={"HF_HUB_OFFLINE": "1"})
    assert code == 0 and "is already installed in" in out


def test_invalid_model_id(tmp_path):
    code, _, err = install(tmp_path, "--model", "qwen", "prepare")
    assert code == 2 and "model must be a full Hugging Face repository ID (owner/repo)" in err
