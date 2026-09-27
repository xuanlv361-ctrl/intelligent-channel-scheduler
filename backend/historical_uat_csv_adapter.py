"""Strict, offline adapter for the reviewed WEIMETA UAT billing CSV export.

The adapter deliberately discards credential and IP columns at the read
boundary.  Callers receive only allow-listed operational evidence and stable
reason codes; raw sensitive values and the free-form detail column are never
returned.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "domestic_uat_billing_csv_registry_v1.json"
)


def _load_reviewed_schema() -> dict[str, Any]:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if raw.get("registry_version") != "domestic_uat_billing_csv_registry_v1":
        raise RuntimeError("historical_csv_registry_version_invalid")
    schemas = raw.get("schemas")
    if not isinstance(schemas, list) or len(schemas) != 1:
        raise RuntimeError("historical_csv_registry_ambiguous")
    schema = schemas[0]
    if schema.get("schema_id") != "domestic_uat_billing_csv_v1":
        raise RuntimeError("historical_csv_schema_version_invalid")
    return schema


REVIEWED_SCHEMA = _load_reviewed_schema()
CSV_HEADERS = tuple(REVIEWED_SCHEMA["headers"])
CHINA_ZONE = ZoneInfo("Asia/Shanghai")
EVENT_TYPE_MAPPING = dict(REVIEWED_SCHEMA["event_type_mapping"])
ALLOWED_TYPES = set(EVENT_TYPE_MAPPING)

# Reviewed aliases are intentionally explicit.  Unknown model strings are
# rejected instead of being normalized heuristically.
REVIEWED_MODEL_ALIASES = dict(REVIEWED_SCHEMA["reviewed_model_aliases"])

_LATENCY = re.compile(
    r"^(?P<latency>\d+(?:\.\d+)?)s"
    r"(?:\s*/\s*(?P<ttft>\d+(?:\.\d+)?)s)?"
    r"\s*\((?P<mode>非流|流)\)$"
)
_INPUT = re.compile(
    r"^(?P<uncached>\d+)(?:\s*\(缓存读:\s*(?P<cached>\d+)\))?$"
)
_INTEGER = re.compile(r"^\d+$")
_COST = re.compile(r"^¥(?P<amount>\d+(?:\.\d+)?)$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class HistoricalCsvError(ValueError):
    """Fail-closed adapter error with a stable, non-sensitive reason code."""


@dataclass(frozen=True)
class CsvEvidenceRow:
    row_number: int
    timestamp_text: str
    timestamp_china: str
    timestamp_utc: str
    event_type: str
    raw_model: str
    normalized_model: str
    stream: bool
    latency_ms: float
    ttft_ms: float | None
    input_uncached_tokens: int
    input_cached_tokens: int
    input_tokens: int
    output_tokens: int
    cost_cny: str
    detail_present: bool

    def safe_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "timestamp_text": self.timestamp_text,
            "timestamp_china": self.timestamp_china,
            "timestamp_utc": self.timestamp_utc,
            "event_type": self.event_type,
            "raw_model": self.raw_model,
            "normalized_model": self.normalized_model,
            "stream": self.stream,
            "latency_ms": self.latency_ms,
            "ttft_ms": self.ttft_ms,
            "input_uncached_tokens": self.input_uncached_tokens,
            "input_cached_tokens": self.input_cached_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": self.cost_cny,
            "detail_present": self.detail_present,
        }


@dataclass(frozen=True)
class CsvRejection:
    row_number: int
    reason_code: str
    safe_timestamp_text: str | None
    safe_model_text: str | None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "reason_code": self.reason_code,
            "safe_timestamp_text": self.safe_timestamp_text,
            "safe_model_text": self.safe_model_text,
        }


def file_fingerprint(path: Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    return {
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest().upper(),
    }


def _safe_timestamp(value: str) -> str | None:
    text = value.strip()
    return text if _TIME.fullmatch(text) else None


def _safe_model(value: str) -> str | None:
    text = value.strip()
    return text if text in REVIEWED_MODEL_ALIASES else None


class HistoricalUatCsvAdapter:
    """Parse the exact reviewed export schema without retaining secrets."""

    schema_version = "domestic_uat_billing_csv_v1"

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(
        self,
    ) -> tuple[list[CsvEvidenceRow], list[CsvRejection], dict[str, Any]]:
        accepted: list[CsvEvidenceRow] = []
        rejected: list[CsvRejection] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                headers = tuple(next(reader))
            except StopIteration as exc:
                raise HistoricalCsvError("csv_empty") from exc
            if headers != CSV_HEADERS:
                raise HistoricalCsvError("csv_header_mismatch")
            for row_number, values in enumerate(reader, start=2):
                if len(values) != len(CSV_HEADERS):
                    rejected.append(
                        CsvRejection(row_number, "csv_column_count_mismatch", None, None)
                    )
                    continue
                try:
                    accepted.append(self._parse(row_number, values))
                except HistoricalCsvError as exc:
                    rejected.append(
                        CsvRejection(
                            row_number=row_number,
                            reason_code=str(exc),
                            safe_timestamp_text=_safe_timestamp(values[0]),
                            safe_model_text=_safe_model(values[3]),
                        )
                    )
        fingerprint = file_fingerprint(self.path)
        return accepted, rejected, {
            **fingerprint,
            "header_count": len(headers),
            "data_row_count": len(accepted) + len(rejected),
            "accepted_row_count": len(accepted),
            "rejected_row_count": len(rejected),
            "schema_version": self.schema_version,
            "sensitive_columns_discarded": ["API Key", "IP"],
            "free_form_detail_persisted": False,
        }

    @staticmethod
    def _parse(row_number: int, values: list[str]) -> CsvEvidenceRow:
        (
            timestamp_raw,
            _api_key_discarded,
            event_type_raw,
            model_raw,
            latency_raw,
            input_raw,
            output_raw,
            cost_raw,
            _ip_discarded,
            detail_raw,
        ) = values
        timestamp_text = timestamp_raw.strip()
        if not _TIME.fullmatch(timestamp_text):
            raise HistoricalCsvError("invalid_timestamp_format")
        try:
            local_time = datetime.strptime(
                timestamp_text, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CHINA_ZONE)
        except ValueError as exc:
            raise HistoricalCsvError("invalid_timestamp_value") from exc
        event_type = event_type_raw.strip()
        if event_type not in ALLOWED_TYPES:
            raise HistoricalCsvError("unknown_event_type")
        raw_model = model_raw.strip()
        normalized_model = REVIEWED_MODEL_ALIASES.get(raw_model)
        if not normalized_model:
            raise HistoricalCsvError("unknown_model_alias")
        latency_match = _LATENCY.fullmatch(latency_raw.strip())
        if not latency_match:
            raise HistoricalCsvError("invalid_latency_format")
        stream = latency_match.group("mode") == "流"
        ttft_text = latency_match.group("ttft")
        if stream != (ttft_text is not None):
            raise HistoricalCsvError("stream_ttft_inconsistent")
        latency_ms = float(Decimal(latency_match.group("latency")) * 1000)
        ttft_ms = float(Decimal(ttft_text) * 1000) if ttft_text else None
        input_match = _INPUT.fullmatch(input_raw.strip())
        if not input_match:
            raise HistoricalCsvError("invalid_input_tokens_format")
        uncached = int(input_match.group("uncached"))
        cached = int(input_match.group("cached") or 0)
        output_text = output_raw.strip()
        if not _INTEGER.fullmatch(output_text):
            raise HistoricalCsvError("invalid_output_tokens_format")
        output_tokens = int(output_text)
        cost_match = _COST.fullmatch(cost_raw.strip())
        if not cost_match:
            raise HistoricalCsvError("invalid_cost_format")
        try:
            cost = Decimal(cost_match.group("amount"))
        except InvalidOperation as exc:
            raise HistoricalCsvError("invalid_cost_value") from exc
        if not cost.is_finite() or cost < 0:
            raise HistoricalCsvError("invalid_cost_value")
        return CsvEvidenceRow(
            row_number=row_number,
            timestamp_text=timestamp_text,
            timestamp_china=local_time.isoformat(),
            timestamp_utc=local_time.astimezone(timezone.utc).isoformat(),
            event_type=EVENT_TYPE_MAPPING[event_type],
            raw_model=raw_model,
            normalized_model=normalized_model,
            stream=stream,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            input_uncached_tokens=uncached,
            input_cached_tokens=cached,
            input_tokens=uncached + cached,
            output_tokens=output_tokens,
            cost_cny=format(cost, "f"),
            detail_present=bool(detail_raw.strip()),
        )


def safe_schema_mapping() -> dict[str, Any]:
    return {
        "schema_version": HistoricalUatCsvAdapter.schema_version,
        "registry_version": "domestic_uat_billing_csv_registry_v1",
        "registry_sha256": hashlib.sha256(
            REGISTRY_PATH.read_bytes()).hexdigest().upper(),
        "headers": {
            "时间": "timestamp parsed as Asia/Shanghai, normalized to UTC",
            "API Key": "sensitive; discarded at read boundary",
            "类型": "explicit event type",
            "模型": "normalized only through reviewed_model_aliases",
            "用时/首字": "total latency, optional TTFT, and stream mode",
            "输入": "uncached plus explicitly labelled cached-read tokens",
            "输出": "output token integer",
            "花费": "CNY decimal",
            "IP": "sensitive; discarded at read boundary",
            "详情": "presence flag only; raw free-form value discarded",
        },
        "reviewed_model_aliases": dict(sorted(REVIEWED_MODEL_ALIASES.items())),
        "event_type_mapping": dict(sorted(EVENT_TYPE_MAPPING.items())),
        "unknown_format_action": "reject_with_reason_code",
        "identity_digest_persisted": False,
    }


def within_window(
    rows: Iterable[CsvEvidenceRow], start: datetime, end: datetime
) -> list[CsvEvidenceRow]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise HistoricalCsvError("invalid_authorized_window")
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    return [
        row
        for row in rows
        if start_utc
        <= datetime.fromisoformat(row.timestamp_utc).astimezone(timezone.utc)
        <= end_utc
    ]
