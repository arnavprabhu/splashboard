"""Run under Splash's bundled Python, never imported by the manager.

Reads loose GGUF headers and says which model family each is, with Splash's own
steps for a GGUF target (`install/upstream.py` `gguf_target`): its reader, the
quantization check and the engine's `model-check` on the headers, so a file is
accepted here exactly when `splash serve` would accept it. A split GGUF is read as
one model from all of its parts, in split order (`upstream.split_order`). stdin:
{"paths": [PATH | [PART, ...], ...]}; stdout, by the path or the first part:
{path: {"family", "draft_repo", "architecture", "name"} | {"error"}}.
"""

import json
import sys
import tempfile
from pathlib import Path


def classify(paths: list[str]) -> dict[str, object]:
    from install import gguf, hub, models, upstream  # type: ignore[import-not-found]

    files = {Path(path).name: Path(path) for path in paths}
    names = list(files)
    try:
        if len(names) > 1:
            names = upstream.split_order(names, f"the split GGUF {upstream.model_of(names[0])}")
        repo = hub.Repository.local_files(str(Path(paths[0]).parent), files)
        with tempfile.TemporaryDirectory(prefix="splash-gui-classify-") as scratch:
            # No projector: the family is the text model's, whatever lies beside it.
            target = upstream.gguf_target(repo, names, Path(scratch))
        # The first part's metadata describes the model.
        metadata = gguf.Metadata(files[names[0]])
    except models.ModelError as error:
        return {"error": str(error)}
    return {
        "family": target.family.name,
        "draft_repo": target.family.draft_repo,
        "architecture": metadata.values.get("general.architecture"),
        "name": metadata.values.get("general.name"),
    }


def main() -> None:
    spec = json.load(sys.stdin)
    out = {}
    for entry in spec["paths"]:
        paths = [entry] if isinstance(entry, str) else list(entry)
        try:
            out[paths[0]] = classify(paths)
        except Exception as error:
            out[paths[0]] = {"error": f"{type(error).__name__}: {error}"}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
