"""Build auditable Stage 2 source/config/build digests without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", "test-results",
    "playwright-report", "coverage", "browser-profile",
}


def files_under(*roots: str, include_dist: bool = False) -> list[Path]:
    values: set[Path] = set()
    for name in roots:
        path = ROOT / name
        if path.is_file():
            values.add(path)
            continue
        if not path.exists():
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            relative_parts = candidate.relative_to(ROOT).parts
            if any(part in SKIP_PARTS for part in relative_parts):
                continue
            if not include_dist and "dist" in relative_parts:
                continue
            values.add(candidate)
    return sorted(values, key=lambda item: item.relative_to(ROOT).as_posix())


def digest(paths: list[Path]) -> dict[str, object]:
    value = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        value.update(len(relative).to_bytes(4, "big"))
        value.update(relative)
        payload = path.read_bytes()
        value.update(len(payload).to_bytes(8, "big"))
        value.update(payload)
    return {"file_count": len(paths), "sha256": value.hexdigest().upper()}


def build(transition_id: str, output: Path) -> dict[str, object]:
    groups = {
        "source": files_under(
            "backend", "collector", "src", "tools", "scripts", "tests",
            "web/src", "web/e2e", "README.md", ".env.example"),
        "config": files_under(
            "config", "schemas", "web/package.json", "web/package-lock.json",
            "web/tsconfig.json", "web/tsconfig.app.json", "web/tsconfig.node.json",
            "web/vite.config.ts", "web/playwright.config.ts"),
        "build": files_under("web/dist", include_dist=True),
        "backend": files_under("backend", "collector", "src", "tools"),
        "frontend": files_under(
            "web/src", "web/e2e", "web/package.json", "web/package-lock.json",
            "web/tsconfig.json", "web/tsconfig.app.json", "web/tsconfig.node.json",
            "web/vite.config.ts", "web/playwright.config.ts"),
    }
    result = {
        "schema_version": "stage2_source_freeze_manifest_v2",
        "transition_id": transition_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_freeze_active": True,
        "freeze_type": "content_digest_and_change_control",
        "profile_material_included": False,
        "digests": {name: digest(paths) for name, paths in groups.items()},
        "change_policy": (
            "Any source, configuration, dependency or production-build change "
            "invalidates this Stage 2 readiness and requires new gates/digests."),
    }
    output.parent.mkdir(parents=True, exist_ok=False)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transition-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = build(arguments.transition_id, arguments.output)
    print(json.dumps(result["digests"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
