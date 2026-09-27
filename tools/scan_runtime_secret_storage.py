"""Report only aggregate plaintext-secret findings in local runtime storage."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PATTERNS = {
    # Platform keys use one uninterrupted high-entropy alphanumeric payload;
    # excluding a second hyphen avoids false positives from locales such as sk-SK.
    "api_key_plaintext": re.compile(rb"sk-[A-Za-z0-9]{32,}"),
    "bearer_token_plaintext": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~-]{20,}"),
    "authorization_value_plaintext": re.compile(rb"(?i)authorization\s*[:=]\s*[A-Za-z0-9._~+/-]{16,}"),
    "cookie_value_plaintext": re.compile(rb"(?i)cookie\s*[:=]\s*[A-Za-z0-9._~+/%-]{16,}"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    findings = {name: 0 for name in PATTERNS}
    affected_files: list[dict[str, object]] = []
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        file_counts: dict[str, int] = {}
        for name, pattern in PATTERNS.items():
            count = len(pattern.findall(payload))
            findings[name] += count
            if count:
                file_counts[name] = count
        if file_counts:
            affected_files.append({"path": str(path.relative_to(root)), "counts": file_counts})
    result = {"root": str(root), "files_scanned": scanned, **findings,
              "total_findings": sum(findings.values()), "affected_files": affected_files}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(1 if result["total_findings"] else 0)


if __name__ == "__main__":
    main()
