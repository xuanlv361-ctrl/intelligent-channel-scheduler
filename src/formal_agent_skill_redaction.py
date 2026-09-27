"""Recursive redaction shared by formal Skill results and audit records."""

from __future__ import annotations

import re
from typing import Any


SENSITIVE_KEY = re.compile(
    r"(?:authorization(?:[_-]?header)?|api[_-]?key|token|(?:access|refresh|bearer|auth|session)[_-]?token|"
    r"(?:client[_-]?)?secret|password|cookie(?:[_-]?header)?|session[_-]?value|credential|"
    r"ciphertext|storage[_-]?value|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*\S+|cookie\s*[:=]\s*\S+)")


def redact(value: Any) -> tuple[Any, int]:
    """Return a JSON-compatible recursively redacted copy and count."""
    count = 0
    seen: set[int] = set()

    def clean(item: Any, key: str | None = None, depth: int = 0) -> Any:
        nonlocal count
        if depth > 12:
            count += 1
            return "[REDACTED_DEPTH_LIMIT]"
        # Token usage and latency fields (input_tokens, output_tokens,
        # first_token_latency_ms) are observability data, not credentials.
        # Only complete credential-field names are redacted.
        if key is not None and SENSITIVE_KEY.fullmatch(key):
            count += 1
            return "[REDACTED]"
        if isinstance(item, dict):
            if id(item) in seen:
                count += 1
                return "[REDACTED_CYCLE]"
            seen.add(id(item))
            result = {}
            for index, (raw_key, child) in enumerate(item.items()):
                if index >= 500:
                    count += 1
                    result["[TRUNCATED]"] = "collection limit reached"
                    break
                child_key = str(raw_key)[:256]
                if SENSITIVE_VALUE.search(child_key):
                    count += 1
                    child_key = f"[REDACTED_KEY_{index}]"
                result[child_key] = clean(child, child_key, depth + 1)
            seen.remove(id(item))
            return result
        if isinstance(item, (list, tuple, set)):
            if id(item) in seen:
                count += 1
                return "[REDACTED_CYCLE]"
            seen.add(id(item))
            result = [clean(v, depth=depth + 1) for v in list(item)[:500]]
            if len(item) > 500:
                count += 1
                result.append("[TRUNCATED_COLLECTION]")
            seen.remove(id(item))
            return result
        if isinstance(item, bytes):
            count += 1
            return "[REDACTED_BINARY]"
        if isinstance(item, str):
            replaced, matches = SENSITIVE_VALUE.subn("[REDACTED]", item)
            count += matches
            return replaced[:8000]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return clean(str(item))

    return clean(value), count
