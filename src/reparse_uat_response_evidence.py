"""Re-parse stored UAT response evidence without network access or raw-evidence overwrite."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.uat_service import UatStore, reparse_stored_response  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--database", type=Path, default=Path(os.getenv("ROUTING_CONSOLE_DATABASE_PATH") or os.getenv("ROUTING_CONSOLE_DB") or ROOT / "data" / "routing_quality_console.sqlite3"))
    parser.add_argument("--expected-evidence-sha256")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--confirm-amendment", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = reparse_stored_response(
            UatStore(args.database), args.execution_id,
            expected_sha256=args.expected_evidence_sha256,
            confirm=args.confirm_amendment,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
