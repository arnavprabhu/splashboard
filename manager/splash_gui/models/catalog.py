"""The curated "Supported" catalog.

The seed lists repositories only; sizes, licenses, vision and the GGUF variant
table come from the Hub at runtime (`HfClient.repo_info`, cached for a day).

The memory estimate:
`target + draft (~0.7 GB, 1.2 GB for Flash-Next) + vision (~0.9 GB unless
language-only) + KV runway (~0.5 GB) + 2 GB reserve`. A MoE model too large for
that can still run with its routed experts (and Flash-Next's PLE table) streamed
from the SSD (`streamed_need`), which the fit calls "streams". Splash's own
startup budget check stays the authority.
The variant pick is the largest variant at or below the Q4_K_M class that fits,
falling back to the smaller ones, and to the smallest that streams when none
fits whole; the authoritative tensor-type check happens in `/inspect` before a
download.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIB = 1024**3
DRAFT_BYTES = int(0.7 * GIB)
# A family whose DFlash2 draft is larger: incoai/Qwen3.8-Flash-Next-DFlash2's
# model.safetensors is 1.21 GB (Hub, 2026-10-20).
FAMILY_DRAFT_BYTES: dict[str, int] = {"Qwen3.8-Flash-Next": 1_210_000_000}
VISION_BYTES = int(0.9 * GIB)
KV_RUNWAY_BYTES = int(0.5 * GIB)
RESERVE_BYTES = 2 * GIB

# (family, repo, group, notes, perf note). The group is the row's format, but for
# "nvfp4": a Model Optimizer or compressed-tensors checkpoint, a safetensors target
# like MLX's (format "mlx") listed apart (splash/DEVELOPMENT.md "Model Optimizer
# targets" names nvidia/Qwen3.8-27B-NVFP4). Both NVFP4 rows were checked on the Hub
# on 2026-10-14.
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
        "nvidia/Qwen3.8-27B-NVFP4",
        "nvfp4",
        "NVIDIA's NVFP4 checkpoint (Model Optimizer), about 22 GB of weights.",
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
        "mlx-community/Qwen3.6-35B-A3B-nvfp4",
        "mlx",
        "MLX nvfp4, about 20 GB of weights.",
        None,
    ),
    # Flash-Next loads from a GGUF only, and every GGUF of it on the Hub is split
    # into a folder per variant. Splash's README names the ISTA-DASLab Q2_0 as its
    # example and links unsloth's repository for the other variants; both checked
    # on the Hub on 2026-10-20.
    (
        "Qwen3.8-Flash-Next",
        "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF",
        "gguf",
        "Recommended: Q2_0 (about 66 GB in two files; streams its experts and PLE table "
        "on a 64 GB Mac).",
        "Q2_0 ≈95 tok/s decode on a 64 GB M5 Pro (Splash docs)",
    ),
    (
        "Qwen3.8-Flash-Next",
        "unsloth/Qwen3.8-Flash-Next-GGUF",
        "gguf",
        "UD-IQ1_S to UD-Q6_K_XL and Q8_0, each in split files.",
        None,
    ),
]

# Each family's DFlash2 draft, which every install fetches beside the target
# (splash/install/families.py `FAMILIES`; install/upstream.py `draft_files`).
DRAFT_REPOS: dict[str, str] = {
    "Qwen3.8-27B": "incoai/Qwen3.8-27B-DFlash2",
    "Qwen3.6-35B-A3B": "incoai/Qwen3.6-35B-A3B-DFlash2",
    "Qwen3.8-Flash-Next": "incoai/Qwen3.8-Flash-Next-DFlash2",
}
# A family without a released draft (Splash's `draft_repo` is None) needs one named
# in the model's settings; Splash refuses to install it otherwise.
NO_DRAFT = (
    "Splash has no released draft for this model's family; set Draft model in the model's settings"
)

# Each family's label in the catalog.
FAMILY_LABELS: dict[str, str] = {
    "Qwen3.8-27B": "Qwen3.8-27B dense",
    "Qwen3.6-35B-A3B": "Qwen3.6-35B-A3B MoE",
    "Qwen3.8-Flash-Next": "Qwen3.8-Flash-Next MoE",
}

_SHARD = re.compile(r"-\d{5}-of-\d{5}$")
# One part of a split GGUF (install/upstream.py `SPLIT_NAME`).
SPLIT_NAME = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
# A multi-token-prediction head published beside the model
# (`mtp-Qwen3.8-Flash-Next-Q8_0.gguf`), never a model of its own.
_MTP = re.compile(r"(?:^|[-_.])mtp(?:[-_.]|$)", re.IGNORECASE)
ROOT_SPLIT = "Splash installs a split GGUF only from a folder named after its variant"
_BITS = re.compile(r"(?:I?Q|PQ)(\d)", re.I)
# MXFP4 and NVFP4 (llama.cpp's MXFP4_MOE, NVFP4) are 4-bit formats.
_FP4 = re.compile(r"(?:MX|NV)FP4", re.I)
# Variants Splash cannot load, known from the name before `/inspect` reads headers.
# Splash loads BF16 and F16 tensors inside a quantized GGUF (UD-Q8_K_XL's
# among them) but refuses a whole unquantized model, one whose projections and
# experts are all BF16, F16 or F32 (install/gguf.py `require_loadable`), which is
# what a BF16, F16 or F32 variant is.
WHOLE_UNQUANTIZED = (
    "Splash loads quantized GGUFs, not whole BF16, F16 or F32 models; choose a quantized variant"
)
UNLOADABLE = dict.fromkeys(("BF16", "F16", "F32"), WHOLE_UNQUANTIZED)
# A llama.cpp importance matrix (`imatrix*.gguf`) is calibration data, not a model.
# Splash's own check of unsloth/Qwen3.8-27B-GGUF's `imatrix_unsloth.gguf` says this
# (install/gguf.py via the compatibility helper, run 2026-10-04).
NOT_A_MODEL = "missing or invalid GGUF metadata: general.architecture"

# Recommend "the largest variant at or below UD-Q4_K_M-class". Splash
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
    (`mmproj-BF16.gguf`, Prism's `MODEL-mmproj-BF16.gguf`), install/upstream.py
    `projector_named`."""
    return "mmproj" in Path(filename).stem.lower()


def is_mtp_head(filename: str) -> bool:
    """A multi-token-prediction head (`mtp-MODEL-Q8_0.gguf`), which no variant is."""
    return bool(_MTP.search(Path(filename).stem))


def installable_id(repo_id: str, variant: str | None, files: Iterable[str]) -> str:
    """The model ID Splash installs for `repo_id:variant`. In a repository with one
    root GGUF besides its projector, Splash's `select_gguf` strips that file's whole
    name as the shared model name, so a :VARIANT that is the whole name
    (`q4_k_m.gguf` as :q4_k_m) matches nothing. Without :VARIANT Splash takes the
    only GGUF, so such an ID is installed as the bare repository. Its own -VARIANT
    ending (:Q4_K_M for `X-Q4_K_M.gguf`) resolves and is kept."""
    if variant is None:
        return repo_id
    names = list(files)
    targets = [n for n in names if "/" not in n and n.endswith(".gguf") and not is_projector(n)]
    # A bare ID with safetensors in the repository is read as an MLX model
    # (install/upstream.py `inspect_target`).
    if (
        len(targets) == 1
        and Path(targets[0]).stem.lower() == variant.lower()
        and not any(n.endswith(".safetensors") for n in names)
    ):
        return repo_id
    return f"{repo_id}:{variant}"


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
    `UD-Q4_K_M`), the recommendation ceiling; None when it has none."""
    found = [size for name, size in sizes.items() if quant(name) == CEILING_QUANT and size]
    return max(found) if found else None


def within_ceiling(name: str, size: int | None, ceiling: int | None) -> bool:
    """At or below UD-Q4_K_M-class: 4 bits or fewer, not a larger 4-bit type, and
    no bigger than the repository's Q4_K_M file."""
    if (bits(name) or 99) > 4 or quant(name) in ABOVE_CEILING:
        return False
    return ceiling is None or (size or 0) <= ceiling


def family_guess(repo_id: str) -> str | None:
    """A guess from the name, for grouping only; support is decided by the config."""
    name = repo_id.lower()
    if "flash-next" in name:
        return "Qwen3.8-Flash-Next"
    if "35b-a3b" in name:
        return "Qwen3.6-35B-A3B"
    if "27b" in name:
        return "Qwen3.8-27B"
    return None


# The KV cache's geometry per family: (full-attention layers, KV heads, head dimension).
# Splash's hybrid models run a full-attention layer every fourth layer
# (runtime/model/Qwen3_8.hpp, Qwen3_6Moe.hpp and Qwen4Exp.hpp: `fullAttentionPeriod = 4`;
# 64, 40 and 48 layers), with 4, 2 and 2 KV heads of 256 (`attentionKvHeads`,
# `attentionHeadDimension`).
KV_GEOMETRY: dict[str, tuple[int, int, int]] = {
    "Qwen3.8-27B": (16, 4, 256),
    "Qwen3.6-35B-A3B": (10, 2, 256),
    "Qwen3.8-Flash-Next": (12, 2, 256),
}
# Flash-Next's KV pages also hold its sparse attention indexer's pooled keys: 128
# values in bf16 per block of 4 tokens in each attention layer, 64 bytes a token a
# layer (runtime/ops/PagedKv.hpp `indexBytesPerLayerPage`; Qwen4Exp.hpp
# `indexerHeadDimension`, `indexerBlockTokens`).
KV_INDEX_BYTES_PER_LAYER_TOKEN: dict[str, int] = {"Qwen3.8-Flash-Next": 128 * 2 // 4}


def kv_bytes(family: str, tokens: int, *, bf16: bool = False) -> int:
    """The KV cache a context of `tokens` takes (runtime/ops/PagedKv.hpp `bytesPerModelPage`).
    The default INT8 format stores each head's values as one byte each plus one float32
    scale per token and head, for keys and for values; BF16 stores two bytes each. An
    indexer's keys come on top, whatever the format."""
    layers, heads, dim = KV_GEOMETRY[family]
    per_token = layers * 2 * heads * (dim * 2 if bf16 else dim + 4)
    per_token += layers * KV_INDEX_BYTES_PER_LAYER_TOKEN.get(family, 0)
    return per_token * tokens


# The SSD cache stages a state's write through one resident buffer of a state's size,
# which the memory plan sets aside (runtime/engine/RuntimeResources.mm, "a state's write
# staging"). bf16 recurrent states (`--state-format bf16`) halve it. Flash-Next's is its
# 36 linear-attention layers' recurrent state, 48 heads of 128 × 128 fp32 (108 MiB), with
# its convolution, PLE and indexer history (runtime/model/QwenHybridLayout.hpp
# `gdnStateLayout`): about 113 MiB, an estimate until the engine reports it.
STATE_STAGING_BYTES: dict[str, int] = {
    "Qwen3.8-27B": 153 * 1024**2,
    "Qwen3.6-35B-A3B": 64 * 1024**2,
    "Qwen3.8-Flash-Next": 113 * 1024**2,
}


def state_staging_bytes(family: str, cache_bytes: int | None, state_format: str = "fp32") -> int:
    """The staging an SSD cache of `cache_bytes` (`--max-cache-disk`) costs in the plan.
    Only an explicit size counts: with the automatic quota (None) Splash sets the staging
    aside only where the model and the requested context already fit beside it, and 0
    turns the cache off."""
    if not cache_bytes:
        return 0
    staging = STATE_STAGING_BYTES.get(family, 0)
    return staging // 2 if state_format == "bf16" else staging


def memory_need(
    target: int,
    *,
    vision: bool,
    kv: int | None = None,
    staging: int = 0,
    family: str | None = None,
) -> int:
    """`kv` is the KV cache the context needs (`kv_bytes`); the default is the 64-page runway.
    `staging` is the SSD cache's state staging (`state_staging_bytes`), 0 by default.
    `family` sizes the draft (`FAMILY_DRAFT_BYTES`)."""
    cache = KV_RUNWAY_BYTES if kv is None else max(kv, KV_RUNWAY_BYTES)
    draft = FAMILY_DRAFT_BYTES.get(family or "", DRAFT_BYTES)
    return target + draft + (VISION_BYTES if vision else 0) + cache + staging + RESERVE_BYTES


# A MoE target's routed experts can stay in their files on the SSD and stream through
# a store of at least one layer's experts (splash/DEVELOPMENT.md "Streamed experts"),
# and Flash-Next's PLE n-gram table through a row cache of at most 512 MiB ("PLE table
# streaming"). Per MoE family: the routed experts' share of the weights outside the
# PLE table, and the layers they spread over. 35B-A3B's are 256 experts of 3 × 2048 ×
# 512 in each of 40 layers, 32B of its 35B weights; Flash-Next's 512 of 3 × 2560 × 640
# in each of 48 (Qwen4Exp.hpp), 121B of about 126B, and its quantizations keep the rest
# wider: ISTA-DASLab's Q2_0 has about 34 GB of experts in its 37.6 GB first part.
STREAMED_EXPERTS: dict[str, tuple[float, int]] = {
    "Qwen3.6-35B-A3B": (0.9, 40),
    "Qwen3.8-Flash-Next": (0.9, 48),
}
# Flash-Next's PLE table: 320M rows of 160 values, 28.8 GB in IQ4_NL (DEVELOPMENT.md),
# the second file of each ISTA-DASLab variant. Without a header to read, a variant's
# table is taken as that, or 45% of its bytes when it is smaller than 64 GB.
PLE_BYTES: dict[str, int] = {"Qwen3.8-Flash-Next": 28_800_000_000}
PLE_SHARE = 0.45
PLE_CACHE_MAX = 512 * 1024**2


def streamed_need(
    family: str | None, target: int, *, vision: bool, kv: int | None = None, staging: int = 0
) -> int | None:
    """The least memory the target runs in with its routed experts and PLE table
    streamed from the SSD: the rest of its weights, one layer's experts and the PLE
    row cache, with `memory_need`'s other terms. None for a family that streams nothing
    (a dense model). An estimate from the file sizes; Splash's own plan decides."""
    if family not in STREAMED_EXPERTS:
        return None
    share, layers = STREAMED_EXPERTS[family]
    ple = min(PLE_BYTES.get(family, 0), int(target * PLE_SHARE))
    experts = int((target - ple) * share)
    resident = target - experts - ple + experts // layers + min(ple, PLE_CACHE_MAX)
    return memory_need(resident, vision=vision, kv=kv, staging=staging, family=family)


def fit_for(need: int, memory: int, streamed: int | None = None) -> str:
    """Tight above `memsize − 8 GB`, Won't fit above `memsize − 3 GB`, unless the
    streamed need (`streamed_need`) is within that: then it streams."""
    if need <= memory - 8 * GIB:
        return "fits"
    if need <= memory - 3 * GIB:
        return "tight"
    if streamed is not None and streamed <= memory - 3 * GIB:
        return "streams"
    return "wont_fit"


def fit_of(target: int, memory: int, *, vision: bool, family: str | None = None) -> str:
    """The fit of `target` bytes of weights on a Mac of `memory`, streamed or not."""
    need = memory_need(target, vision=vision, family=family)
    return fit_for(need, memory, streamed_need(family, target, vision=vision))


def variant_name(repo_id: str, filename: str) -> str | None:
    """`Qwen3.8-27B-UD-Q4_K_M-00001-of-00002.gguf` → `UD-Q4_K_M`; a split GGUF's part
    in a folder named after its variant (`Q2_0/MODEL-Q2_0-00001-of-00002.gguf`) → the
    folder, `Q2_0`. None for a projector, a root MTP head or a file deeper down; an MTP
    head inside a variant's folder counts, as Splash's `split_gguf` counts it."""
    if filename.count("/") > 1 or not filename.lower().endswith(".gguf"):
        return None
    if is_projector(filename):
        return None
    if "/" in filename:
        return filename.split("/", 1)[0]
    if is_mtp_head(filename):
        return None
    stem = _SHARD.sub("", filename[: -len(".gguf")])
    base = repo_id.split("/", 1)[1]
    base = re.sub(r"-gguf$", "", base, flags=re.I)
    if stem.lower().startswith(base.lower() + "-"):
        return stem[len(base) + 1 :]
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def split_complete(names: list[str]) -> bool:
    """Whether `names` are every part of one split GGUF, 1 to N, each once
    (install/upstream.py `split_order`)."""
    parts: dict[int, int] = {}
    for name in names:
        match = SPLIT_NAME.search(name)
        if match is None or int(match.group(1)) in parts:
            return False
        parts[int(match.group(1))] = int(match.group(2))
    counts = set(parts.values())
    return len(counts) == 1 and sorted(parts) == list(range(1, counts.pop() + 1))


def root_choice(files: Iterable[str], variant: str) -> str | None:
    """The root GGUF Splash's `select_gguf` takes for :VARIANT before it looks in a
    VARIANT/ folder: the file named the shared model name then -VARIANT, else the only
    one ending in -VARIANT; None when no root file answers (install/upstream.py
    `select_gguf`). Splash counts every root GGUF but its projectors, MTP heads too."""
    roots = sorted(n for n in files if "/" not in n and n.endswith(".gguf") and not is_projector(n))
    words = [Path(n).stem.split("-") for n in roots]
    shared = len(os.path.commonprefix(words))
    wanted = variant.lower()
    exact = [n for n, w in zip(roots, words, strict=True) if "-".join(w[shared:]).lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    ending = [n for n in roots if Path(n).stem.lower().endswith("-" + variant.lower())]
    return ending[0] if len(ending) == 1 else None


def bits(name: str) -> int | None:
    if name.upper() in ("BF16", "F16"):
        return 16
    if name.upper() == "F32":
        return 32
    if _FP4.search(name):
        return 4
    match = _BITS.search(name)
    return int(match.group(1)) if match else None


# Quality tier: the label from a variant's bits per weight, lowest floor last.
QUALITY_TIERS: tuple[tuple[int, str], ...] = (
    (8, "Highest"),
    (5, "Higher"),
    (4, "Balanced"),
    (0, "Compact"),
)


def quality_tier(name: str, files: list[str] | None = None) -> str | None:
    """The quality tier label of a GGUF variant, from its bits (`bits`, by name). None for a
    variant Splash cannot load (`unloadable_reason`), so its row shows a dash, and for a name
    without a bit width."""
    if unloadable_reason(name, files) is not None:
        return None
    width = bits(name)
    if width is None:
        return None
    return next(label for floor, label in QUALITY_TIERS if width >= floor)


@dataclass
class Variant:
    name: str
    size: int
    files: list[str]
    # Why Splash cannot install the variant from where its files lie, when it can't.
    layout: str | None = None

    @property
    def unloadable(self) -> str | None:
        return self.layout or unloadable_reason(self.name, self.files)


def gguf_variants(repo_id: str, files: dict[str, int | None]) -> list[Variant]:
    """The repository's GGUF variants: its root files by name, and each split GGUF
    kept in a folder named after its variant, which Splash installs as that
    :VARIANT (install/upstream.py `split_gguf`). A root variant hides a folder of
    the same name, which Splash never reaches."""
    found: dict[str, Variant] = {}
    for filename, size in files.items():
        name = variant_name(repo_id, filename)
        if name is None:
            continue
        variant = found.setdefault(name, Variant(name, 0, []))
        variant.size += size or 0
        variant.files.append(filename)
    roots = {v.name.lower() for v in found.values() if all("/" not in f for f in v.files)}
    variants = []
    for variant in found.values():
        variant.files.sort()
        folder = [f for f in variant.files if "/" in f]
        if not folder:
            if len(variant.files) > 1 or SPLIT_NAME.search(variant.files[0]):
                variant.layout = ROOT_SPLIT
        elif len(folder) < len(variant.files) or variant.name.lower() in roots:
            # The root file of the same name is the variant; the folder is not.
            variant.files = [f for f in variant.files if "/" not in f]
            variant.size = sum(files.get(f) or 0 for f in variant.files)
            if not variant.files:
                continue
        elif all(is_mtp_head(f) for f in folder):
            # A folder of MTP heads only (`MTP/`) is no variant.
            continue
        elif heads := [f for f in folder if is_mtp_head(f)]:
            variant.layout = (
                f"Splash counts {heads[0]} as a part of the split GGUF in {variant.name}/"
            )
        elif not split_complete(folder):
            variant.layout = f"the split GGUF in {variant.name}/ is incomplete"
        elif (taken := root_choice(files, variant.name)) is not None:
            variant.layout = (
                f"Splash takes {taken} for :{variant.name}, not the files in {variant.name}/"
            )
        variants.append(variant)
    return sorted(variants, key=lambda v: (v.size, v.name))


def projector(files: dict[str, int | None]) -> str | None:
    """The vision projector Splash would take: BF16, F32 or F16, preferred in that
    order (install/upstream.py `select_vision`; Splash reads the headers, this goes
    by name)."""
    names = [f for f in files if "/" not in f and f.lower().endswith(".gguf") and is_projector(f)]
    for kind in ("BF16", "F32", "F16"):
        hit = next(
            (f for f in names if kind in f.upper() and (kind != "F16" or "BF16" not in f.upper())),
            None,
        )
        if hit:
            return hit
    return None


def pick_variant(
    variants: list[Variant], memory: int, vision: bool, family: str | None = None
) -> Variant | None:
    """The largest loadable variant at or below UD-Q4_K_M-class that
    fits; else the largest that is at least tight; else the smallest that runs
    streamed from the SSD, which reads the fewest bytes a token; else none."""
    ceiling = q4_k_m_ceiling({v.name: v.size for v in variants})
    usable = [
        v
        for v in variants
        if not v.unloadable and v.size > 0 and within_ceiling(v.name, v.size, ceiling)
    ]
    fits = {v.name: fit_of(v.size, memory, vision=vision, family=family) for v in usable}
    for wanted in ("fits", "tight"):
        candidates = [v for v in usable if fits[v.name] == wanted]
        if candidates:
            return max(candidates, key=lambda v: v.size)
    streams = [v for v in usable if fits[v.name] == "streams"]
    return min(streams, key=lambda v: v.size) if streams else None


def default_variant(
    repo_id: str,
    variants: list[Variant],
    memory: int,
    *,
    vision: bool,
    preset_model: str | None = None,
    refused: frozenset[str] | set[str] = frozenset(),
    family: str | None = None,
) -> str | None:
    """The GGUF variant the GUI recommends for this Mac, wherever it is shown (the
    catalog, `/inspect`, and the variant `/inspect` checks first): the preset's
    pick's own variant when the pick is this repository, else `pick_variant`.
    `refused` are variants Splash's check turned down; `family` is the model's
    (`family_guess` when unknown), which decides whether it can stream."""
    if preset_model:
        repo, _, name = preset_model.partition(":")
        if repo == repo_id and name and name not in refused:
            return name
    picked = pick_variant(
        [v for v in variants if v.name not in refused],
        memory,
        vision,
        family or family_guess(repo_id),
    )
    return picked.name if picked else None


# The tokenizer files an MLX target may supply (splash/install/upstream.py `TOKENIZER_FILES`).
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "special_tokens_map.json",
)


# Where an MLX repository keeps its image processor's configuration: a file of its
# own, or processor_config.json, where newer Transformers releases save it
# (splash/install/upstream.py `PROCESSOR_FILES`).
PROCESSOR_FILES = ("preprocessor_config.json", "processor_config.json")


def selected_files(
    fmt: str, files: dict[str, int | None], variant_files: list[str] | None, *, language_only: bool
) -> list[str]:
    """The target files Splash's installer fetches, from the Hub listing alone, for a
    row that has not been through `/inspect` (which asks Splash itself):
    - MLX (install/upstream.py `mlx_target`): config, tokenizer files, the
      processor config unless language-only, the shard index and every root shard
      (language-only still fetches every shard holding a tensor);
    - GGUF (`gguf_target`): the variant's files, one or a split GGUF's parts, plus
      the projector unless language-only (`variant_files` carries it last when
      there is one)."""
    if fmt == "gguf":
        chosen = list(variant_files or [])
        if language_only:
            chosen = [f for f in chosen if not is_projector(f)]
        return chosen
    wanted = {"config.json", "model.safetensors.index.json", *TOKENIZER_FILES}
    if not language_only:
        wanted.update(PROCESSOR_FILES)
    return [
        name
        for name in files
        if "/" not in name and (name in wanted or name.endswith(".safetensors"))
    ]


def weights_bytes(files: dict[str, int | None]) -> int:
    """An MLX repository's download: weights plus config and tokenizer files."""
    return sum(size or 0 for name, size in files.items() if not name.lower().endswith(".md"))


def entry_facts(
    repo_id: str, fmt: str, info: Any, memory: int, family: str | None = None
) -> dict[str, Any]:
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
        picked = pick_variant(variants, memory, vision, family)
        facts["vision"] = vision
        facts["variants"] = [
            {
                "name": v.name,
                "size_bytes": v.size + ((files.get(mmproj) or 0) if mmproj else 0),
                "bits_per_weight": float(b) if (b := bits(v.name)) else None,
                "quality": quality_tier(v.name, v.files),
                # False with the reason when the name tells; null = checked by /inspect.
                "loadable": False if v.unloadable else None,
                "reason": v.unloadable,
                "fit": fit_of(v.size, memory, vision=vision, family=family),
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
            facts["memory_need_bytes"] = memory_need(target.size, vision=vision, family=family)
            facts["fit"] = fit_of(target.size, memory, vision=vision, family=family)
            facts["recommended_variant"] = picked.name if picked else None
        return facts
    size = weights_bytes(files)
    vision = any(name in files for name in PROCESSOR_FILES)
    facts.update(
        size_bytes=size,
        vision=vision,
        memory_need_bytes=memory_need(size, vision=vision, family=family),
    )
    facts["fit"] = fit_of(size, memory, vision=vision, family=family)
    return facts
