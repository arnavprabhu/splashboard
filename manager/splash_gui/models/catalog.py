"""The curated "Supported" catalog (SPEC §9.1, Appendix C).

The seed lists repositories only; sizes, licenses, vision and the GGUF variant
table come from the Hub at runtime (`HfClient.repo_info`, cached for a day).
Splash's own official list (`install/completions/official-models.txt` in the
package, refreshed into `<Splash data>/catalog/official-models.txt`,
`install/catalog.py`) is merged in so a new official package appears by itself.

The memory estimate is SPEC §9.1's:
`target + draft (~0.7 GB) + vision (~0.9 GB unless language-only) + KV runway
(~0.5 GB) + 2 GB reserve`. Splash's own startup budget check stays the authority.
The variant pick is the largest variant at or below the Q4_K_M class that fits,
falling back to the smaller ones; the authoritative tensor-type check happens in
`/inspect` before a download.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIB = 1024**3
DRAFT_BYTES = int(0.7 * GIB)
VISION_BYTES = int(0.9 * GIB)
KV_RUNWAY_BYTES = int(0.5 * GIB)
RESERVE_BYTES = 2 * GIB

# Appendix C: (family, repo, format, notes, perf note).
SEED: list[tuple[str, str, str, str, str | None]] = [
    (
        "Qwen3.8-27B",
        "mlx-community/Qwen3.8-27B-4bit",
        "mlx",
        "About 21 GB with draft and vision.",
        "≈74 tok/s decode on an M5 Pro (Splash docs)",
    ),
    (
        "Qwen3.8-27B",
        "unsloth/Qwen3.8-27B-GGUF",
        "gguf",
        "Recommended: UD-Q4_K_M (≥ 36 GB), UD-IQ3_XXS (24 GB, use --language-only for agents).",
        "UD-Q4_K_M ≈74 tok/s on an M5 Pro; UD-IQ3_XXS ≈43.5 tok/s on a 24 GB M6",
    ),
    (
        "Qwen3.8-27B",
        "prism-ml/Ternary-Bonsai-2-27B-gguf",
        "gguf",
        "7.2 GB, vision, Hadamard-rotated.",
        None,
    ),
    (
        "Qwen3.8-27B",
        "incoai/Qwen3.8-27B-Splash",
        "legacy",
        "Official catalog. No variant, revision or language-only.",
        None,
    ),
    (
        "Qwen3.6-35B-A3B",
        "mlx-community/Qwen3.6-35B-A3B-4bit",
        "mlx",
        "About 24 GB with draft and vision; fastest decode.",
        "≈210 tok/s decode on an M5 Pro (Splash docs)",
    ),
    (
        "Qwen3.6-35B-A3B",
        "unsloth/Qwen3.6-35B-A3B-GGUF",
        "gguf",
        "Recommended: UD-Q4_K_M (≥ 36 GB), UD-Q2_K_XL (24 GB, 256K context).",
        "UD-Q4_K_M ≈175 tok/s on an M5 Pro; UD-Q2_K_XL ≈145 tok/s on a 24 GB M6",
    ),
    (
        "Qwen3.6-35B-A3B",
        "incoai/Qwen3.6-35B-A3B-Splash",
        "legacy",
        "Official catalog.",
        None,
    ),
]

# Each family's DFlash2 draft, which every install fetches beside the target
# (splash/install/families.py:74, :105; install/upstream.py `_draft_files`).
DRAFT_REPOS: dict[str, str] = {
    "Qwen3.8-27B": "incoai/Qwen3.8-27B-DFlash2",
    "Qwen3.6-35B-A3B": "incoai/Qwen3.6-35B-A3B-DFlash2",
}

_SHARD = re.compile(r"-\d{5}-of-\d{5}$")
_BITS = re.compile(r"(?:I?Q|PQ)(\d)", re.I)
# Variants Splash cannot load, known from the name before `/inspect` reads headers
# (SPEC §3.2, §9.1). splash/DEVELOPMENT.md "GGUF targets" (:599-613): the loader
# takes Q2_K…Q8_0, the IQ formats, MXFP4 and PQ2_0, so every Unsloth file of both
# families loads but UD-Q8_K_XL and BF16; F16/F32 linears are in no accepted list.
UNLOADABLE = {
    "UD-Q8_K_XL": "Splash has no kernels for this variant (UD-Q8_K_XL)",
    "BF16": "Splash has no kernels for BF16 tensors yet",
    "F16": "this GGUF stores F16 tensors Splash cannot load; choose another variant",
    "F32": "this GGUF stores F32 tensors Splash cannot load; choose another variant",
}
# A llama.cpp importance matrix (`imatrix*.gguf`) is calibration data, not a model.
# Splash's own check of unsloth/Qwen3.8-27B-GGUF's `imatrix_unsloth.gguf` says this
# (install/gguf.py via the compatibility helper, run 2026-10-04).
NOT_A_MODEL = "missing or invalid GGUF metadata: general.architecture"

# SPEC §9.1: recommend "the largest variant at or below UD-Q4_K_M-class". Splash
# has no quant order of its own, so the ceiling is the repository's own Q4_K_M
# file size when it has one (Unsloth's UD-Q4_K_XL and Q4_1 are larger), and by
# name otherwise: llama.cpp's quantize table lists Q4_1 larger than Q4_K_M (4.78 G
# vs 4.58 G for Llama-3-8B; tools/quantize/quantize.cpp QUANT_OPTIONS, **[code]**
# https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/quantize.cpp),
# and Unsloth's _L/_XL variants are larger than their _M (unsloth/Qwen3.8-27B-GGUF:
# UD-Q4_K_XL 17.6 GB vs UD-Q4_K_M 16.5 GB on the Hub, 2026-10-04).
CEILING_QUANT = "Q4_K_M"
ABOVE_CEILING = frozenset({"Q4_1", "Q4_K_L", "Q4_K_XL"})


def is_projector(filename: str) -> bool:
    """A vision projector GGUF, as Splash names one: `mmproj` anywhere in the stem
    (`mmproj-BF16.gguf`, Prism's `MODEL-mmproj-BF16.gguf`), install/upstream.py:59-62."""
    return "mmproj" in Path(filename).stem.lower()


def quant(name: str) -> str:
    """`UD-Q4_K_M` → `Q4_K_M`: the llama.cpp quant type behind a variant name."""
    upper = name.upper()
    return upper[3:] if upper.startswith("UD-") else upper


def unloadable_reason(name: str, files: list[str] | None = None) -> str | None:
    """Why Splash cannot load this variant, when its name or file tells."""
    reason = UNLOADABLE.get(name.upper())
    if reason:
        return reason
    if any(Path(f).stem.lower().startswith("imatrix") for f in files or [name]):
        return NOT_A_MODEL
    return None


def q4_k_m_ceiling(sizes: dict[str, int | None]) -> int | None:
    """The size of the repository's own Q4_K_M-class file (largest of `Q4_K_M` and
    `UD-Q4_K_M`), the §9.1 recommendation ceiling; None when it has none."""
    found = [size for name, size in sizes.items() if quant(name) == CEILING_QUANT and size]
    return max(found) if found else None


def within_ceiling(name: str, size: int | None, ceiling: int | None) -> bool:
    """At or below UD-Q4_K_M-class: 4 bits or fewer, not a larger 4-bit type, and
    no bigger than the repository's Q4_K_M file."""
    if (bits(name) or 99) > 4 or quant(name) in ABOVE_CEILING:
        return False
    return ceiling is None or (size or 0) <= ceiling


def family_guess(repo_id: str) -> str | None:
    """For grouping an official package only; support is decided by the config."""
    name = repo_id.lower()
    if "35b-a3b" in name:
        return "Qwen3.6-35B-A3B"
    if "27b" in name:
        return "Qwen3.8-27B"
    return None


def official_ids(pkg: Path | None, data_dir: Path) -> list[str]:
    """Splash's official package list: the bundled seed plus its refreshed cache."""
    out: list[str] = []
    for path in (
        pkg / "install" / "completions" / "official-models.txt" if pkg else None,
        data_dir / "catalog" / "official-models.txt",
    ):
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "/" in line and line not in out:
                out.append(line)
    return out


def memory_need(target: int, *, vision: bool) -> int:
    return target + DRAFT_BYTES + (VISION_BYTES if vision else 0) + KV_RUNWAY_BYTES + RESERVE_BYTES


def fit_for(need: int, memory: int) -> str:
    """SPEC §9.1: Tight above `memsize − 8 GB`, Won't fit above `memsize − 3 GB`."""
    if need <= memory - 8 * GIB:
        return "fits"
    if need <= memory - 3 * GIB:
        return "tight"
    return "wont_fit"


def variant_name(repo_id: str, filename: str) -> str | None:
    """`Qwen3.8-27B-UD-Q4_K_M-00001-of-00002.gguf` → `UD-Q4_K_M`; None for a
    projector or a file below the root."""
    if "/" in filename or not filename.lower().endswith(".gguf"):
        return None
    stem = _SHARD.sub("", filename[: -len(".gguf")])
    if is_projector(filename):
        return None
    base = repo_id.split("/", 1)[1]
    base = re.sub(r"-gguf$", "", base, flags=re.I)
    if stem.lower().startswith(base.lower() + "-"):
        return stem[len(base) + 1 :]
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def bits(name: str) -> int | None:
    if name.upper() in ("BF16", "F16"):
        return 16
    if name.upper() == "F32":
        return 32
    match = _BITS.search(name)
    return int(match.group(1)) if match else None


@dataclass
class Variant:
    name: str
    size: int
    files: list[str]

    @property
    def unloadable(self) -> str | None:
        return unloadable_reason(self.name, self.files)


def gguf_variants(repo_id: str, files: dict[str, int | None]) -> list[Variant]:
    found: dict[str, Variant] = {}
    for filename, size in files.items():
        name = variant_name(repo_id, filename)
        if name is None:
            continue
        variant = found.setdefault(name, Variant(name, 0, []))
        variant.size += size or 0
        variant.files.append(filename)
    return sorted(found.values(), key=lambda v: (v.size, v.name))


def projector(files: dict[str, int | None]) -> str | None:
    """The vision projector Splash would take: BF16 preferred, F32 accepted, F16
    refused (install/upstream.py `select_vision`; Splash reads the headers, this
    goes by name)."""
    names = [f for f in files if "/" not in f and f.lower().endswith(".gguf") and is_projector(f)]
    for kind in ("BF16", "F32"):
        hit = next((f for f in names if kind in f.upper()), None)
        if hit:
            return hit
    return None


def pick_variant(variants: list[Variant], memory: int, vision: bool) -> Variant | None:
    """SPEC §9.1: the largest loadable variant at or below UD-Q4_K_M-class that
    fits; else the largest that is at least tight; else none."""
    ceiling = q4_k_m_ceiling({v.name: v.size for v in variants})
    usable = [
        v
        for v in variants
        if not v.unloadable and v.size > 0 and within_ceiling(v.name, v.size, ceiling)
    ]
    for wanted in ("fits", "tight"):
        candidates = [
            v for v in usable if fit_for(memory_need(v.size, vision=vision), memory) == wanted
        ]
        if candidates:
            return max(candidates, key=lambda v: v.size)
    return None


# The tokenizer files an MLX target may supply (splash/install/upstream.py:26-34).
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "special_tokens_map.json",
)


def selected_files(
    fmt: str, files: dict[str, int | None], variant_files: list[str] | None, *, language_only: bool
) -> list[str]:
    """The target files Splash's installer fetches, from the Hub listing alone, for a
    row that has not been through `/inspect` (which asks Splash itself):
    - MLX (install/upstream.py `_mlx_target`, :167-212): config, tokenizer files, the
      processor config unless language-only, the shard index and every root shard
      (language-only still fetches every shard holding a tensor);
    - GGUF (`_gguf_target`, :140-164): the variant's files, plus the projector unless
      language-only (`variant_files` carries it last when there is one);
    - legacy package: every file, as the compatibility helper lists it."""
    if fmt == "legacy":
        return list(files)
    if fmt == "gguf":
        chosen = list(variant_files or [])
        if language_only:
            chosen = [f for f in chosen if not is_projector(f)]
        return chosen
    wanted = {"config.json", "model.safetensors.index.json", *TOKENIZER_FILES}
    if not language_only:
        wanted.add("preprocessor_config.json")
    return [
        name
        for name in files
        if "/" not in name and (name in wanted or name.endswith(".safetensors"))
    ]


def weights_bytes(files: dict[str, int | None]) -> int:
    """An MLX/legacy repository's download: weights plus config and tokenizer files."""
    return sum(size or 0 for name, size in files.items() if not name.lower().endswith(".md"))


def entry_facts(repo_id: str, fmt: str, info: Any, memory: int) -> dict[str, Any]:
    """Size, memory need, fit, vision, license and variants for one catalog row
    from `HfClient.repo_info`; {} without Hub data."""
    if info is None:
        return {}
    files: dict[str, int | None] = info.files
    facts: dict[str, Any] = {"license": info.license, "last_modified": info.last_modified}
    if fmt == "gguf":
        mmproj = projector(files)
        vision = mmproj is not None
        variants = gguf_variants(repo_id, files)
        picked = pick_variant(variants, memory, vision)
        facts["vision"] = vision
        facts["variants"] = [
            {
                "name": v.name,
                "size_bytes": v.size + ((files.get(mmproj) or 0) if mmproj else 0),
                "bits_per_weight": float(b) if (b := bits(v.name)) else None,
                # False with the reason when the name tells; null = checked by /inspect.
                "loadable": False if v.unloadable else None,
                "reason": v.unloadable,
                "fit": fit_for(memory_need(v.size, vision=vision), memory),
                "recommended": picked is not None and v.name == picked.name,
                "files": v.files + ([mmproj] if mmproj else []),
            }
            for v in variants
        ]
        loadable = [v for v in variants if not v.unloadable]
        target = picked or (loadable[0] if loadable else None)
        if target is not None:
            size = target.size + ((files.get(mmproj) or 0) if mmproj else 0)
            facts["size_bytes"] = size
            facts["memory_need_bytes"] = memory_need(target.size, vision=vision)
            facts["fit"] = fit_for(facts["memory_need_bytes"], memory)
            facts["recommended_variant"] = picked.name if picked else None
        return facts
    size = weights_bytes(files)
    vision = "preprocessor_config.json" in files
    facts.update(
        size_bytes=size,
        vision=vision,
        memory_need_bytes=memory_need(size, vision=vision),
    )
    facts["fit"] = fit_for(facts["memory_need_bytes"], memory)
    return facts
