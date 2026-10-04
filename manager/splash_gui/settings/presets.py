"""Welcome-wizard presets (SPEC §8.6)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

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


def apply_preset(
    doc: SettingsDocument,
    preset_id: PresetId,
    model: str | None = None,
    memory_bytes: int | None = None,
) -> dict[str, Any]:
    """The raw settings document with the preset's global values (and, when `model` is
    the recommended pick, its per-model overrides) applied. Validate before saving."""
    preset = PRESETS[preset_id]
    data = doc.to_json_dict()
    glob = data["global"]
    for key, value in preset.settings.items():
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
