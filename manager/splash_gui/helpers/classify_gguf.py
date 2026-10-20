"""Run under Splash's bundled Python, never imported by the manager.

Reads loose GGUF headers and says which model family each is, using Splash's own
reader and the engine's `model-check` (`install/gguf.py`, `upstream.check_model`),
the same steps `upstream.gguf_target` takes, so a file is accepted here exactly when
`splash serve` would accept it. A split GGUF is read as one model from all of its
parts, in split order, as Splash merges them (`gguf.merged_split`). stdin:
{"paths": [PATH | [PART, ...], ...]}; stdout, by the path or the first part:
{path: {"family", "draft_repo", "architecture", "name"} | {"error"}}.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def family_of(metadata: Any) -> Any:
    """The family the engine finds a GGUF to describe: the derived config and the
    scalar metadata are written to files and handed to the engine's `model-check`,
    as `upstream.gguf_target` does."""
    from install import gguf, models, upstream  # type: ignore[import-not-found]

    with tempfile.TemporaryDirectory(prefix="splash-gui-classify-") as scratch:
        config = Path(scratch) / "config.json"
        scalars = Path(scratch) / "gguf-metadata.json"
        config.write_bytes(models.json_bytes(gguf.model_config(metadata)))
        scalars.write_bytes(models.json_bytes(gguf.scalar_metadata(metadata)))
        return upstream.check_model("gguf", "none", config, gguf_metadata=scalars)


def classify(paths: list[str]) -> dict[str, object]:
    from install import gguf, models  # type: ignore[import-not-found,unused-ignore]

    try:
        headers = [gguf.Metadata(path, tensors=True) for path in paths]
        metadata = gguf.merged_split(headers) if len(headers) > 1 else headers[0]
        family = family_of(metadata)
        gguf.require_loadable(metadata)  # the same check `splash serve` makes (upstream.py)
    except models.ModelError as error:
        return {"error": str(error)}
    return {
        "family": family.name,
        "draft_repo": family.draft_repo,
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
