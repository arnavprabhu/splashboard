"""Plain wording for Splash's technical model refusals.

Splash 1.3.0's `model-check` refuses an MLX checkpoint that is not affine 4-bit,
group size 64 with the first mismatching module, e.g. "quantization
language_model.model.embed_tokens bits mismatch: MLX 8, runtime 4"
(`splash/runtime/model/ModelDescriptor.mm:404-422`, `requireNumber` at :117-124).
Every place that shows a verdict (the `/inspect` reason, a refused download, a
failed load) leads with one plain line and keeps the engine's words as the detail.
"""

from __future__ import annotations

import re

MLX_QUANTIZATION = "Splash runs MLX models only at 4-bit, group size 64."

_MLX_QUANTIZATION = re.compile(
    r"requires an mlx affine 4-bit"
    r"|\bquantization \S+ (?:bits|group_size) mismatch: mlx\b"
    r"|\bquantization \S+ (?:mode must be affine|must be an object)",
    re.IGNORECASE,
)


def explain_refusal(reason: str | None) -> tuple[str | None, str | None]:
    """`(reason, detail)`: the plain line and the engine's own text when Splash's
    refusal is one we can say more simply, else `(reason, None)` unchanged."""
    if reason and _MLX_QUANTIZATION.search(reason):
        return MLX_QUANTIZATION, reason
    return reason, None
