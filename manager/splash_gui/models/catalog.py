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

_SHARD = re.compile(r"-\d{5}-of-\d{5}$")
_BITS = re.compile(r"(?:I?Q|PQ)(\d)", re.I)
# Variants Splash cannot load (SPEC §3.2, §9.1).
UNLOADABLE = {
    "UD-Q8_K_XL": "Splash has no kernels for this variant (UD-Q8_K_XL)",
    "BF16": "Splash has no kernels for BF16 tensors yet",
}


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
    if stem.lower().startswith("mmproj"):
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
        return UNLOADABLE.get(self.name.upper()) or UNLOADABLE.get(self.name)


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
    refused (install/upstream.py)."""
    names = [f for f in files if "/" not in f and f.lower().startswith("mmproj")]
    for kind in ("BF16", "F32"):
        hit = next((f for f in names if kind in f.upper()), None)
        if hit:
            return hit
    return None


def pick_variant(variants: list[Variant], memory: int, vision: bool) -> Variant | None:
    """The largest loadable variant at or below 4 bits that fits; else the largest
    that is at least tight; else none."""
    usable = [v for v in variants if not v.unloadable and (bits(v.name) or 99) <= 4 and v.size > 0]
    for wanted in ("fits", "tight"):
        candidates = [
            v for v in usable if fit_for(memory_need(v.size, vision=vision), memory) == wanted
        ]
        if candidates:
            return max(candidates, key=lambda v: v.size)
    return None


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
                "loadable": False if v.unloadable else None,
                "reason": v.unloadable,
                "fit": fit_for(memory_need(v.size, vision=vision), memory),
                "recommended": picked is not None and v.name == picked.name,
                "files": v.files + ([mmproj] if mmproj else []),
            }
            for v in variants
        ]
        target = picked or (variants[0] if variants else None)
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
