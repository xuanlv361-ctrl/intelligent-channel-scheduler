"""Resolve configured offline candidate channels for a selected model."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING_PATH = ROOT / "data" / "model_channel_mapping_v1.json"


class ModelChannelResolutionError(ValueError):
    pass


def load_mapping(path: Path = DEFAULT_MAPPING_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def resolve_model_channels(
    selected_model: str,
    mapping: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    mapping = mapping or load_mapping()
    if selected_model not in mapping.get("models", {}):
        raise ModelChannelResolutionError(f"unknown model_id: {selected_model}")
    rows = [
        deepcopy(row) for row in mapping["models"][selected_model]
        if row.get("availability_status") == "available"
    ]
    if any(row.get("is_mock") is not True for row in rows):
        raise ModelChannelResolutionError("offline mapping contains non-Mock channel")
    rows.sort(key=lambda row: (int(row["priority"]), str(row["channel_id"])))
    return rows
