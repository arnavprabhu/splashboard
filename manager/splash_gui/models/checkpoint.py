"""Which kind of safetensors checkpoint a repository holds.

Splash loads three kinds of safetensors checkpoint as one target
(install/upstream.py `mlx_target`, recorded as `target_format` "mlx-affine"):
MLX's own (a config.json `quantization` object), and NVFP4 or FP8 weights from
NVIDIA's Model Optimizer or llm-compressor's compressed-tensors (a
`quantization_config` whose `quant_method` is "modelopt" or "compressed-tensors",
runtime/model/ModelDescriptor.mm `requireQuantization`). The GUI's `format`
stays "mlx" for all three; `checkpoint` labels the other two, e.g.
"NVFP4 · Model Optimizer", so they are not shown as MLX. A label never decides
support: Splash's model-check does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# quant_method → the name the label shows.
METHODS = {"modelopt": "Model Optimizer", "compressed-tensors": "compressed-tensors"}
# Model Optimizer's quant_algo values Splash loads (requireModelOptimizerQuantization).
_MODELOPT_KINDS = {"NVFP4": "NVFP4", "W4A16_NVFP4": "NVFP4", "FP8": "FP8"}
# compressed-tensors' group formats Splash loads (requireCompressedTensorsQuantization).
_COMPRESSED_KINDS = {"nvfp4-pack-quantized": "NVFP4", "float-quantized": "FP8"}
_NAME_NVFP4 = re.compile(r"nvfp4|(?<![a-z])fp4(?![a-z])", re.IGNORECASE)
_NAME_FP8 = re.compile(r"(?<![a-z])fp8(?![a-z])", re.IGNORECASE)


def label(kind: str | None, method: str) -> str:
    """`NVFP4 · Model Optimizer`, or the method alone when the weights' kind is unknown."""
    name = METHODS[method]
    return f"{kind} · {name}" if kind else name


def _kind(found: Iterable[str | None]) -> str | None:
    """A checkpoint that mixes NVFP4 and FP8 layers (NVIDIA's NVFP4 releases keep
    some projections in FP8) is named by its NVFP4 part."""
    kinds = set(found)
    return "NVFP4" if "NVFP4" in kinds else "FP8" if "FP8" in kinds else None


def from_config(config: Any) -> str | None:
    """The label from a config.json; None for an MLX checkpoint or anything else."""
    if not isinstance(config, dict) or isinstance(config.get("quantization"), dict):
        return None
    quantization = config.get("quantization_config")
    if not isinstance(quantization, dict):
        return None
    method = quantization.get("quant_method")
    if method == "modelopt":
        # Each quantized layer names its own algorithm when `quantized_layers` is
        # given (quant_algo is then MIXED_PRECISION); else quant_algo covers them all.
        layers = quantization.get("quantized_layers")
        entries = (
            [e for e in layers.values() if isinstance(e, dict)]
            if isinstance(layers, dict) and layers
            else [quantization]
        )
        algorithms = [str(e.get("quant_algo")) for e in entries]
        return label(_kind(_MODELOPT_KINDS.get(a) for a in algorithms), method)
    if method == "compressed-tensors":
        groups = quantization.get("config_groups")
        top = quantization.get("format")
        formats = (
            [g.get("format", top) for g in groups.values() if isinstance(g, dict)]
            if isinstance(groups, dict)
            else []
        )
        return label(_kind(_COMPRESSED_KINDS.get(f) for f in formats if isinstance(f, str)), method)
    return None


def from_hub(repo_id: str, tags: Iterable[str]) -> str | None:
    """The label from a Hub listing, before config.json is read: the method from the
    Hub's `modelopt` or `compressed-tensors` tag, the kind from the repository's name
    (or Model Optimizer's `fp4` / `fp8` tags). None for an MLX or GGUF repository."""
    tagset = {t.lower() for t in tags}
    if "mlx" in tagset or "gguf" in tagset:
        return None
    method = next((m for m in METHODS if m in tagset), None)
    if method is None:
        return None
    name = repo_id.rsplit("/", 1)[-1]
    kind = "NVFP4" if _NAME_NVFP4.search(name) else "FP8" if _NAME_FP8.search(name) else None
    if kind is None and method == "modelopt":
        kind = "NVFP4" if "fp4" in tagset else "FP8" if "fp8" in tagset else None
    return label(kind, method)
