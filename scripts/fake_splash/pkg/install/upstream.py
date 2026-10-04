"""Small compatibility stand-in. The real helper still imports the engine API.

Mirrors splash/install/upstream.py's `Target` and the screening order of
`inspect_target`: a GGUF's variant must be selected and loadable, an MLX
checkpoint must be affine 4-bit group 64, and vision needs a usable projector.
`families.family_for` does the architecture check, so an unsupported config
fails here rather than in the manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import families, models


@dataclass(frozen=True)
class Target:
    """What the repository supplies, known before any weight download."""

    format: str
    vision_format: str
    config: dict
    files: dict[str, str]


def inspect_target(repo, variant, language_only):
    if variant is not None or not any(n.endswith(".safetensors") for n in repo.files):
        return _gguf_target(repo, variant, language_only)
    return _mlx_target(repo, language_only)


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
        n
        for n in files
        if "/" not in n and n.endswith(".gguf") and "mmproj" not in Path(n).stem.lower()
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


def _gguf_target(repo, variant, language_only):
    name, by_ending = select_gguf(repo.files, variant)
    if by_ending:
        print(
            f"No GGUF is named for :{variant} alone; using {name}, the only one "
            f"whose name ends in -{variant}.",
            flush=True,
        )
    if any(part in name for part in ("BF16", "UD-Q8_K_XL")):
        raise models.ModelError("this GGUF stores tensors Splash cannot load")
    files = {"target/" + name: name}
    vision_format = "none"
    if not language_only:
        projector = select_vision(repo)
        files["vision/mmproj.gguf"] = projector
        vision_format = "gguf"
    config = repo.json("config.json")
    families.family_for(config)
    return Target("gguf", vision_format, config, files)


def _mlx_target(repo, language_only):
    config = repo.json("config.json")
    families.family_for(config)
    quant = config.get("quantization") or {}
    if quant.get("mode", "affine") != "affine" or quant.get("bits") != 4:
        raise models.ModelError(
            f"Splash needs an MLX affine 4-bit checkpoint, not "
            f"{quant.get('mode', 'affine')} {quant.get('bits', 4)}-bit"
        )
    if quant.get("group_size") != 64:
        raise models.ModelError(
            f"Splash needs MLX group size 64, not {quant.get('group_size')}"
        )
    files = {name: name for name in sorted(repo.files)}
    files.setdefault("config.json", "config.json")
    return Target("mlx-affine", "none" if language_only else "safetensors", config, files)
