"""Run under Splash's bundled Python (it ships `tokenizers`), never imported by the
manager: read {"tokenizer": path, "ids": [int]} on stdin, print
{"pieces": [{"id", "piece", "text"}]} — each id's vocabulary entry and what it
decodes to alone (SPEC §10.6 Tokenizer, §22 Q6)."""

import json
import sys


def main() -> None:
    from tokenizers import Tokenizer  # type: ignore[import-not-found]

    spec = json.load(sys.stdin)
    tokenizer = Tokenizer.from_file(spec["tokenizer"])
    pieces = []
    for token_id in spec["ids"]:
        pieces.append(
            {
                "id": token_id,
                "piece": tokenizer.id_to_token(token_id),
                "text": tokenizer.decode([token_id], skip_special_tokens=False),
            }
        )
    print(json.dumps({"pieces": pieces}))


if __name__ == "__main__":
    main()
