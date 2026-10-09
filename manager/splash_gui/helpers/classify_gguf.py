"""Run under Splash's bundled Python, never imported by the manager.

Reads loose GGUF headers and says which model family each is, using Splash 1.3.0's
own reader and the engine's `model-check` (`install/gguf.py`, `upstream.check_model`),
the same steps `upstream._gguf_target` takes, so a file is accepted here exactly when
`splash serve` would accept it. stdin: {"paths": [...]}; stdout:
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
    as `upstream._gguf_target` does (splash/install/upstream.py:179-204)."""
    from install import gguf, models, upstream  # type: ignore[import-not-found]

    with tempfile.TemporaryDirectory(prefix="splash-gui-classify-") as scratch:
        config = Path(scratch) / "config.json"
        scalars = Path(scratch) / "gguf-metadata.json"
        config.write_bytes(models.json_bytes(gguf.model_config(metadata)))
        scalars.write_bytes(models.json_bytes(gguf.scalar_metadata(metadata)))
        return upstream.check_model("gguf", "none", config, gguf_metadata=scalars)


def classify(path: str) -> dict[str, object]:
    from install import gguf, models  # type: ignore[import-not-found,unused-ignore]

    try:
        metadata = gguf.Metadata(path, tensors=True)
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
    for path in spec["paths"]:
        try:
            out[path] = classify(path)
        except Exception as error:
            out[path] = {"error": f"{type(error).__name__}: {error}"}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
