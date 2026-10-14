# Derived from Splash (github.com/incoai/splash, Apache-2.0), modified by the Splashboard authors.
# See LICENSE.splash and NOTICE in scripts/fake_splash.
"""Small compatibility stand-in for splash/install/upstream.py (1.3.2).

Mirrors its `Target`, `check_model` and the screening order of
`inspect_target(repo, variant, language_only, scratch)`: a GGUF's variant must be
selected and loadable, a safetensors checkpoint must be quantized in a format the
kernels hold (MLX's, or NVFP4 or FP8 from Model Optimizer or compressed-tensors), and vision
needs a usable projector. The real `check_model` runs the engine's `model-check`;
the fake has no engine, so `signatures.check` stands in for it, and an unsupported
config fails here rather than in the manager.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import families, models, signatures


@dataclass(frozen=True)
class Target:
    """What the repository supplies, known before any weight download."""

    format: str
    vision_format: str
    config: Path
    gguf_metadata: Path | None
    family: families.ModelFamily
    files: dict[str, str]


def check_model(target_format, vision_format, config, *, gguf_metadata=None, draft=None):
    """The family the "engine" finds config (a path) to describe."""
    return signatures.check(json.loads(Path(config).read_text()))


def inspect_target(repo, variant, language_only, scratch):
    if variant is not None or not any(n.endswith(".safetensors") for n in repo.files):
        return _gguf_target(repo, variant, language_only, Path(scratch))
    return _mlx_target(repo, language_only, Path(scratch))


def select_vision(repo):
    """The one vision projector Splash can use, chosen by precision.

    `splash/install/upstream.py select_vision` (1.3.1 on) keeps only a `clip` projector
    whose tensors are BF16, F32 or F16, preferred in that order.
    """
    usable: dict[str, list[str]] = {"BF16": [], "F32": [], "F16": []}
    found: list[str] = []
    for name in sorted(n for n in repo.files if "mmproj" in Path(n).stem.lower()):
        precision = next((p for p in ("BF16", "F32", "F16") if p in name), None)
        found.append(f"{name} (clip: {precision or 'unknown'})")
        if precision in usable:
            usable[precision].append(name)
    for precision in ("BF16", "F32", "F16"):
        if len(usable[precision]) == 1:
            return usable[precision][0]
        if len(usable[precision]) > 1:
            raise models.ModelError(
                f"several {precision} vision projectors, "
                + ", ".join(usable[precision])
                + ", describe no single tower; use --language-only to serve text only"
            )
    raise models.ModelError(
        "the GGUF repository has no BF16, F32 or F16 vision projector ("
        + ("; ".join(found) or "no GGUF named mmproj")
        + "); use --language-only to serve text only"
    )


def select_gguf(files, variant):
    """The target GGUF among a repository's root files, and whether it was
    taken by its -VARIANT ending from several (mirrors
    splash/install/upstream.py select_gguf).

    :VARIANT names the file whose name is the model name all of them share, then
    -VARIANT; failing that, the only file whose name ends in -VARIANT. Without
    :VARIANT, the only target GGUF.
    """
    candidates = sorted(
        n for n in files if "/" not in n and n.endswith(".gguf") and "mmproj" not in Path(n).stem.lower()
    )
    if variant is None:
        if len(candidates) == 1:
            return candidates[0], False
        choice = "select a GGUF with OWNER/REPO:VARIANT"
    else:
        parts = [Path(name).stem.split("-") for name in candidates]
        shared = len(os.path.commonprefix(parts))
        exact = [
            name
            for name, words in zip(candidates, parts, strict=True)
            if "-".join(words[shared:]).lower() == variant.lower()
        ]
        if len(exact) == 1:
            return exact[0], False
        ending = [n for n in candidates if Path(n).stem.lower().endswith("-" + variant.lower())]
        if len(ending) == 1:
            return ending[0], len(candidates) > 1
        choice = "no single GGUF matches :" + variant
    listed = ", ".join(candidates) or "none"
    raise models.ModelError(f"{choice} (files in the repository root: {listed})")


def _gguf_target(repo, variant, language_only, scratch):
    name, by_ending = select_gguf(repo.files, variant)
    if by_ending:
        print(
            f"No GGUF is named for :{variant} alone; using {name}, the only one whose name ends in -{variant}.",
            flush=True,
        )
    # The fake's GGUFs carry no real header: the repository's config.json stands
    # in for the config upstream.py derives from it (gguf.model_config).
    config, metadata = scratch / "config.json", scratch / "gguf-metadata.json"
    config.write_bytes(models.json_bytes(repo.json("config.json")))
    metadata.write_bytes(models.json_bytes({"unsigned": {}, "float": {}, "string": {}}))
    time.sleep(_inspect_seconds(name))
    if Path(name).stem.lower().startswith("imatrix"):
        # An importance matrix has no model metadata (real Splash, install/gguf.py).
        raise models.ModelError("missing or invalid GGUF metadata: general.architecture")
    family = check_model("gguf", "none", config, gguf_metadata=metadata)
    # install/gguf.py require_loadable (1.3.2, lines 574-596) reads the tensor
    # types; the fake's GGUFs have none, so the name stands in. Since 1.3.2 only a
    # whole unquantized model is refused (UD-Q8_K_XL, with BF16 parts, loads).
    if Path(name).stem.upper().endswith(("-BF16", "-F16", "-F32")):
        raise models.ModelError(
            "this GGUF stores every projection and expert unquantized (BF16, F16 or F32): Splash loads quantized "
            "GGUFs, not whole-BF16 or F16 models; choose a quantized variant of the repository"
        )
    files = {"target/" + name: name}
    vision_format = "none"
    if not language_only:
        projector = select_vision(repo)
        files["vision/mmproj.gguf"] = projector
        vision_format = "gguf"
    return Target("gguf", vision_format, config, metadata, family, files)


def _inspect_seconds(name):
    """`FAKE_SPLASH_INSPECT_SECONDS`: how long reading one GGUF's header takes, e.g.
    `0.2`, or `0.2,UD-Q4_K_M=1` to make one variant slower than the rest."""
    seconds = 0.0
    for part in filter(None, os.environ.get("FAKE_SPLASH_INSPECT_SECONDS", "").split(",")):
        variant, _, value = part.rpartition("=")
        if not variant:
            seconds = float(value)
        elif Path(name).stem.endswith("-" + variant):
            return float(value)
    return seconds


# The affine formats the block kernels hold (metal/abi/QuantFormat.h): bits and group sizes.
AFFINE_BITS = (2, 3, 4, 5, 6, 8)
AFFINE_GROUPS = (32, 64, 128)
# Where an MLX repository keeps its image processor's configuration (upstream.py `PROCESSOR_FILES`).
PROCESSOR_FILES = ("preprocessor_config.json", "processor_config.json")


def _require_quantization(entry, label, module):
    """ModelDescriptor.mm `requireMlxQuantization` (1.3.2) for one entry: an affine
    format the kernels hold, mxfp4 4-bit in groups of 32 or nvfp4 4-bit in groups of
    16. A module's entry that omits bits or group size takes its mode's default."""
    mode = entry.get("mode", "affine")
    if not isinstance(mode, str):
        raise models.ModelError(f"{label} mode must be a string")
    affine, nvfp4 = mode == "affine", mode == "nvfp4"

    def number(key, default):
        value = entry.get(key)
        if module and value is None:
            return default
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != int(value) or value < 0:
            raise models.ModelError(f"{label} {key} must be a whole number")
        return int(value)

    bits, group = number("bits", 4), number("group_size", 64 if affine else 16 if nvfp4 else 32)
    if affine:
        ok = bits in AFFINE_BITS and group in AFFINE_GROUPS
    elif nvfp4:
        ok = bits == 4 and group == 16
    else:
        ok = mode == "mxfp4" and bits == 4 and group == 32
    if not ok:
        raise models.ModelError(
            f"{label} is {mode} {bits}-bit in groups of {group}; MLX weights load as affine 2, 3, 4, 5, 6 "
            "or 8 bits in groups of 32, 64 or 128, or as mxfp4 or nvfp4"
        )


def _require_quantization_config(quantization):
    """ModelDescriptor.mm `requireModelOptimizerQuantization` and
    `requireCompressedTensorsQuantization` (1.3.2): a quantization_config of
    quant_method modelopt holding NVFP4 (in groups of 16) or FP8, or of
    compressed-tensors whose groups hold NVFP4 or FP8. The fake checks the formats,
    not which modules each covers."""
    method = quantization.get("quant_method")
    if method == "modelopt":
        layers = quantization.get("quantized_layers")
        if isinstance(layers, dict):
            # Each quantized layer names its own algorithm; the top-level one plays no part.
            for module, entry in layers.items():
                label = f"quantization_config quantized_layers {module}"
                if not isinstance(entry, dict):
                    raise models.ModelError(f"{label} must be an object")
                _require_modelopt_algorithm(entry, label)
            return
        _require_modelopt_algorithm(quantization, "quantization_config")
        return
    groups = quantization.get("config_groups")
    if not isinstance(groups, dict) or not groups:
        raise models.ModelError("quantization_config config_groups must be a non-empty object")
    for name, group in groups.items():
        label = f"quantization_config config_groups {name}"
        weights = group.get("weights") if isinstance(group, dict) else None
        if not isinstance(weights, dict):
            raise models.ModelError(f"{label} weights must be an object")
        form = group.get("format", quantization.get("format", ""))
        kind, bits, strategy = weights.get("type"), weights.get("num_bits"), weights.get("strategy")
        group_size = weights.get("group_size") or 0
        nvfp4 = form == "nvfp4-pack-quantized" and kind == "float" and bits == 4 and strategy == "tensor_group"
        fp8 = form == "float-quantized" and kind == "float" and bits == 8 and strategy in ("channel", "tensor")
        if not ((nvfp4 and group_size == 16) or fp8):
            raise models.ModelError(
                f"{label} is {form}: {kind} {bits}-bit by {strategy}"
                + (f" in groups of {group_size}" if group_size else "")
                + "; compressed-tensors weights load as NVFP4 (nvfp4-pack-quantized: float 4-bit by tensor_group "
                "in groups of 16) or FP8 (float-quantized: float 8-bit by channel or tensor)"
            )
        if weights.get("symmetric") is False or weights.get("actorder") in ("group", True):
            raise models.ModelError(
                f"{label} is asymmetric or ordered by activation groups; Splash loads symmetric "
                "compressed-tensors weights in their stored column order"
            )


def _require_modelopt_algorithm(entry, label):
    """ModelDescriptor.mm `requireModelOptimizerQuantization`'s requireAlgorithm (1.3.2)."""
    algorithm = entry.get("quant_algo")
    if algorithm == "FP8":
        return
    if algorithm not in ("NVFP4", "W4A16_NVFP4"):
        raise models.ModelError(f"{label} is {algorithm}; Model Optimizer weights load as NVFP4, W4A16_NVFP4 or FP8")
    group = entry.get("group_size")
    if group is not None and group != 16:
        raise models.ModelError(f"{label} is NVFP4 in groups of {group}; Model Optimizer NVFP4 loads in groups of 16")


def _mlx_target(repo, language_only, scratch):
    config = scratch / "config.json"
    config.write_bytes(models.json_bytes(repo.json("config.json")))
    family = check_model("mlx-affine", "none", config)
    # The engine model-check's wording (ModelDescriptor.mm requireQuantization, 1.3.2).
    config_json = repo.json("config.json")
    quant, quantization_config = config_json.get("quantization"), config_json.get("quantization_config")
    if isinstance(quant, dict):
        _require_quantization(quant, "quantization", False)
        for module, entry in quant.items():
            if isinstance(entry, dict):
                _require_quantization(entry, f"quantization {module}", True)
    elif isinstance(quantization_config, dict) and quantization_config.get("quant_method") in (
        "modelopt",
        "compressed-tensors",
    ):
        _require_quantization_config(quantization_config)
    else:
        raise models.ModelError(
            "this model requires an MLX checkpoint (affine 2, 3, 4, 5, 6 or 8 bits in groups of 32, 64 or 128, "
            "mxfp4 or nvfp4), an NVFP4 or FP8 checkpoint of Model Optimizer or compressed-tensors, or a "
            "supported GGUF"
        )
    files = {name: name for name in sorted(repo.files)}
    files.setdefault("config.json", "config.json")
    if language_only:
        # upstream.py `_mlx_target` (1.3.1 on): the processor config is read only when
        # vision is on.
        for name in PROCESSOR_FILES:
            files.pop(name, None)
    return Target("mlx-affine", "none" if language_only else "safetensors", config, None, family, files)
