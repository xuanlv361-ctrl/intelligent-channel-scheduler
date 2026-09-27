from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


FINGERPRINT_VERSION = "uat_billing_shape_v1"
SENSITIVE_FIELD_NAMES = {
    "authorization", "cookie", "set_cookie", "password", "api_key", "apikey",
    "access_token", "refresh_token", "prompt", "messages", "request_body",
    "response_body", "localstorage", "sessionstorage", "storage_state",
    "client_secret", "client_token", "bearer_token", "id_token",
}
SENSITIVE_FIELD_PARTS = {
    "authorization", "cookie", "password", "passwd", "secret", "token",
    "credential", "apikey", "api_key", "storage",
}
SAFE_USAGE_FIELD_NAMES = {
    "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
    "cached_tokens", "cache_tokens", "total_tokens", "reasoning_tokens",
    "audio_input_tokens", "audio_output_tokens", "image_output_tokens",
    # Provider billing logs use this as the redacted API-key alias shown in the UI.
    # It is descriptive metadata, not the credential itself.
    "token_name",
    # Numeric Provider token record id; it is not credential material and is
    # never projected into normalized evidence or application logs.
    "token_id",
}


class SchemaObservationError(ValueError):
    pass


def _field_token(value: str) -> str:
    return value.casefold().replace("-", "_").replace(" ", "_")


def _is_sensitive_field(value: str) -> bool:
    token = _field_token(value)
    if token in SAFE_USAGE_FIELD_NAMES:
        return False
    if token in SENSITIVE_FIELD_NAMES:
        return True
    parts = {part for part in token.split("_") if part}
    return bool(parts & SENSITIVE_FIELD_PARTS) or any(
        token.endswith(suffix) for suffix in (
            "secret", "token", "password", "credential", "cookie"))


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise SchemaObservationError("schema_value_type_unsupported")


def _shape(
    value: Any, *, depth: int, counters: dict[str, int],
    sensitive_fields: list[str],
) -> dict[str, Any]:
    if depth > 8:
        raise SchemaObservationError("schema_depth_limit_exceeded")
    if isinstance(value, dict):
        if len(value) > 128:
            raise SchemaObservationError("schema_field_limit_exceeded")
        fields: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if len(key) > 128:
                raise SchemaObservationError("schema_key_length_limit_exceeded")
            counters["fields"] += 1
            if counters["fields"] > 512:
                raise SchemaObservationError("schema_total_field_limit_exceeded")
            if _is_sensitive_field(key):
                sensitive_fields.append(key)
                continue
            fields[key] = _shape(
                item, depth=depth + 1, counters=counters,
                sensitive_fields=sensitive_fields)
        return {"type": "object", "fields": fields}
    if isinstance(value, list):
        item_shapes = {
            json.dumps(
                _shape(
                    item, depth=depth + 1, counters=counters,
                    sensitive_fields=sensitive_fields),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            for item in value[:2]
        }
        return {
            "type": "array",
            "empty": len(value) == 0,
            "item_shapes": [json.loads(item) for item in sorted(item_shapes)],
        }
    if isinstance(value, str) and len(value) > 65536:
        raise SchemaObservationError("schema_scalar_length_limit_exceeded")
    return {"type": _scalar_type(value), "nullable": value is None}


@dataclass(frozen=True)
class SchemaObservation:
    environment_id: str
    safe_source_path: str
    fingerprint: str
    shape: dict[str, Any]
    sensitive_field_count: int
    candidate_record_count: int | None


def observe_schema(
    payload: Any, environment_id: str, source_url: str,
) -> SchemaObservation:
    parsed = urlsplit(source_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SchemaObservationError("schema_source_invalid") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or \
            parsed.password or port not in {None, 443}:
        raise SchemaObservationError("schema_source_invalid")
    safe_source_path = f"https://{parsed.hostname.casefold()}{parsed.path}"
    sensitive_fields: list[str] = []
    shape = _shape(
        payload, depth=0, counters={"fields": 0},
        sensitive_fields=sensitive_fields)
    canonical = json.dumps({
        "fingerprint_version": FINGERPRINT_VERSION,
        "environment_id": environment_id,
        "safe_source_path": safe_source_path,
        "shape": shape,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    candidate_count = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict) and \
            isinstance(payload["data"].get("items"), list):
        candidate_count = len(payload["data"]["items"])
    return SchemaObservation(
        environment_id=environment_id,
        safe_source_path=safe_source_path,
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        shape=shape,
        sensitive_field_count=len(sensitive_fields),
        candidate_record_count=candidate_count,
    )


class SchemaAdapterRegistry:
    def __init__(self, path: Path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("registry_version") != "domestic_uat_log_schema_registry_v1":
            raise SchemaObservationError("schema_registry_version_invalid")
        self.registry_version = raw["registry_version"]
        self.adapters = tuple(raw.get("adapters", ()))

    def select(
        self, observation: SchemaObservation,
    ) -> dict[str, Any] | None:
        if observation.sensitive_field_count:
            raise SchemaObservationError("credential_like_field_present")
        for adapter in self.adapters:
            if adapter.get("environment_id") != observation.environment_id:
                continue
            if adapter.get("safe_source_path") != observation.safe_source_path:
                continue
            accepted = set(adapter.get("accepted_schema_fingerprints", ()))
            if observation.fingerprint not in accepted:
                if adapter.get("adapter_mode") != "reviewed_billing_records_v1" or \
                        not self._matches_reviewed_billing_shape(
                            adapter, observation.shape):
                    continue
            return dict(adapter)
        return None

    @staticmethod
    def _matches_reviewed_billing_shape(
        adapter: dict[str, Any], shape: dict[str, Any],
    ) -> bool:
        try:
            data = shape["fields"]["data"]["fields"]
            item_shapes = data["items"]["item_shapes"]
        except (KeyError, TypeError):
            return False
        required = set(adapter.get("required_record_fields", ()))
        allowed = set(adapter.get("allowed_record_fields", ()))
        if not isinstance(item_shapes, list) or not item_shapes or \
                not required or not required.issubset(allowed):
            return False
        for item_shape in item_shapes:
            if item_shape.get("type") != "object":
                return False
            fields = set((item_shape.get("fields") or {}).keys())
            if not required.issubset(fields) or not fields.issubset(allowed):
                return False
        return True

    @staticmethod
    def extract(
        adapter: dict[str, Any], payload: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        mode = adapter.get("adapter_mode")
        if mode not in {"empty_envelope_only", "reviewed_billing_records_v1"}:
            raise SchemaObservationError("schema_adapter_mode_invalid")
        if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), dict):
            raise SchemaObservationError("schema_required_field_invalid")
        data = payload["data"]
        if not isinstance(data.get("items"), list):
            raise SchemaObservationError("schema_required_field_invalid")
        if mode == "empty_envelope_only" and data["items"]:
            raise SchemaObservationError("schema_mapping_required")
        if mode == "reviewed_billing_records_v1":
            required = set(adapter.get("required_record_fields", ()))
            allowed = set(adapter.get("allowed_record_fields", ()))
            for item in data["items"]:
                if not isinstance(item, dict):
                    raise SchemaObservationError("schema_required_field_invalid")
                fields = set(item)
                if not required.issubset(fields) or not fields.issubset(allowed):
                    raise SchemaObservationError("schema_mapping_required")
        for key in ("page", "page_size", "total"):
            if not isinstance(data.get(key), int) or isinstance(
                    data.get(key), bool):
                raise SchemaObservationError("schema_required_field_invalid")
        return list(data["items"]), {
            "page": data["page"],
            "page_size": data["page_size"],
            "total": data["total"],
        }
