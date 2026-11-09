# Derived from Splash (github.com/incoai/splash, Apache-2.0), modified by the Splashboard authors.
# See LICENSE.splash and NOTICE in scripts/fake_splash.
"""Small compatibility stand-in for splash/install/upstream.py (1.6.0).

Mirrors its `Target`, `check_model` and the screening order of
`inspect_target(repo, variant, language_only, scratch)`; the GGUF target selection
(`root_ggufs` to `split_order`) is copied from it, with type annotations added: a
GGUF's variant must be selected (one root file, a split GGUF's parts at the root,
or a split GGUF's parts in a folder named after the variant) and loadable, a
safetensors checkpoint must be quantized in a format the
kernels hold (MLX's, or NVFP4 or FP8 from Model Optimizer or compressed-tensors) and
be of a family that loads one, and vision needs a usable projector. The real
`check_model` runs the engine's `model-check`; the fake has no engine, so
`signatures.check` stands in for it, and an unsupported config fails here rather
than in the manager.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import families, models, signatures


@dataclass(frozen=True)
class Target:
    """What the repository supplies, known before any weight download."""

    format: str
    vision_format: str
    config: Path
    # The directory of a GGUF's headers (1.6.0; its scalar metadata as a file before).
    gguf_headers: Path | None
    family: families.ModelFamily
    files: dict[str, str]


def check_model(target_format, vision_format, config, *, gguf_headers=None, draft=None):
    """The family the "engine" finds config (a path) to describe. A family that
    loads from a GGUF only refuses any other target with the engine's words
    (ModelDescriptor.mm describeSourceModel)."""
    family = signatures.check(json.loads(Path(config).read_text()))
    if target_format != "gguf" and family.name in signatures.GGUF_ONLY:
        raise models.ModelError(f"{family.name} loads from a GGUF only")
    return family


def inspect_target(repo, variant, language_only, scratch):
    try:
        if variant is not None or not any(n.endswith(".safetensors") for n in repo.files):
            return _gguf_target(repo, variant, language_only, Path(scratch))
        return mlx_target(repo, language_only, Path(scratch))
    except models.VisionError as error:
        raise models.VisionError(f"{error}; use --language-only to serve text only") from None


def select_vision(repo):
    """The one vision projector Splash can use, chosen by precision.

    `splash/install/upstream.py select_vision` keeps only a root `clip` projector
    whose tensors are BF16, F32 or F16, preferred in that order.
    """
    usable: dict[str, list[str]] = {"BF16": [], "F32": [], "F16": []}
    found: list[str] = []
    for name in sorted(n for n in root_ggufs(repo.files) if projector_named(n)):
        precision = next((p for p in ("BF16", "F32", "F16") if p in name), None)
        found.append(f"{name} (clip: {precision or 'unknown'})")
        if precision in usable:
            usable[precision].append(name)
    for precision in ("BF16", "F32", "F16"):
        if len(usable[precision]) == 1:
            return usable[precision][0]
        if len(usable[precision]) > 1:
            raise models.VisionError(
                f"several {precision} vision projectors, " + ", ".join(usable[precision]) + ", describe no single tower"
            )
    raise models.VisionError(
        "the GGUF repository has no BF16, F32 or F16 vision projector ("
        + ("; ".join(found) or "no GGUF named mmproj")
        + ")"
    )


def root_ggufs(files):
    """The GGUF files at a repository's root, named *.gguf as the native
    loader finds a target. Files in a subfolder count only as the parts of a
    split GGUF in its VARIANT/ folder (split_gguf); others (MTP heads) never
    do."""
    return sorted(n for n in files if "/" not in n and n.endswith(".gguf"))


def projector_named(name):
    """Whether a root GGUF is named as a vision projector: mmproj-*.gguf, or
    MODEL-mmproj-*.gguf as Prism ML names theirs."""
    return "mmproj" in Path(name).stem.lower()


SPLIT_NAME = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")


def model_of(name):
    """The model a GGUF file holds, by which :VARIANT names a target: its
    name without .gguf and, for a split GGUF's part, without
    -0000K-of-0000N."""
    match = SPLIT_NAME.search(name)
    return name[: match.start()] if match else name.removesuffix(".gguf")


def target_of(name):
    """The target a GGUF file is one of, by which a repository root's files
    group into targets, remote and local alike: its model (model_of), and
    whether it is a split GGUF's part. A whole file is a target of its own,
    apart from any split parts of its model, such as the file they merge
    into."""
    return model_of(name), SPLIT_NAME.search(name) is not None


@dataclass(frozen=True)
class GgufTarget:
    """A GGUF target among a repository's files, which :VARIANT selects by
    its name: one file or a split GGUF's parts at the root (root_targets), or
    a split GGUF's parts in a folder of its own (folder_targets)."""

    # Its model's name (model_of) at the root; its folder's name in a folder.
    name: str
    # Whether its files are a split GGUF's parts: a root target's named
    # NAME-0000K-of-0000N (target_of), and a folder's always, as a folder
    # holds a split GGUF's parts by convention.
    split: bool
    # Its files, in name order, which is a whole split GGUF's split order.
    files: tuple[str, ...]

    def __str__(self):
        """How a list of targets names it: X, or X (split, N parts)."""
        if not self.split:
            return self.name
        count = len(self.files)
        return f"{self.name} (split, {count} part{'' if count == 1 else 's'})"


def root_targets(files):
    """The GGUF targets at a repository's root (target_of), in name order, a
    whole file before the split GGUF of its name. Vision projectors are never
    targets."""
    targets: dict[tuple[str, bool], list[str]] = {}
    for name in root_ggufs(files):
        if not projector_named(name):
            targets.setdefault(target_of(name), []).append(name)
    return [GgufTarget(model, split, tuple(names)) for (model, split), names in sorted(targets.items())]


def folder_targets(files):
    """The split GGUFs a repository keeps in folders named after their
    variants, as repositories publish the files too large for one
    (VARIANT/MODEL-VARIANT-00001-of-0000N.gguf ...): each folder's GGUF files
    but vision projectors, in name order. :VARIANT names a folder whatever
    its case, so folders whose names differ in case alone are one target,
    named as the first is."""
    targets: dict[str, tuple[str, list[str]]] = {}
    for name in sorted(files):
        folder, _, file = name.partition("/")
        if file and "/" not in file and file.endswith(".gguf") and not projector_named(file):
            targets.setdefault(folder.lower(), (folder, []))[1].append(name)
    return [GgufTarget(folder, True, tuple(names)) for folder, names in sorted(targets.values())]


def target_files(target):
    """The files of a root target (root_targets) as the native loader reads
    them: its one file, or its split GGUF's parts, all present, in split
    order."""
    if not target.split:
        return list(target.files)
    return split_order(target.files, f"the split GGUF {target.name}")


def select_gguf(files, variant):
    """The target GGUF among a repository's targets, as the list of its files
    in split order (target_files; split_gguf), and whether it was taken by
    its -VARIANT ending from several targets, which the caller reports.
    :VARIANT names, in this order: the root target (root_targets) whose name
    is the model name all of them share, then -VARIANT (X-Q4_K_M for Q4_K_M,
    not X-UD-Q4_K_M); the folder named VARIANT (split_gguf); the only root
    target whose name ends in -VARIANT. A repository of one root name and no
    folder names no variant apart from its model, so its one match needs no
    report. Without :VARIANT, the root targets' only name. A name selects
    its target by named_target. Anything else is an error listing every
    target :VARIANT can select, root targets then folders."""
    targets = root_targets(files)
    names = sorted({target.name for target in targets})
    if variant is None:
        if target := named_target(targets, names):
            return target_files(target), False
        choice = "select a GGUF with OWNER/REPO:VARIANT"
    else:
        words = [name.split("-") for name in names]
        shared = len(os.path.commonprefix(words))
        exact = [
            name for name, own in zip(names, words, strict=True) if "-".join(own[shared:]).lower() == variant.lower()
        ]
        if target := named_target(targets, exact):
            return target_files(target), False
        if split := split_gguf(files, variant):
            return split, False
        suffix = "-" + variant.lower()
        ending = [name for name in names if name.lower().endswith(suffix)]
        if target := named_target(targets, ending):
            several = len(names) + len(selectable_folders(files)) > 1
            return target_files(target), several
        choice = "no single GGUF matches :" + variant
    listed = ", ".join(map(str, targets + selectable_folders(files))) or "none"
    raise models.ModelError(f"{choice} (GGUF targets: {listed})")


def named_target(targets, names):
    """The root target (root_targets) that names, distinct names of root
    targets, select when they are one name: the name's whole file before the
    split GGUF of that name, as both hold one model, a merge and its parts;
    None for no name or several. :VARIANT selects by the names it matches,
    and a repository or a local directory without one by all of its root
    targets' names."""
    if len(names) != 1:
        return None
    [name] = names
    return min((t for t in targets if t.name == name), key=lambda t: t.split)


def selectable_folders(files):
    """The folder targets (folder_targets) :VARIANT selects (split_gguf):
    those whose files are one whole split GGUF, not an incomplete one, nor a
    folder of other GGUF files such as an MTP head."""
    selectable = []
    for target in folder_targets(files):
        try:
            split_gguf(files, target.name)
        except models.ModelError:
            continue
        selectable.append(target)
    return selectable


def split_gguf(files, variant):
    """The parts of the split GGUF a repository keeps in the folder :VARIANT
    names (folder_targets), in split order; None when there is no such
    folder. Every part from 1 to N must be present."""
    for target in folder_targets(files):
        if target.name.lower() == variant.lower():
            return split_order(target.files, f"the split GGUF in {variant}/")
    return None


def split_order(names, split):
    """names, the files of one split GGUF, in split order: the parts of one
    name, each NAME-0000K-of-0000N.gguf, every part from 1 to N present once.
    The engine holds a target's files to the same rule (findTargetGgufs).
    split names the split GGUF in a refusal."""
    matches = {name: SPLIT_NAME.search(name) for name in names}
    for name, match in matches.items():
        if not match:
            raise models.ModelError(f"{name} is not one part of {split} (NAME-0000K-of-0000N.gguf)")
    if len(own := {model_of(Path(name).name) for name in names}) > 1:
        raise models.ModelError(
            f"{split} mixes the parts of several split GGUFs "
            f"({', '.join(sorted(own))}); a split GGUF is the parts of one name, "
            "NAME-0000K-of-0000N.gguf"
        )
    parts: dict[int, tuple[int, str]] = {}
    for name, match in matches.items():
        assert match is not None
        number = int(match.group(1))
        if number in parts:
            raise models.ModelError(f"{parts[number][1]} and {name} are both part {number} of {split}")
        parts[number] = (int(match.group(2)), name)
    counts = {count for count, _ in parts.values()}
    if len(counts) != 1 or sorted(parts) != list(range(1, counts.pop() + 1)):
        raise models.ModelError(f"{split} is incomplete: " + ", ".join(sorted(names)))
    return [parts[number][1] for number in sorted(parts)]


def _gguf_target(repo, variant, language_only, scratch):
    names, by_ending = select_gguf(repo.files, variant)
    name = names[0] if len(names) == 1 else f"{len(names)} split files {names[0]} ..."
    if by_ending:
        print(
            f"No GGUF is named for :{variant} alone; using {name}, the only one whose name ends in -{variant}.",
            flush=True,
        )
    # The real one prints "Selected NAME from REPO." here; the fake installer does
    # (install/models.py target_repo).
    return gguf_target(repo, names, scratch, language_only=language_only)


def gguf_target(repo, names, scratch, *, language_only=False):
    """The GGUF target whose files in repo are names, one or a split GGUF's parts
    in split order, which side by side in target/ the native loader finds."""
    # The fake's GGUFs carry no real header: the repository's config.json stands
    # in for the config upstream.py derives from it (gguf.model_config), and an
    # empty file for each part's header in scratch's target/ (1.6.0).
    config, target = scratch / "config.json", scratch / "target"
    config.write_bytes(models.json_bytes(repo.json("config.json")))
    target.mkdir(exist_ok=True)
    for part in names:
        (target / Path(part).name).write_bytes(b"")
    time.sleep(_inspect_seconds(names[0]))
    stem = SPLIT_NAME.sub("", names[0]).removesuffix(".gguf")
    if Path(stem).name.lower().startswith("imatrix"):
        # An importance matrix has no model metadata (real Splash, install/gguf.py).
        raise models.ModelError("missing or invalid GGUF metadata: general.architecture")
    # install/gguf.py require_quantized (1.6.0) weighs the tensors' values; the
    # fake's GGUFs have none, so the name stands in. Only a whole unquantized model
    # is refused (UD-Q8_K_XL, with BF16 parts, loads).
    if stem.upper().endswith(("-BF16", "-F16", "-F32")):
        raise models.ModelError(
            "this GGUF stores its weights unquantized (BF16, F16 or F32): Splash loads quantized "
            "GGUFs, not whole-BF16 or F16 models; choose a quantized variant of the repository"
        )
    family = check_model("gguf", "none", config, gguf_headers=target)
    files = {"target/" + Path(part).name: part for part in names}
    vision_format = "none"
    # A family without a vision tower (Flint) serves text only and reads no projector.
    if not language_only and family.vision:
        projector = select_vision(repo)
        files["vision/mmproj.gguf"] = projector
        vision_format = "gguf"
    return Target("gguf", vision_format, config, target, family, files)


def _inspect_seconds(name):
    """`FAKE_SPLASH_INSPECT_SECONDS`: how long reading one GGUF's header takes, e.g.
    `0.2`, or `0.2,UD-Q4_K_M=1` to make one variant slower than the rest."""
    seconds = 0.0
    stem = SPLIT_NAME.sub("", Path(name).name).removesuffix(".gguf")
    for part in filter(None, os.environ.get("FAKE_SPLASH_INSPECT_SECONDS", "").split(",")):
        variant, _, value = part.rpartition("=")
        if not variant:
            seconds = float(value)
        elif stem.endswith("-" + variant):
            return float(value)
    return seconds


# The affine formats the block kernels hold (metal/abi/QuantFormat.h): bits and group sizes.
AFFINE_BITS = (2, 3, 4, 5, 6, 8)
AFFINE_GROUPS = (32, 64, 128)
# Where an MLX repository keeps its image processor's configuration (upstream.py `PROCESSOR_FILES`).
PROCESSOR_FILES = ("preprocessor_config.json", "processor_config.json")


def _require_quantization(entry, label, module):
    """ModelDescriptor.mm `requireMlxQuantization` for one entry: an affine
    format the kernels hold, mxfp4 4-bit in groups of 32 or nvfp4 4-bit in groups of
    16. A module's entry that omits bits or group size takes its mode's default."""
    mode = entry.get("mode", "affine")
    if not isinstance(mode, str):
        raise models.ModelError(f"{label} mode must be a string")
    affine, nvfp4 = mode == "affine", mode == "nvfp4"

    def number(key, default):
        value = entry.get(key)
        if module and value is None:
            return default
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != int(value) or value < 0:
            raise models.ModelError(f"{label} {key} must be a whole number")
        return int(value)

    bits, group = number("bits", 4), number("group_size", 64 if affine else 16 if nvfp4 else 32)
    if affine:
        ok = bits in AFFINE_BITS and group in AFFINE_GROUPS
    elif nvfp4:
        ok = bits == 4 and group == 16
    else:
        ok = mode == "mxfp4" and bits == 4 and group == 32
    if not ok:
        raise models.ModelError(
            f"{label} is {mode} {bits}-bit in groups of {group}; MLX weights load as affine 2, 3, 4, 5, 6 "
            "or 8 bits in groups of 32, 64 or 128, or as mxfp4 or nvfp4"
        )


def _require_quantization_config(quantization):
    """ModelDescriptor.mm `requireModelOptimizerQuantization` and
    `requireCompressedTensorsQuantization`: a quantization_config of
    quant_method modelopt holding NVFP4 (in groups of 16) or FP8, or of
    compressed-tensors whose groups hold NVFP4 or FP8. The fake checks the formats,
    not which modules each covers."""
    method = quantization.get("quant_method")
    if method == "modelopt":
        layers = quantization.get("quantized_layers")
        if isinstance(layers, dict):
            # Each quantized layer names its own algorithm; the top-level one plays no part.
            for module, entry in layers.items():
                label = f"quantization_config quantized_layers {module}"
                if not isinstance(entry, dict):
                    raise models.ModelError(f"{label} must be an object")
                _require_modelopt_algorithm(entry, label)
            return
        _require_modelopt_algorithm(quantization, "quantization_config")
        return
    groups = quantization.get("config_groups")
    if not isinstance(groups, dict) or not groups:
        raise models.ModelError("quantization_config config_groups must be a non-empty object")
    for name, group in groups.items():
        label = f"quantization_config config_groups {name}"
        weights = group.get("weights") if isinstance(group, dict) else None
        if not isinstance(weights, dict):
            raise models.ModelError(f"{label} weights must be an object")
        form = group.get("format", quantization.get("format", ""))
        kind, bits, strategy = weights.get("type"), weights.get("num_bits"), weights.get("strategy")
        group_size = weights.get("group_size") or 0
        nvfp4 = form == "nvfp4-pack-quantized" and kind == "float" and bits == 4 and strategy == "tensor_group"
        fp8 = form == "float-quantized" and kind == "float" and bits == 8 and strategy in ("channel", "tensor")
        if not ((nvfp4 and group_size == 16) or fp8):
            raise models.ModelError(
                f"{label} is {form}: {kind} {bits}-bit by {strategy}"
                + (f" in groups of {group_size}" if group_size else "")
                + "; compressed-tensors weights load as NVFP4 (nvfp4-pack-quantized: float 4-bit by tensor_group "
                "in groups of 16) or FP8 (float-quantized: float 8-bit by channel or tensor)"
            )
        if weights.get("symmetric") is False or weights.get("actorder") in ("group", True):
            raise models.ModelError(
                f"{label} is asymmetric or ordered by activation groups; Splash loads symmetric "
                "compressed-tensors weights in their stored column order"
            )


def _require_modelopt_algorithm(entry, label):
    """ModelDescriptor.mm `requireModelOptimizerQuantization`'s requireAlgorithm."""
    algorithm = entry.get("quant_algo")
    if algorithm == "FP8":
        return
    if algorithm not in ("NVFP4", "W4A16_NVFP4"):
        raise models.ModelError(f"{label} is {algorithm}; Model Optimizer weights load as NVFP4, W4A16_NVFP4 or FP8")
    group = entry.get("group_size")
    if group is not None and group != 16:
        raise models.ModelError(f"{label} is NVFP4 in groups of {group}; Model Optimizer NVFP4 loads in groups of 16")


def mlx_target(repo, language_only, scratch):
    config = scratch / "config.json"
    config.write_bytes(models.json_bytes(repo.json("config.json")))
    family = check_model("mlx-affine", "none", config)
    # The engine model-check's wording (ModelDescriptor.mm requireQuantization).
    config_json = repo.json("config.json")
    quant, quantization_config = config_json.get("quantization"), config_json.get("quantization_config")
    if isinstance(quant, dict):
        _require_quantization(quant, "quantization", False)
        for module, entry in quant.items():
            if isinstance(entry, dict):
                _require_quantization(entry, f"quantization {module}", True)
    elif isinstance(quantization_config, dict) and quantization_config.get("quant_method") in (
        "modelopt",
        "compressed-tensors",
    ):
        _require_quantization_config(quantization_config)
    else:
        raise models.ModelError(
            "this model requires an MLX checkpoint (affine 2, 3, 4, 5, 6 or 8 bits in groups of 32, 64 or 128, "
            "mxfp4 or nvfp4), an NVFP4 or FP8 checkpoint of Model Optimizer or compressed-tensors, or a "
            "supported GGUF"
        )
    files = {name: name for name in sorted(repo.files)}
    files.setdefault("config.json", "config.json")
    if language_only:
        # upstream.py `mlx_target`: the processor config is read only when vision is on.
        for name in PROCESSOR_FILES:
            files.pop(name, None)
    return Target("mlx-affine", "none" if language_only else "safetensors", config, None, family, files)


# The names before Splash 1.4.0 made them public, which this fake's older tests call.
_mlx_target = mlx_target
