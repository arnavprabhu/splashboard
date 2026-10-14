"""Plain wording for Splash's technical model refusals.

Splash 1.3.2's `model-check` takes a safetensors checkpoint that is MLX's,
quantized as affine 2, 3, 4, 5, 6 or 8 bits in groups of 32, 64 or 128, or as
mxfp4 or nvfp4, or NVIDIA's Model Optimizer's or compressed-tensors' with NVFP4
or FP8 weights, and refuses any other with the first module that breaks the rule
(`splash/runtime/model/ModelDescriptor.mm` `requireQuantization`), e.g.
"quantization language_model.model.embed_tokens is affine 7-bit in groups of 16;
MLX weights load as ...", "quantization_config is INT4_AWQ; Model Optimizer
weights load as NVFP4, W4A16_NVFP4 or FP8" or "quantization_config leaves lm_head
unquantized; Splash loads quantized projections". A checkpoint without any of
them (BF16 weights) gets "this model requires an MLX checkpoint (...), an NVFP4 or
FP8 checkpoint of Model Optimizer or compressed-tensors, or a supported GGUF". A
GGUF whose projections and experts are all BF16, F16 or F32 is refused by the
installer (`install/gguf.py` `require_loadable`).
Every place that shows a verdict (the `/inspect` reason, a refused download, a
failed load) leads with one plain line and keeps the engine's words as the detail.
"""

from __future__ import annotations

import re

from .catalog import WHOLE_UNQUANTIZED

MLX_QUANTIZATION = (
    "Splash runs MLX models quantized as affine 2, 3, 4, 5, 6 or 8 bits "
    "(groups of 32, 64 or 128), mxfp4 or nvfp4, and NVFP4 or FP8 checkpoints "
    "from Model Optimizer or compressed-tensors."
)
MLX_UNQUANTIZED = "Splash needs every projection of an MLX model quantized."
CHECKPOINT_QUANTIZATION = (
    "Splash runs Model Optimizer and compressed-tensors checkpoints whose weights "
    "are NVFP4 (in groups of 16) or FP8."
)
CHECKPOINT_UNQUANTIZED = (
    "Splash needs every projection and the head of a Model Optimizer or "
    "compressed-tensors checkpoint quantized as NVFP4 or FP8."
)

_MLX_QUANTIZATION = re.compile(
    r"requires an mlx checkpoint \(affine"
    r"|\bquantization\b[^;]* is \S+ \d+-bit in groups of \d+; mlx weights load as"
    r"|\bquantization\b.* (?:mode must be a string|must be a whole number)",
    re.IGNORECASE,
)
_MLX_UNQUANTIZED = re.compile(
    r"\bquantization \S+ is unquantized; splash loads quantized mlx", re.IGNORECASE
)
_CHECKPOINT_UNQUANTIZED = re.compile(
    r"\bquantization_config leaves \S+ unquantized; splash loads quantized projections",
    re.IGNORECASE,
)
_CHECKPOINT_QUANTIZATION = re.compile(
    r"\bquantization_config\b.*(?:; model optimizer (?:weights load as|nvfp4 loads in groups)"
    r"|; compressed-tensors weights load as"
    r"|is asymmetric or ordered by activation groups"
    r"|is not a regular expression splash reads|entry exceeds \d+ bytes|too complex to match"
    r"| must be (?:an object|an array|a non-empty object|a string|a whole number))",
    re.IGNORECASE,
)
_GGUF_UNQUANTIZED = re.compile(
    r"stores every projection and expert unquantized \(bf16, f16 or f32\)", re.IGNORECASE
)


def explain_refusal(reason: str | None) -> tuple[str | None, str | None]:
    """`(reason, detail)`: the plain line and the engine's own text when Splash's
    refusal is one we can say more simply, else `(reason, None)` unchanged."""
    if not reason:
        return reason, None
    for pattern, plain in (
        (_MLX_UNQUANTIZED, MLX_UNQUANTIZED),
        (_CHECKPOINT_UNQUANTIZED, CHECKPOINT_UNQUANTIZED),
        (_CHECKPOINT_QUANTIZATION, CHECKPOINT_QUANTIZATION),
        (_MLX_QUANTIZATION, MLX_QUANTIZATION),
        (_GGUF_UNQUANTIZED, WHOLE_UNQUANTIZED),
    ):
        if pattern.search(reason):
            return plain, reason
    return reason, None
