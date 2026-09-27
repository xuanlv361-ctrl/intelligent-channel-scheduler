"""Reviewed domestic-UAT billing-log read contract.

This module is deliberately independent from browser state.  It only builds
the exact, read-only request observed in the current UAT console bundle and
validates that a captured response belongs to the authorized page/range.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


CONTRACT_VERSION = "domestic_uat_log_read_contract_v1"
ENDPOINT = "https://uat.weimeta.cn/api/log/self"
ALLOWED_HOST = "uat.weimeta.cn"
ALLOWED_PATH = "/api/log/self"
MAX_PAGE_SIZE = 100
MAX_PAGE_NUMBER = 10_000


class UatLogReadContractError(ValueError):
    """Fail-closed query-contract error with a stable reason code."""


def _utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise UatLogReadContractError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UatLogReadContractError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DomesticUatLogQuery:
    date_from_utc: str
    date_to_utc: str
    page: int = 1
    page_size: int = 100
    event_type: int = 0
    token_name: str = ""
    model_name: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        start = _utc(self.date_from_utc, "date_from")
        end = _utc(self.date_to_utc, "date_to")
        if start >= end:
            raise UatLogReadContractError("range_order_invalid")
        if not 1 <= int(self.page) <= MAX_PAGE_NUMBER:
            raise UatLogReadContractError("page_invalid")
        if not 1 <= int(self.page_size) <= MAX_PAGE_SIZE:
            raise UatLogReadContractError("page_size_invalid")
        if int(self.event_type) not in {0, 1, 2, 3, 4, 5, 6}:
            raise UatLogReadContractError("event_type_invalid")
        for name, value in (
            ("token_name", self.token_name),
            ("model_name", self.model_name),
            ("request_id", self.request_id),
        ):
            if not isinstance(value, str) or len(value) > 128:
                raise UatLogReadContractError(f"{name}_invalid")
            if any(ord(char) < 32 for char in value):
                raise UatLogReadContractError(f"{name}_invalid")

    @property
    def start(self) -> datetime:
        return _utc(self.date_from_utc, "date_from")

    @property
    def end(self) -> datetime:
        return _utc(self.date_to_utc, "date_to")

    def parameters(self) -> dict[str, str | int]:
        # The current UAT console uses inclusive Unix-second boundaries.
        return {
            "p": int(self.page),
            "page_size": int(self.page_size),
            "type": int(self.event_type),
            "token_name": self.token_name,
            "model_name": self.model_name,
            "request_id": self.request_id,
            "start_timestamp": int(self.start.timestamp()),
            "end_timestamp": int(self.end.timestamp()),
        }

    def url(self) -> str:
        return ENDPOINT + "?" + urlencode(self.parameters())


def validate_authorized_url(url: str, expected: DomesticUatLogQuery) -> None:
    """Validate exact host/path/query and reject range or page substitution."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UatLogReadContractError("source_not_allowed") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or (parsed.hostname or "").casefold() != ALLOWED_HOST
        or parsed.path != ALLOWED_PATH
        or parsed.fragment
    ):
        raise UatLogReadContractError("source_not_allowed")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    expected_query = {
        key: [str(value)] for key, value in expected.parameters().items()
    }
    if query != expected_query:
        raise UatLogReadContractError("query_contract_mismatch")


def validate_observed_url(
    url: str, authorized: DomesticUatLogQuery,
) -> DomesticUatLogQuery:
    """Validate a console-issued query without expanding the authorization.

    The reviewed page may use a smaller UI page size than the local upper
    bound. Every other query field, including both range boundaries, remains
    exact. The returned query is the authoritative pagination contract for
    validating the response envelope.
    """
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UatLogReadContractError("source_not_allowed") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or (parsed.hostname or "").casefold() != ALLOWED_HOST
        or parsed.path != ALLOWED_PATH
        or parsed.fragment
    ):
        raise UatLogReadContractError("source_not_allowed")
    values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    required_keys = {
        "p", "page_size", "type", "start_timestamp", "end_timestamp",
    }
    optional_keys = {"token_name", "model_name", "request_id"}
    if (
        not required_keys.issubset(values)
        or not set(values).issubset(required_keys | optional_keys)
        or any(len(item) != 1 for item in values.values())
    ):
        raise UatLogReadContractError("query_contract_mismatch")
    try:
        observed = DomesticUatLogQuery(
            date_from_utc=datetime.fromtimestamp(
                int(values["start_timestamp"][0]), timezone.utc).isoformat(),
            date_to_utc=datetime.fromtimestamp(
                int(values["end_timestamp"][0]), timezone.utc).isoformat(),
            page=int(values["p"][0]),
            page_size=int(values["page_size"][0]),
            event_type=int(values["type"][0]),
            token_name=values.get("token_name", [""])[0],
            model_name=values.get("model_name", [""])[0],
            request_id=values.get("request_id", [""])[0],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise UatLogReadContractError("query_contract_mismatch") from exc
    observed_parameters = observed.parameters()
    authorized_parameters = authorized.parameters()
    if (
        observed_parameters["start_timestamp"] != authorized_parameters["start_timestamp"]
        or observed_parameters["end_timestamp"] != authorized_parameters["end_timestamp"]
        or observed.page != authorized.page
        or observed.page_size > authorized.page_size
        or observed.event_type != authorized.event_type
        or observed.token_name != authorized.token_name
        or observed.model_name != authorized.model_name
        or observed.request_id != authorized.request_id
    ):
        raise UatLogReadContractError("query_contract_mismatch")
    return observed


def safe_contract_summary() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "method": "GET",
        "safe_endpoint": ENDPOINT,
        "query_fields": [
            "p",
            "page_size",
            "type",
            "token_name",
            "model_name",
            "request_id",
            "start_timestamp",
            "end_timestamp",
        ],
        "timezone_semantics": "authorized aware timestamps normalized to UTC Unix seconds",
        "end_boundary": "inclusive_as_implemented_by_reviewed_uat_console",
        "maximum_page_size": MAX_PAGE_SIZE,
        "write_capable": False,
        "completion_capable": False,
    }


def validate_response_pagination(
    payload: Any,
    expected: DomesticUatLogQuery,
    *,
    previous_envelope_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the UAT envelope before any record is projected."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise UatLogReadContractError("response_envelope_invalid")
    data = payload["data"]
    items = data.get("items")
    page = data.get("page")
    page_size = data.get("page_size")
    total = data.get("total")
    if (
        not isinstance(items, list)
        or not isinstance(page, int)
        or isinstance(page, bool)
        or not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
    ):
        raise UatLogReadContractError("response_pagination_invalid")
    if page != expected.page or page_size != expected.page_size:
        raise UatLogReadContractError("response_pagination_mismatch")
    import hashlib
    import json

    envelope_sha256 = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        expected.page > 1
        and previous_envelope_sha256
        and envelope_sha256 == previous_envelope_sha256
    ):
        raise UatLogReadContractError("pagination_loop_detected")
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "candidate_record_count": len(items),
        "has_next_page": page * page_size < total,
        "envelope_sha256": envelope_sha256,
    }


def safe_source_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
