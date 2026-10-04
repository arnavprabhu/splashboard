"""Run under Splash's bundled Python, never imported by the manager."""

import contextlib
import io
import json
import os
import sys
from pathlib import Path


def main() -> None:
    from install import families, hub, models, upstream  # type: ignore[import-not-found]

    spec = json.load(sys.stdin)
    repo = hub.Repository(spec["repo"], spec["sha"], set(spec["files"]))
    variants = []
    names = [
        n
        for n in spec["files"]
        if "/" not in n and n.endswith(".gguf") and "mmproj" not in Path(n).stem.lower()
    ]
    parts = [Path(n).stem.split("-") for n in names]
    shared = len(os.path.commonprefix(parts)) if len(parts) > 1 else 0
    for name, words in zip(names, parts, strict=False):
        variant = "-".join(words[shared:]) if shared else Path(name).stem
        variants.append({"name": variant, "files": [name], "size_bytes": spec["files"][name]})
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
    results = []
    for choice, reported in choices:
        out = {"name": reported, "compatible": False}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
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
                else:
                    target = upstream.inspect_target(repo, choice, True)
                    language_files = sorted(set(target.files.values()))
                    family = families.family_for(target.config)
                    vision = False
                    reason = None
                    try:
                        full = upstream.inspect_target(repo, choice, False)
                        vision = True
                        target = full
                    except Exception as error:
                        reason = str(error)
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
        results.append(out)
    print(json.dumps({"variants": variants, "results": results}))


if __name__ == "__main__":
    main()
