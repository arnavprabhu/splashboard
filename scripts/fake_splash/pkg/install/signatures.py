"""The architecture each family's configs state, for the fake's stand-in for the
engine's `model-check` (splash/runtime/main.mm checkModel, ModelDescriptor.mm).

Splash 1.3.0 moved the architecture match from Python (1.2.x `families.family_for`)
into the engine binary, and `install/families.py` now names only each family and
its draft repository. The fake has no engine binary, so the fields it builds
synthetic configs from, and checks them against, live here.
"""

from __future__ import annotations

from . import families, models

# The text_config fields that identify each family's target, as an MLX config
# states them (the 1.2.0 families.py signatures; Qwen3.8-Flash-Next's from
# runtime/model/Qwen4Exp.hpp, its config derived from a GGUF header).
TEXT: dict[str, tuple[tuple[str, object], ...]] = {
    "Qwen3.8-27B": (
        ("model_type", "qwen3_5_text"),
        ("max_position_embeddings", 262144),
        ("hidden_size", 5120),
        ("num_hidden_layers", 64),
        ("vocab_size", 248320),
        ("num_attention_heads", 24),
        ("num_key_value_heads", 4),
        ("head_dim", 256),
    ),
    "Qwen3.6-35B-A3B": (
        ("model_type", "qwen3_5_moe_text"),
        ("max_position_embeddings", 262144),
        ("hidden_size", 2048),
        ("num_hidden_layers", 40),
        ("vocab_size", 248320),
        ("num_attention_heads", 16),
        ("num_key_value_heads", 2),
        ("head_dim", 256),
        ("num_experts", 256),
        ("num_experts_per_tok", 8),
    ),
    "Qwen3.8-Flash-Next": (
        ("model_type", "qwen4_exp_text"),
        ("max_position_embeddings", 262144),
        ("hidden_size", 2560),
        ("num_hidden_layers", 48),
        ("vocab_size", 248320),
        ("num_attention_heads", 24),
        ("num_key_value_heads", 2),
        ("head_dim", 256),
        ("num_experts", 512),
        ("num_experts_per_tok", 10),
    ),
}
# The families whose layout loads no safetensors checkpoint, only a GGUF
# (Qwen4Exp.hpp `loadsSafetensors = false`).
GGUF_ONLY = frozenset({"Qwen3.8-Flash-Next"})

# The fields every DFlash2 draft states alike.
_DFLASH2 = (
    ("architectures", ["DFlash2DraftModel"]),
    ("sliding_window", 2048),
    ("is_causal", False),
    ("attention_bias", False),
    ("tie_word_embeddings", False),
    ("rms_norm_eps", 1e-6),
    ("hidden_act", "silu"),
)

DRAFT: dict[str, tuple[tuple[str, object], ...]] = {
    "Qwen3.8-27B": (
        *_DFLASH2,
        ("num_hidden_layers", 5),
        ("hidden_size", 5120),
        ("vocab_size", 248320),
        ("intermediate_size", 17408),
    ),
    "Qwen3.6-35B-A3B": (
        *_DFLASH2,
        ("num_hidden_layers", 6),
        ("hidden_size", 2048),
        ("vocab_size", 248320),
        ("intermediate_size", 6144),
    ),
    # incoai/Qwen3.8-Flash-Next-DFlash2's config.json (Hub, 2026-10-20).
    "Qwen3.8-Flash-Next": (
        *_DFLASH2,
        ("num_hidden_layers", 5),
        ("hidden_size", 2560),
        ("vocab_size", 248320),
        ("intermediate_size", 7680),
    ),
}


def text_config(family: families.ModelFamily) -> dict:
    return dict(TEXT[family.name])


def draft_config(family: families.ModelFamily) -> dict:
    return dict(DRAFT[family.name])


def check(config: object) -> families.ModelFamily:
    """The family config describes, or the engine's refusal
    (ModelDescriptor.mm unsupportedModel: "no supported model has this architecture
    (...); supported: " and every family)."""
    supported = ", ".join(f.name for f in families.FAMILIES)
    text = config.get("text_config") if isinstance(config, dict) else None
    if not isinstance(text, dict):
        raise models.ModelError(
            f"no supported model has this architecture (its config has no text_config); supported: {supported}"
        )
    for family in families.FAMILIES:
        if all(text.get(key) == value for key, value in TEXT[family.name]):
            return family
    keys = sorted({key for fields in TEXT.values() for key, _ in fields})
    found = ", ".join(f"{key}={text.get(key)}" for key in keys if key in text)
    raise models.ModelError(f"no supported model has this architecture ({found}); supported: {supported}")
