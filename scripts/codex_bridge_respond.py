"""Write one explicit response for a Website Codex development-bridge turn.

This helper is intentionally boring: it only wraps a caller-supplied
assistant message and never invents a PASS/answer.  A Codex/operator workflow
can inspect ``pending/<request_id>.json`` (including the image URL/hash and
executed-tool summaries), make a real judgement, then invoke this command with
the JSON assistant message.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="CODEX_BRIDGE_ROOT")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--message-json", required=True, help="assistant message JSON object")
    parser.add_argument("--metadata-json", default="{}")
    args = parser.parse_args()
    try:
        message = json.loads(args.message_json)
        metadata = json.loads(args.metadata_json)
    except json.JSONDecodeError as error:
        raise SystemExit(f"invalid JSON: {error}") from error
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        raise SystemExit("message must be an assistant JSON object")
    if not isinstance(metadata, dict):
        raise SystemExit("metadata must be a JSON object")
    root = Path(args.root).resolve()
    responses = root / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": "website-codex-bridge-response.v1",
        "request_id": args.request_id,
        "message": message,
        "metadata": metadata,
    }
    target = responses / f"{args.request_id}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
