"""Behaviour Splash 1.3.1 changed that the GUI reads: Splash packages are refused,
MLX checkpoints load in more quantizations, and `/status` reports the thermal state."""

from __future__ import annotations

import json

import pytest
from test_installer import install

from install import models, upstream

PACKAGE = "incoai/Qwen3.6-35B-A3B-Splash"


def test_a_splash_package_is_refused_with_its_mlx_model(tmp_path):
    code, _, err = install(tmp_path, "--model", PACKAGE, "prepare")
    assert code == 1
    assert err.strip() == (
        f"error: {PACKAGE} is a Splash package, which Splash no longer loads; serve the MLX "
        "model of its family instead: splash serve --model mlx-community/Qwen3.6-35B-A3B-4bit"
    )


def test_an_installed_splash_package_is_refused_and_its_files_named(tmp_path):
    snapshot = tmp_path / "models" / "models--incoai--Qwen3.8-27B-Splash" / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text(json.dumps({"format": {"name": "splash-packed-q4"}}))
    link = tmp_path / "links" / "incoai" / "Qwen3.8-27B-Splash"
    link.parent.mkdir(parents=True)
    link.symlink_to(snapshot)
    for action in ("prepare", "verify"):
        code, _, err = install(tmp_path, "--model", "incoai/Qwen3.8-27B-Splash", action)
        assert code == 1
        assert f"(its files in {snapshot.parent.parent} can be deleted)" in err
        assert "splash serve --model mlx-community/Qwen3.8-27B-4bit" in err


def test_another_tools_manifest_passes():
    models.refuse_package("someone/model", {"format": "gguf"})
    models.refuse_package("someone/model", {"schema_version": 1})


class Repo:
    def __init__(self, quantization):
        self.files = {"config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json"}
        self.config = {"text_config": {}, "quantization": quantization}

    def json(self, name):
        return self.config


@pytest.mark.parametrize(
    "quantization",
    [
        {"bits": 4, "group_size": 64, "mode": "affine"},
        {"bits": 8, "group_size": 64},
        {"bits": 2, "group_size": 128},
        {"bits": 4, "group_size": 32, "mode": "mxfp4"},
        # Mixed precision: a module's entry takes its mode's default for what it omits.
        {"bits": 4, "group_size": 64, "language_model.model.embed_tokens": {"bits": 6}},
    ],
)
def test_mlx_quantizations_splash_1_3_1_loads(quantization, monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "check_model", lambda *a, **k: "family")
    upstream._mlx_target(Repo(quantization), False, tmp_path)


@pytest.mark.parametrize(
    ("quantization", "message"),
    [
        (None, "this model requires an MLX checkpoint (affine 2, 3, 4, 5, 6 or 8 bits"),
        ({"bits": 7, "group_size": 64}, "quantization is affine 7-bit in groups of 64; MLX weights load as"),
        ({"bits": 4, "group_size": 16}, "quantization is affine 4-bit in groups of 16;"),
        ({"bits": 4, "group_size": 64, "mode": "mxfp4"}, "quantization is mxfp4 4-bit in groups of 64;"),
        ({"bits": 4, "group_size": 64, "lm_head": {"bits": 7}}, "quantization lm_head is affine 7-bit"),
    ],
)
def test_mlx_quantizations_splash_refuses(quantization, message, monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "check_model", lambda *a, **k: "family")
    with pytest.raises(models.ModelError, match=message.replace("(", r"\(")):
        upstream._mlx_target(Repo(quantization), False, tmp_path)


def test_status_reports_the_thermal_state(make_engine, tmp_path):
    assert make_engine(base=tmp_path / "cool").json("GET", "/status")[1]["thermal_state"] == "nominal"
    hot = make_engine(base=tmp_path / "hot", env={"FAKE_SPLASH_THERMAL_STATE": "serious"})
    code, status = hot.json("GET", "/status")
    assert code == 200 and status["thermal_state"] == "serious" and status["ready"] is True
    metrics = hot.request("GET", "/metrics")[2].decode()
    assert 'splash_thermal_state{state="serious"} 1' in metrics
