"""Run under Splash's bundled Python, never imported by the manager.

Reads loose GGUF headers and says which model family each is, using Splash's own
reader and matcher (`install/gguf.py`, `install/families.py`), so a file is accepted
here exactly when `splash serve` would accept it. stdin: {"paths": [...]}; stdout:
{path: {"family", "draft_repo", "architecture", "name"} | {"error"}}.
"""

import json
import sys


def classify(path: str) -> dict[str, object]:
    from install import families, gguf, models  # type: ignore[import-not-found]

    try:
        metadata = gguf.Metadata(path, tensors=True)
        config = gguf.model_config(metadata)
        family = families.family_for(config)
        gguf.require_loadable(metadata)  # the same check `splash serve` makes (upstream.py)
    except models.ModelError as error:
        return {"error": str(error)}
    return {
        "family": family.name,
        "draft_repo": family.draft.repo,
        "architecture": metadata.values.get("general.architecture"),
        "name": metadata.values.get("general.name"),
    }


def main() -> None:
    spec = json.load(sys.stdin)
    out = {}
    for path in spec["paths"]:
        try:
            out[path] = classify(path)
        except Exception as error:
            out[path] = {"error": f"{type(error).__name__}: {error}"}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
