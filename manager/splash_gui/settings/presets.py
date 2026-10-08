"""Welcome-wizard presets (SPEC §8.6)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..models import catalog as cat
from .model import PresetId, SettingsDocument

GIB = 1024**3


@dataclass(frozen=True)
class ModelPick:
    model: str
    note: str
    overrides: dict[str, Any] = field(default_factory=dict)  # per-model `serve` overrides


@dataclass(frozen=True)
class Recommendation:
    primary: ModelPick | None
    alternatives: tuple[ModelPick, ...]
    reason: str


@dataclass(frozen=True)
class Preset:
    id: PresetId
    label: str
    description: str
    settings: dict[str, Any]  # dotted global key -> value


PRESETS: dict[PresetId, Preset] = {
    "coding": Preset(
        "coding",
        "Coding agents",
        "Long context and a persistent SSD cache so agents reuse their prompts across restarts.",
        {
            "serve.max_context": "128K",
            "serve.max_cache_disk": "32G",
            "serve.persistent_cache": True,
            "serve.default_reasoning_effort": None,
            "routing.auto_load": True,
        },
    ),
    "chat": Preset(
        "chat",
        "Chat & general",
        "Automatic context, vision on, no disk tier.",
        {
            "serve.max_context": "auto",
            "serve.max_cache_disk": "0",
            "serve.persistent_cache": False,
        },
    ),
    "speed": Preset(
        "speed",
        "Max speed",
        "The MoE model with INT8 KV and a session-only SSD cache.",
        {
            "serve.max_context": "auto",
            "serve.kv_format": "int8",
            "serve.decode_share": 0.5,
            "serve.max_cache_disk": "16G",
            "serve.persistent_cache": False,
        },
    ),
}

_MLX_27B = "mlx-community/Qwen3.8-27B-4bit"
_MLX_35B = "mlx-community/Qwen3.6-35B-A3B-4bit"
_GGUF_35B_Q4 = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
_GGUF_35B_Q2 = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL"
_GGUF_27B_IQ3 = "unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS"
_BONSAI = "prism-ml/Ternary-Bonsai-2-27B-gguf:PQ2_0"


def recommend(preset: PresetId, memory_bytes: int) -> Recommendation:
    gb = memory_bytes / GIB
    if gb < 24:
        return Recommendation(None, (), "Splash's supported models need at least 24 GB.")
    if preset == "coding":
        if gb >= 48:
            return Recommendation(
                ModelPick(_MLX_27B, "Best quality"),
                (ModelPick(_MLX_35B, "Faster alternative"),),
                "≥ 48 GB",
            )
        if gb >= 36:
            return Recommendation(
                ModelPick(_GGUF_35B_Q4, "Fits with room for context"), (), "36–47 GB"
            )
        return Recommendation(
            ModelPick(
                _GGUF_27B_IQ3,
                "Language-only reaches the ~100K agents need on 24 GB",
                {"language_only": True},
            ),
            (ModelPick(_GGUF_35B_Q2, "MoE, 256K context"),),
            "24–35 GB",
        )
    if preset == "chat":
        if gb >= 36:
            return Recommendation(ModelPick(_MLX_35B, "Fast, with vision"), (), "≥ 36 GB")
        return Recommendation(ModelPick(_BONSAI, "7.2 GB, with vision"), (), "24–35 GB")
    if gb >= 36:
        return Recommendation(ModelPick(_MLX_35B, "About 210 tok/s on an M5 Pro"), (), "≥ 36 GB")
    return Recommendation(ModelPick(_GGUF_35B_Q2, "MoE at 2-bit"), (), "24–35 GB")


# The context ladder for "128K (or the largest that fits)" (SPEC §8.6), in tokens.
CODING_CONTEXTS: tuple[tuple[str, int], ...] = (
    ("128K", 128 * 1024),
    ("64K", 64 * 1024),
    ("32K", 32 * 1024),
    ("16K", 16 * 1024),
    ("8K", 8 * 1024),
)

# Each coding pick's family (its KV geometry, `catalog.KV_GEOMETRY`) and target weights.
# MLX: Splash's own download size less the draft (splash/DEVELOPMENT.md "Model storage",
# up to about 21 GB and 24 GB). UD-Q4_K_M: the Hub's file size (docs/spec-drift.md row
# 70). The two smaller GGUF variants are estimates from their bits per weight.
_CODING_TARGETS: dict[str, tuple[str, int]] = {
    _MLX_27B: ("Qwen3.8-27B", int(20 * GIB)),
    _MLX_35B: ("Qwen3.6-35B-A3B", int(23 * GIB)),
    _GGUF_35B_Q4: ("Qwen3.6-35B-A3B", int(16.5 * GIB)),
    _GGUF_27B_IQ3: ("Qwen3.8-27B", 11 * GIB),
    _GGUF_35B_Q2: ("Qwen3.6-35B-A3B", 12 * GIB),
}


def coding_context(memory_bytes: int, pick: ModelPick) -> str:
    """The largest context step whose memory estimate (SPEC §9.1, with the KV cache that
    context takes at the default INT8 format) is not "Won't fit" for `pick` on this Mac.
    None of them fitting gives the smallest step; a pick with no geometry keeps 128K."""
    spec = _CODING_TARGETS.get(pick.model)
    if spec is None:
        return CODING_CONTEXTS[0][0]
    family, target = spec
    vision = not pick.overrides.get("language_only", False)
    for label, tokens in CODING_CONTEXTS:
        need = cat.memory_need(target, vision=vision, kv=cat.kv_bytes(family, tokens))
        if cat.fit_for(need, memory_bytes) != "wont_fit":
            return label
    return CODING_CONTEXTS[-1][0]


def preset_settings(preset_id: PresetId, memory_bytes: int | None = None) -> dict[str, Any]:
    """The dotted global settings a preset applies. The coding preset's context is the
    largest step that fits this Mac (`coding_context`) when the memory is known."""
    settings = dict(PRESETS[preset_id].settings)
    if preset_id == "coding" and memory_bytes is not None:
        primary = recommend("coding", memory_bytes).primary
        if primary is not None:
            settings["serve.max_context"] = coding_context(memory_bytes, primary)
    return settings


def apply_preset(
    doc: SettingsDocument,
    preset_id: PresetId,
    model: str | None = None,
    memory_bytes: int | None = None,
) -> dict[str, Any]:
    """The raw settings document with the preset's global values (and, when `model` is
    the recommended pick, its per-model overrides) applied. Validate before saving."""
    data = doc.to_json_dict()
    glob = data["global"]
    for key, value in preset_settings(preset_id, memory_bytes).items():
        section, name = key.split(".", 1)
        glob.setdefault(section, {})[name] = copy.deepcopy(value)
    glob["wizard"]["preset"] = preset_id
    if model is not None and memory_bytes is not None:
        rec = recommend(preset_id, memory_bytes)
        picks = ([rec.primary] if rec.primary else []) + list(rec.alternatives)
        for pick in picks:
            if pick.model == model and pick.overrides:
                entry = data["models"].setdefault(
                    model, {"serve": {}, "sampling_defaults": {}, "profiles": {}}
                )
                entry["serve"].update(pick.overrides)
    return data
