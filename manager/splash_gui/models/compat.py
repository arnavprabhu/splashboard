"""Plain wording for Splash's technical model refusals.

Splash 1.3.1's `model-check` takes an MLX checkpoint quantized as affine 2, 3, 4,
5, 6 or 8 bits in groups of 32, 64 or 128, or as mxfp4, and refuses any other
with the first module that breaks the rule, e.g. "quantization
language_model.model.embed_tokens is affine 7-bit in groups of 16; MLX weights
load as ..." (`splash/runtime/model/ModelDescriptor.mm` `requireQuantization`).
A checkpoint without a quantization object (BF16 weights) gets "this model
requires an MLX checkpoint (...) or a supported GGUF".
Every place that shows a verdict (the `/inspect` reason, a refused download, a
failed load) leads with one plain line and keeps the engine's words as the detail.
"""

from __future__ import annotations

import re

MLX_QUANTIZATION = (
    "Splash runs MLX models quantized as affine 2, 3, 4, 5, 6 or 8 bits "
    "(groups of 32, 64 or 128) or as mxfp4."
)
MLX_UNQUANTIZED = "Splash needs every projection and the token table of an MLX model quantized."

_MLX_QUANTIZATION = re.compile(
    r"requires an mlx checkpoint \(affine"
    r"|\bquantization\b[^;]* is \S+ \d+-bit in groups of \d+; mlx weights load as"
    r"|\bquantization\b.* (?:mode must be a string|must be a whole number)",
    re.IGNORECASE,
)
_MLX_UNQUANTIZED = re.compile(
    r"\bquantization \S+ is unquantized; splash loads quantized mlx", re.IGNORECASE
)


def explain_refusal(reason: str | None) -> tuple[str | None, str | None]:
    """`(reason, detail)`: the plain line and the engine's own text when Splash's
    refusal is one we can say more simply, else `(reason, None)` unchanged."""
    if reason and _MLX_UNQUANTIZED.search(reason):
        return MLX_UNQUANTIZED, reason
    if reason and _MLX_QUANTIZATION.search(reason):
        return MLX_QUANTIZATION, reason
    return reason, None
