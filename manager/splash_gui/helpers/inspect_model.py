"""Run under Splash's bundled Python, never imported by the manager."""

import contextlib
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def variant_label(repo_id: str, name: str, names: list[str], upstream: Any) -> str:
    """The shortest :VARIANT that Splash's own `select_gguf` resolves to `name`.

    Splash strips the model name all root GGUFs share (`X-Q4_K_M` → `Q4_K_M`),
    but one odd file (an `imatrix_*.gguf`) leaves nothing shared, so the label
    is also tried without the repository's model name and as the full stem; the
    first that Splash resolves uniquely to this file is the ID's VARIANT."""
    if len(names) == 1:
        return Path(name).stem
    stem = Path(name).stem
    parts = [Path(n).stem.split("-") for n in names]
    shared = len(os.path.commonprefix(parts))
    model = re.sub(r"-gguf$", "", repo_id.split("/", 1)[-1], flags=re.IGNORECASE)
    labels = []
    if shared:
        labels.append("-".join(stem.split("-")[shared:]))
    if stem.lower().startswith(model.lower() + "-"):
        labels.append(stem[len(model) + 1 :])
    labels.append(stem)
    for label in labels:
        try:
            chosen, _ = upstream.select_gguf(names, label)
        except Exception:  # noqa: S112 - not unique or not found: try the next label
            continue
        if chosen == name:
            return label
    return stem


def main() -> None:
    from install import families, hub, models, upstream  # type: ignore[import-not-found]

    spec = json.load(sys.stdin)
    repo = hub.Repository(spec["repo"], spec["sha"], set(spec["files"]))
    names = [
        n
        for n in spec["files"]
        if "/" not in n and n.endswith(".gguf") and "mmproj" not in Path(n).stem.lower()
    ]
    variants = [
        {
            "name": variant_label(spec["repo"], name, names, upstream),
            "files": [name],
            "size_bytes": spec["files"][name],
        }
        for name in names
    ]
    requested = spec.get("variant")
    # Each choice is (variant to ask the engine for, name to report it under).
    # They differ for a repository of one GGUF, which names no variant apart
    # from its model: the engine is asked for the only file it has, but the
    # result is still reported under the name the variant table shows.
    if requested:
        choices = [(requested, requested)]
    elif len(variants) == 1:
        choices = [(None, variants[0]["name"])]
    elif variants:
        choices = [(v["name"], v["name"]) for v in variants]
    else:
        choices = [(None, None)]

    def screen(choice: str | None, reported: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {"name": reported, "compatible": False}
        try:
            if "manifest.json" in repo.files and spec["repo"].startswith("incoai/"):
                manifest = models.read_json(repo.file("manifest.json"))
                if manifest.get("schema_version", manifest.get("version")) not in (3, 4):
                    raise ValueError("Unsupported legacy manifest schema")
                family = families.named(spec["repo"].split("/")[-1].removesuffix("-Splash"))
                out.update(
                    compatible=True,
                    family=family.name,
                    format="legacy",
                    vision=True,
                    draft=None,
                    files=list(repo.files),
                )
                return out
            # The full check (target plus vision) first: when it passes, the
            # language-only selection is the same files minus the projector, so
            # the target header is read once instead of twice.
            reason = None
            try:
                target = upstream.inspect_target(repo, choice, False)
                vision = True
                language_files = sorted(
                    path for key, path in target.files.items() if key.startswith("target/")
                ) or sorted(set(target.files.values()))
                if target.format != "gguf":
                    language_files = sorted(
                        set(upstream.inspect_target(repo, choice, True).files.values())
                    )
            except Exception as error:
                reason = str(error)
                target = upstream.inspect_target(repo, choice, True)
                vision = False
                language_files = sorted(set(target.files.values()))
            family = families.family_for(target.config)
            out.update(
                compatible=True,
                family=family.name,
                format="gguf" if target.format == "gguf" else "mlx",
                vision=vision,
                vision_reason=reason,
                draft=family.draft.repo,
                files=sorted(set(target.files.values())),
                language_files=language_files,
            )
        except Exception as error:
            out["reason"] = str(error)
        return out

    # Variants are screened in parallel: each reads a GGUF header (several MB of
    # metadata) over HTTP range requests, and a repository may have twenty.
    # Splash's own prints are silenced once around the pool.
    workers = min(12, max(1, len(choices)))
    with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda pair: screen(*pair), choices))
    print(json.dumps({"variants": variants, "results": results}))


if __name__ == "__main__":
    main()
