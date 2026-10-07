"""Small compatibility stand-in for splash/install/upstream.py (1.3.0).

Mirrors its `Target`, `check_model` and the screening order of
`inspect_target(repo, variant, language_only, scratch)`: a GGUF's variant must be
selected and loadable, an MLX checkpoint must be affine 4-bit group 64, and vision
needs a usable projector. The real `check_model` runs the engine's `model-check`;
the fake has no engine, so `signatures.check` stands in for it, and an unsupported
config fails here rather than in the manager.
"""

from __future__ import annotations

import json
import os
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

    `splash/install/upstream.py select_vision` keeps only a `clip` projector
    whose tensors are BF16 or F32, preferring BF16: an F16 projector has already
    rounded small weights, and preparation never rounds a weight.
    """
    usable: dict[str, list[str]] = {"BF16": [], "F32": []}
    found: list[str] = []
    for name in sorted(n for n in repo.files if "mmproj" in Path(n).stem.lower()):
        precision = next((p for p in ("BF16", "F32", "F16") if p in name), None)
        found.append(f"{name} (clip: {precision or 'unknown'})")
        if precision in usable:
            usable[precision].append(name)
    for precision in ("BF16", "F32"):
        if len(usable[precision]) == 1:
            return usable[precision][0]
        if len(usable[precision]) > 1:
            raise models.ModelError(
                f"several {precision} vision projectors, "
                + ", ".join(usable[precision])
                + ", describe no single tower; use --language-only to serve text only"
            )
    raise models.ModelError(
        "the GGUF repository has no BF16 or F32 vision projector ("
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
    family = check_model("gguf", "none", config, gguf_metadata=metadata)
    if any(part in name for part in ("BF16", "UD-Q8_K_XL")):
        raise models.ModelError("this GGUF stores tensors Splash cannot load")
    files = {"target/" + name: name}
    vision_format = "none"
    if not language_only:
        projector = select_vision(repo)
        files["vision/mmproj.gguf"] = projector
        vision_format = "gguf"
    return Target("gguf", vision_format, config, metadata, family, files)


def _mlx_target(repo, language_only, scratch):
    config = scratch / "config.json"
    config.write_bytes(models.json_bytes(repo.json("config.json")))
    family = check_model("mlx-affine", "none", config)
    # The engine model-check's wording (ModelDescriptor.mm validateQuantization and
    # requireNumber, 1.3.0), naming the first module it checks.
    quant = repo.json("config.json").get("quantization")
    if not isinstance(quant, dict):
        raise models.ModelError(
            "this model requires an MLX affine 4-bit/group-64 checkpoint or a supported GGUF"
        )
    label = "quantization language_model.model.embed_tokens"
    if quant.get("bits") != 4:
        raise models.ModelError(f"{label} bits mismatch: MLX {quant.get('bits')}, runtime 4")
    if quant.get("group_size") != 64:
        raise models.ModelError(
            f"{label} group_size mismatch: MLX {quant.get('group_size')}, runtime 64"
        )
    if quant.get("mode", "affine") != "affine":
        raise models.ModelError(f"{label} mode must be affine")
    files = {name: name for name in sorted(repo.files)}
    files.setdefault("config.json", "config.json")
    if language_only:
        # upstream.py `_mlx_target` (1.3.0, :208-243): the processor config is read
        # and linked only when vision is on.
        files.pop("preprocessor_config.json", None)
    return Target("mlx-affine", "none" if language_only else "safetensors", config, None, family, files)
