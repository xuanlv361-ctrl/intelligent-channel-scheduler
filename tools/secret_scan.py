"""Conservative current-tree secret scanner used locally and in CI."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {
    ".git", "node_modules", "dist", "__pycache__", ".pytest_cache",
    "test-results", "playwright-report", "coverage", ".npm-cache",
    ".playwright-cli", ".playwright-local",
}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".md", ".txt",
    ".toml", ".ini", ".cfg", ".css", ".html", ".example",
}
PATTERNS = {
    "bearer_value": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.I),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "assigned_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|password|secret)\b\s*[:=]\s*"
        r"[\"'](?!test|fake|mock|example|not-a-real)[^\"']{12,}[\"']"),
}


def scan(root: Path = ROOT) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_PARTS or part.startswith(".pytest")
                                     for part in relative.parts):
            continue
        if path.name == ".env.example":
            pass
        elif path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            ".gitignore", ".gitattributes",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "secret-scan: allow" in line:
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((str(relative), number, name))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    for path, line, kind in findings:
        print(f"{path}:{line}: {kind}")
    print(f"secret_scan_findings={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
