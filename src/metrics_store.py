"""Read-only loader and validator for versioned channel metric snapshots."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_PATH = ROOT / "data" / "channel_metrics_v1.json"
REQUIRED_METRICS = (
    "success_rate", "avg_latency_ms", "p95_latency_ms", "avg_cost"
)


class MetricsValidationError(ValueError):
    pass


class MetricsStore:
    def __init__(self, snapshot: dict[str, Any], *, source_path: Path | None = None):
        self._snapshot = deepcopy(snapshot)
        self.source_path = Path(source_path) if source_path else None
        self._validate()
        self._by_channel = {
            str(row["channel_id"]): deepcopy(row)
            for row in self._snapshot["channels"]
        }

    @classmethod
    def load(cls, path: Path = DEFAULT_METRICS_PATH) -> "MetricsStore":
        source = Path(path)
        with source.open(encoding="utf-8-sig") as handle:
            snapshot = json.load(handle)
        return cls(snapshot, source_path=source)

    def _validate(self) -> None:
        for field in ("metrics_version", "updated_at", "channels"):
            if field not in self._snapshot:
                raise MetricsValidationError(f"missing snapshot field: {field}")
        if not isinstance(self._snapshot["channels"], list):
            raise MetricsValidationError("channels must be a list")
        identifiers: list[str] = []
        for index, row in enumerate(self._snapshot["channels"]):
            if not isinstance(row, dict):
                raise MetricsValidationError(f"channel row {index} must be an object")
            if not row.get("channel_id"):
                raise MetricsValidationError(f"channel row {index} is missing channel_id")
            if "metrics" not in row or "sample_count" not in row:
                raise MetricsValidationError(
                    f"channel {row['channel_id']} is missing metrics or sample_count"
                )
            metrics = row["metrics"]
            missing = [field for field in REQUIRED_METRICS if field not in metrics]
            if missing:
                raise MetricsValidationError(
                    f"channel {row['channel_id']} missing metrics: {','.join(missing)}"
                )
            try:
                success = float(metrics["success_rate"])
                latency = float(metrics["avg_latency_ms"])
                p95 = float(metrics["p95_latency_ms"])
                cost = float(metrics["avg_cost"])
                sample_count = int(row["sample_count"])
            except (TypeError, ValueError) as exc:
                raise MetricsValidationError(
                    f"channel {row['channel_id']} contains non-numeric metrics"
                ) from exc
            if not 0 <= success <= 1:
                raise MetricsValidationError("success_rate must be in [0,1]")
            if latency < 0 or p95 < latency or cost < 0 or sample_count < 0:
                raise MetricsValidationError(
                    f"channel {row['channel_id']} contains invalid metric ranges"
                )
            identifiers.append(str(row["channel_id"]))
        if len(identifiers) != len(set(identifiers)):
            raise MetricsValidationError("channel_id must be unique")

    @property
    def metrics_version(self) -> str:
        return str(self._snapshot["metrics_version"])

    @property
    def updated_at(self) -> str:
        return str(self._snapshot["updated_at"])

    def get(self, channel_id: str) -> dict[str, Any] | None:
        row = self._by_channel.get(str(channel_id))
        return deepcopy(row) if row is not None else None

    def channels(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for row in self._snapshot["channels"]]

    def snapshot_metadata(self) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in self._snapshot.items() if key != "channels"
        }
