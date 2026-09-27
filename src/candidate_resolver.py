"""Resolve mode-specific candidate catalogs without mixing Mock and measured rows."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from real_channel_shadow_adapter import adapt_real_channel, adapt_shadow_catalog


ROOT = Path(__file__).resolve().parents[1]
MOCK_CATALOG_PATH = ROOT / "data" / "mock_channel_catalog_v3.csv"
REAL_CATALOG_PATH = ROOT / "data" / "channel_catalog_real_v1.csv"


@dataclass(frozen=True, slots=True)
class ResolvedCandidates:
    mode: str
    catalog_path: str
    catalog_version: str
    catalog_sha256: str
    source_type: str
    candidates: list[dict[str, str]]
    raw_candidate_count: int


class CandidateResolutionError(ValueError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _adapt_mock(row: dict[str, str]) -> dict[str, str]:
    required = ("candidate_id", "channel_id", "requested_model", "latency_ms", "input_price_per_1m", "output_price_per_1m", "success_probability_prior", "sample_size", "supports_text", "supports_non_stream", "supports_stream", "availability_status", "currency", "metrics_updated_at", "priority", "is_mock", "data_source")
    if any(not row.get(field) for field in required):
        raise CandidateResolutionError("mock catalog row is missing a required field")
    if row["is_mock"] != "TRUE":
        raise CandidateResolutionError("mock catalog contains a non-Mock candidate")
    result = dict(row)
    result["success_rate"] = row["success_probability_prior"]
    result["observed_success_rate"] = row["success_probability_prior"]
    result["source_type"] = row.get("parameter_type", "simulation_parameter")
    result["routing_eligible"] = "FALSE"
    result["shadow_eligible"] = "FALSE"
    result["routing_block_reason"] = "mock_candidate_not_real_routing"
    return result


class CandidateResolver:
    def __init__(self, *, mock_catalog_path: Path = MOCK_CATALOG_PATH, real_catalog_path: Path = REAL_CATALOG_PATH):
        self.mock_catalog_path = Path(mock_catalog_path)
        self.real_catalog_path = Path(real_catalog_path)

    def resolve(self, mode: str) -> ResolvedCandidates:
        if mode in {"simulation", "mock_execute"}:
            path = self.mock_catalog_path
            raw = read_csv(path)
            candidates = [_adapt_mock(row) for row in raw]
            version, source_type = "mock-channel-v3", "simulation_parameter"
        elif mode in {"real_shadow", "real_execute"}:
            path = self.real_catalog_path
            raw = read_csv(path)
            if mode == "real_shadow":
                candidates = adapt_shadow_catalog(raw)
            else:
                candidates = [adapt_real_channel(row) for row in raw if row.get("routing_eligible") == "TRUE"]
            version = raw[0]["catalog_version"] if raw else "unknown"
            source_type = "measured"
        else:
            raise CandidateResolutionError(f"unsupported mode: {mode}")
        if candidates:
            mock_flags = {row["is_mock"] for row in candidates}
            if len(mock_flags) != 1:
                raise CandidateResolutionError("Mock and real candidates must not be mixed")
            if mode.startswith("real_") and mock_flags != {"FALSE"}:
                raise CandidateResolutionError("real mode resolved Mock candidates")
            if mode in {"simulation", "mock_execute"} and mock_flags != {"TRUE"}:
                raise CandidateResolutionError("Mock mode resolved real candidates")
        return ResolvedCandidates(
            mode=mode, catalog_path=str(path.relative_to(ROOT)).replace("\\", "/"),
            catalog_version=version, catalog_sha256=hashlib.sha256(path.read_bytes()).hexdigest().upper(),
            source_type=source_type, candidates=candidates, raw_candidate_count=len(raw),
        )
