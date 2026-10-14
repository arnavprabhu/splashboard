"""NVFP4 and FP8 safetensors checkpoints: their labels, search and the check's timeout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from splash_gui.models import checkpoint as cp
from splash_gui.models.api import search_row
from splash_gui.models.layout import FileRef, Selection
from splash_gui.models.service import SAFETENSORS_TIMEOUT_S, inspect_timeout, installed_checkpoint

# The shapes of the real configs (trimmed), read from the Hub on 2026-10-14.
NVIDIA_27B = {
    "quantization_config": {
        "quant_method": "modelopt",
        "quant_algo": "MIXED_PRECISION",
        "quantized_layers": {
            "model.language_model.layers.0.linear_attn.in_proj_qkv": {"quant_algo": "FP8"},
            "model.language_model.layers.0.mlp.down_proj": {
                "quant_algo": "NVFP4",
                "group_size": 16,
            },
        },
        "ignore": ["lm_head"],
    }
}
UNSLOTH_27B = {
    "quantization_config": {
        "quant_method": "compressed-tensors",
        "format": "mixed-precision",
        "config_groups": {
            "group_0": {"format": "float-quantized", "weights": {"num_bits": 8}},
            "group_1": {"format": "nvfp4-pack-quantized", "weights": {"num_bits": 4}},
        },
    }
}
MLX_NVFP4 = {"quantization": {"group_size": 16, "bits": 4, "mode": "nvfp4"}}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (NVIDIA_27B, "NVFP4 · Model Optimizer"),
        (UNSLOTH_27B, "NVFP4 · compressed-tensors"),
        (
            {"quantization_config": {"quant_method": "modelopt", "quant_algo": "FP8"}},
            "FP8 · Model Optimizer",
        ),
        (
            {
                "quantization_config": {
                    "quant_method": "compressed-tensors",
                    "format": "float-quantized",
                    "config_groups": {"group_0": {"weights": {"num_bits": 8}}},
                }
            },
            "FP8 · compressed-tensors",
        ),
        # A method Splash refuses still names the library; the kind is unknown.
        (
            {"quantization_config": {"quant_method": "modelopt", "quant_algo": "INT4_AWQ"}},
            "Model Optimizer",
        ),
        (MLX_NVFP4, None),
        ({"quantization_config": {"quant_method": "gptq"}}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_label_comes_from_the_config(config: Any, expected: str | None) -> None:
    assert cp.from_config(config) == expected


@pytest.mark.parametrize(
    ("repo", "tags", "expected"),
    [
        # The Hub's own tags for these repositories (2026-10-14).
        (
            "nvidia/Qwen3.8-27B-NVFP4",
            ["Model Optimizer", "safetensors", "FP4", "fp4", "FP8", "fp8", "modelopt"],
            "NVFP4 · Model Optimizer",
        ),
        (
            "unsloth/Qwen3.8-27B-NVFP4",
            ["safetensors", "unsloth", "compressed-tensors"],
            "NVFP4 · compressed-tensors",
        ),
        ("someone/Qwen3.6-27B-FP8", ["compressed-tensors"], "FP8 · compressed-tensors"),
        ("someone/Qwen3.6-27B-quant", ["modelopt", "fp8"], "FP8 · Model Optimizer"),
        # An MXFP4 or AWQ checkpoint of compressed-tensors: Splash's check decides.
        ("someone/Qwen3.6-35B-A3B-MXFP4", ["compressed-tensors"], "compressed-tensors"),
        ("mlx-community/Qwen3.6-35B-A3B-nvfp4", ["mlx", "safetensors", "4-bit"], None),
        ("unsloth/Qwen3.8-27B-GGUF", ["gguf"], None),
    ],
)
def test_the_label_from_the_hub(repo: str, tags: list[str], expected: str | None) -> None:
    assert cp.from_hub(repo, tags) == expected


def test_a_search_hit_for_a_checkpoint_is_a_labelled_safetensors_row() -> None:
    row = search_row(
        {"id": "nvidia/Qwen3.6-35B-A3B-NVFP4", "tags": ["modelopt", "fp4"], "downloads": 5}
    )
    assert row["format_guess"] == "mlx"
    assert row["checkpoint"] == "NVFP4 · Model Optimizer"
    plain = search_row({"modelId": "mlx-community/Qwen3.6-35B-A3B-4bit", "tags": ["mlx"]})
    assert plain["format_guess"] == "mlx" and plain["checkpoint"] is None
    assert search_row({"id": "a/b", "tags": None})["format_guess"] == "unknown"


def test_a_safetensors_check_gets_longer_than_one_gguf_variant() -> None:
    """A Model Optimizer shard index is 14 MB, and the check reads it twice."""
    safetensors: dict[str, int | None] = {
        "config.json": 1,
        "model.safetensors.index.json": 14_000_000,
        "model-00001-of-00003.safetensors": 1,
    }
    assert inspect_timeout(safetensors, None) == SAFETENSORS_TIMEOUT_S
    gguf: dict[str, int | None] = {"X-Q4_K_M.gguf": 1, "X-Q8_0.gguf": 1}
    assert inspect_timeout(gguf, None) == 24.0
    assert inspect_timeout(gguf, "Q4_K_M") == 20.0


def _selection(tmp_path: Path, config: Any, target_format: str = "mlx-affine") -> Selection:
    real = tmp_path / "blob-config"
    real.write_text(json.dumps(config))
    return Selection(
        model="nvidia/Qwen3.8-27B-NVFP4",
        link=tmp_path / "link",
        kind="assembly",
        record={"target_format": target_format},
        files=[FileRef("target/config.json", real, real, real.stat().st_size, "nvidia/x")],
    )


def test_an_installed_checkpoint_is_labelled_from_its_assembly_config(tmp_path: Path) -> None:
    assert installed_checkpoint(_selection(tmp_path, NVIDIA_27B)) == "NVFP4 · Model Optimizer"
    (tmp_path / "mlx").mkdir()
    assert installed_checkpoint(_selection(tmp_path / "mlx", MLX_NVFP4)) is None
    (tmp_path / "gguf").mkdir()
    assert installed_checkpoint(_selection(tmp_path / "gguf", NVIDIA_27B, "gguf")) is None
    broken = _selection(tmp_path, NVIDIA_27B)
    broken.files[0].real.write_text("{not json")  # type: ignore[union-attr]
    assert installed_checkpoint(broken) is None
