"""Run the idempotent database migration before exposing the local console."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backend.enterprise_tenant_migrations import EnterpriseTenantMigrator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--status-file", required=True, type=Path)
    args = parser.parse_args()
    database, status_file = args.database.resolve(), args.status_file.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        migration = EnterpriseTenantMigrator(database).migrate()
        payload = {"status": "ready", "completed_at": datetime.now(timezone.utc).isoformat(),
                   "database": str(database), "migration": migration,
                   "credentials_exposed": False}
        exit_code = 0
    except Exception as exc:
        payload = {"status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(),
                   "database": str(database), "error_code": type(exc).__name__,
                   "credentials_exposed": False}
        exit_code = 1
    status_file.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=status_file.name, suffix=".tmp", dir=status_file.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, status_file)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
