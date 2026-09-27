"""Generic, fail-closed HTTP workbench for approved UAT environments.

This module deliberately does not resolve environment or persistent credentials.
A caller must supply a short-lived browser-session credential. A separate catalog
read can verify the key, but direct requests intentionally preserve upstream 401/403
responses instead of requiring a preflight connection test.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from backend.call_log_service import CallLogService
from backend.uat_service import UatSettings, redact, runtime_error


LOGGER = logging.getLogger(__name__)


ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORBIDDEN_HEADERS = frozenset({
    "authorization", "host", "content-length", "connection", "proxy-authorization",
    "cookie", "set-cookie", "x-correlation-id", "x-request-id", "forwarded",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
})
SAFE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "allow", "access-control-allow-origin",
    "access-control-allow-methods", "access-control-allow-headers", "date",
    "retry-after", "x-request-id", "request-id", "x-oneapi-request-id",
    "x-client-request-id", "x-trace-id", "trace-id", "x-ds-trace-id",
    "x-response-id", "x-channel-id", "x-provider",
})
AUTH_HEADER_NAMES = frozenset({"authorization", "x-api-key"})
MAX_RESPONSE_BYTES = 1_000_000
MAX_TIMEOUT_SECONDS = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRETISH = re.compile(r"(?i)(bearer\s+\S+|(?:api[_-]?key|authorization|cookie|password)\s*[:=]\s*\S+)")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkbenchError(ValueError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _public_addresses(host: str) -> tuple[str, ...]:
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses:
        raise WorkbenchError("uat_dns_resolution_empty", 502)
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise WorkbenchError("uat_dns_address_not_public", 403)
    return tuple(sorted(addresses))


def default_transport(spec: dict[str, Any], api_key: str,
                      on_chunk: Callable[[bytes], None] | None = None) -> dict[str, Any]:
    """Send exactly one request without redirects or retries."""
    parsed = urllib.parse.urlsplit(spec["url"])
    _public_addresses(parsed.hostname or "")
    headers = dict(spec["headers"])
    auth = spec["auth"]
    if auth["method"] == "bearer":
        headers["Authorization"] = f"{auth['prefix']} {api_key}".strip()
    elif auth["method"] == "api_key_header":
        headers[auth["header_name"]] = f"{auth['prefix']} {api_key}".strip()
    request = urllib.request.Request(
        spec["url"], data=spec["body_bytes"], headers=headers, method=spec["method"])
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    started = time.perf_counter()
    try:
        response = opener.open(request, timeout=spec["timeout_seconds"])
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        first_token_latency_ms = None
        if spec["method"] == "HEAD":
            body = b""
        elif spec.get("stream"):
            chunks: list[bytes] = []
            observed_bytes = 0
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                if first_token_latency_ms is None:
                    first_token_latency_ms = round((time.perf_counter() - started) * 1000, 2)
                observed_bytes += len(chunk)
                if observed_bytes > MAX_RESPONSE_BYTES:
                    raise WorkbenchError("uat_response_too_large", 502)
                chunks.append(chunk)
                if on_chunk is not None:
                    on_chunk(chunk)
            body = b"".join(chunks)
        else:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise WorkbenchError("uat_response_too_large", 502)
        return {
            "status": int(response.status),
            "headers": {str(k).lower(): str(v) for k, v in response.headers.items()},
            "body": body,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "first_token_latency_ms": first_token_latency_ms,
        }


@dataclass
class ConnectionProof:
    environment_id: str
    credential_fingerprint: str
    auth_session_id: str
    tested_at: str
    expires_at: str


class CredentialConnectionRegistry:
    """Process-local proof that a temporary credential passed `/v1/models`."""
    def __init__(self) -> None:
        self._proofs: dict[tuple[str, str], ConnectionProof] = {}

    def record(self, session_id: str, proof: ConnectionProof) -> None:
        self._proofs[(session_id, proof.environment_id)] = proof

    def clear(self, session_id: str | None, environment_id: str) -> None:
        if session_id:
            self._proofs.pop((session_id, environment_id), None)

    def verified(self, session_id: str | None, environment_id: str,
                 fingerprint: str, auth_session_id: str) -> bool:
        if not session_id:
            return False
        proof = self._proofs.get((session_id, environment_id))
        return bool(proof and proof.credential_fingerprint == fingerprint and
                    proof.auth_session_id == auth_session_id)


def _pairs(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise WorkbenchError(f"{field}_invalid")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise WorkbenchError(f"{field}_item_invalid")
        if item.get("enabled", True) is False:
            continue
        name, raw = str(item.get("name") or "").strip(), item.get("value", "")
        if not name or len(name) > 128 or _CONTROL.search(name):
            raise WorkbenchError(f"{field}_name_invalid")
        text = str(raw)
        if len(text) > 8192 or _CONTROL.search(text):
            raise WorkbenchError(f"{field}_value_invalid")
        result.append({"name": name, "value": text, "type": str(item.get("type") or "string")})
    return result


def _normalize_path(path: Any) -> str:
    raw = str(path or "").strip()
    if not raw or len(raw) > 2048 or _CONTROL.search(raw) or "\\" in raw:
        raise WorkbenchError("uat_path_invalid")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not raw.startswith("/"):
        raise WorkbenchError("uat_path_must_be_relative")
    decoded = urllib.parse.unquote(raw)
    if any(part == ".." for part in decoded.split("/")) or decoded.startswith("//"):
        raise WorkbenchError("uat_path_traversal_rejected")
    normalized = "/" + "/".join(part for part in raw.split("/") if part not in {"", "."})
    return normalized or "/"


def _normalize_headers(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _pairs(value, "headers"):
        name, lowered = item["name"], item["name"].casefold()
        if lowered in FORBIDDEN_HEADERS or lowered.startswith("x-internal-"):
            raise WorkbenchError("uat_header_forbidden")
        result[name] = item["value"]
    return result


def _normalize_auth(value: Any) -> dict[str, str]:
    auth = value if isinstance(value, dict) else {}
    method = str(auth.get("method") or "none").strip().casefold()
    if method not in {"bearer", "api_key_header", "none"}:
        raise WorkbenchError("uat_auth_method_invalid")
    header = str(auth.get("header_name") or ("Authorization" if method == "bearer" else "X-API-Key")).strip()
    prefix = str(auth.get("prefix") if auth.get("prefix") is not None else ("Bearer" if method == "bearer" else "")).strip()
    if method != "none" and (header.casefold() not in AUTH_HEADER_NAMES or _CONTROL.search(header)):
        raise WorkbenchError("uat_auth_header_not_allowed")
    if len(prefix) > 32 or _CONTROL.search(prefix):
        raise WorkbenchError("uat_auth_prefix_invalid")
    return {"method": method, "header_name": header, "prefix": prefix}


def _body(value: Any, method: str, content_type: str | None) -> tuple[str, bytes | None, str | None, Any]:
    item = value if isinstance(value, dict) else {"type": "none", "value": None}
    body_type = str(item.get("type") or "none").casefold()
    if body_type not in {"none", "json", "raw", "form_data", "urlencoded"}:
        raise WorkbenchError("uat_body_type_invalid")
    raw = item.get("value")
    if method in {"GET", "HEAD"} and body_type != "none":
        raise WorkbenchError("uat_method_body_not_supported")
    if body_type == "none":
        return body_type, None, None, None
    if body_type == "json":
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise WorkbenchError("uat_json_body_invalid") from exc
        else:
            parsed = raw
        encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()
        return body_type, encoded, content_type or "application/json", parsed
    if body_type in {"form_data", "urlencoded"}:
        pairs = [(row["name"], row["value"]) for row in _pairs(raw, "body_fields")]
        if body_type == "urlencoded":
            encoded = urllib.parse.urlencode(pairs).encode()
            media = "application/x-www-form-urlencoded"
        else:
            boundary = f"ics-{uuid.uuid4().hex}"
            chunks: list[bytes] = []
            for name, value in pairs:
                safe_name = name.replace('"', "")
                chunks.extend((
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode(),
                    value.encode(), b"\r\n",
                ))
            chunks.append(f"--{boundary}--\r\n".encode())
            encoded = b"".join(chunks)
            media = f"multipart/form-data; boundary={boundary}"
        if len(encoded) > 1_000_000:
            raise WorkbenchError("uat_request_body_too_large")
        return body_type, encoded, content_type or media, [{"name": k, "value": v} for k, v in pairs]
    text = str(raw or "")
    if len(text.encode()) > 1_000_000:
        raise WorkbenchError("uat_request_body_too_large")
    return body_type, text.encode(), content_type or "text/plain; charset=utf-8", "[raw body omitted]"


def normalize_request(body: dict[str, Any], settings: UatSettings) -> dict[str, Any]:
    method = str(body.get("method") or "").upper()
    if method not in ALLOWED_METHODS:
        raise WorkbenchError("uat_http_method_not_allowed", 405)
    path = _normalize_path(body.get("path"))
    base = urllib.parse.urlsplit(settings.base_url)
    if base.scheme != "https" or base.hostname not in settings.allowed_hosts or base.path not in {"", "/"}:
        raise WorkbenchError("uat_environment_target_not_allowed", 403)
    query = _pairs(body.get("query_params"), "query_params")
    query_string = urllib.parse.urlencode([(item["name"], item["value"]) for item in query])
    url = urllib.parse.urlunsplit(("https", base.netloc, path, query_string, ""))
    headers = _normalize_headers(body.get("headers"))
    auth = _normalize_auth(body.get("auth"))
    timeout = body.get("timeout_seconds", body.get("timeout", 30))
    if not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= MAX_TIMEOUT_SECONDS:
        raise WorkbenchError("uat_timeout_invalid")
    body_type, body_bytes, media_type, safe_body = _body(body.get("body"), method, body.get("content_type"))
    if media_type:
        headers["Content-Type"] = media_type
    model = str(body.get("model") or "").strip() or None
    request_type = str(body.get("request_type") or "text").strip().casefold()
    if request_type not in {"text", "image", "audio", "video"}:
        raise WorkbenchError("uat_request_type_invalid")
    routing_policy = str(body.get("routing_policy") or "").strip() or None
    model_selection_mode = str(body.get("model_selection_mode") or "specified").strip().casefold()
    if model_selection_mode not in {"specified", "automatic"}:
        raise WorkbenchError("uat_model_selection_mode_invalid")
    return {
        "method": method, "environment_id": settings.environment_id,
        "base_url": settings.base_url, "path": path, "url": url,
        "query_params": query, "headers": headers, "auth": auth,
        "body_type": body_type, "body_bytes": body_bytes, "safe_body": safe_body,
        "content_type": media_type, "timeout_seconds": float(timeout),
        "stream": body.get("stream") is True, "model": model,
        "request_type": request_type,
        "multimodal_validation_id": str(body.get("multimodal_validation_id") or "").strip() or None,
        "channel_id": str(body.get("channel_id") or "").strip() or None,
        "routing_policy": routing_policy,
        "model_selection_mode": model_selection_mode,
        "execution_limits": body.get("execution_limits") if isinstance(body.get("execution_limits"), dict) else {},
    }


def validate_workbench(body: dict[str, Any], settings: UatSettings, *,
                       credential_configured: bool = False,
                       connection_verified: bool = False,
                       capabilities: dict[str, Any] | None = None,
                       for_execution: bool = False) -> dict[str, Any]:
    try:
        spec = normalize_request(body, settings)
    except WorkbenchError as exc:
        return {"valid": False, "structurally_valid": False, "errors": [exc.code],
                "blocking_reasons": [exc.code], "warnings": [], "safe_preview": None}
    blockers: list[str] = []
    warnings: list[str] = []
    if for_execution:
        if not settings.enabled:
            blockers.append("real_execution_disabled")
        if spec["auth"]["method"] == "none" or not credential_configured:
            blockers.append("temporary_api_key_required")
    if spec["method"] in WRITE_METHODS:
        warnings.append("uat_write_method_selected")
    if spec["path"] == "/v1/chat/completions" or spec["model"]:
        if not spec["model"] and spec["model_selection_mode"] != "automatic":
            blockers.append("model_required")
        elif capabilities is not None:
            capability = capabilities.get(spec["model"])
            status = str((capability or {}).get("status") or "").casefold()
            if status in {"unsupported", "not_supported", "incompatible"}:
                blockers.append("capability_unsupported")
            elif status not in {"confirmed", "active", "supported"}:
                warnings.append("capability_pending")
    safe_headers = {key: value for key, value in spec["headers"].items()
                    if key.casefold() not in AUTH_HEADER_NAMES}
    preview = {
        "method": spec["method"], "environment_id": spec["environment_id"],
        "base_url": spec["base_url"], "path": spec["path"],
        "query_params": spec["query_params"], "headers": safe_headers,
        "auth": {"method": spec["auth"]["method"], "header_name": spec["auth"]["header_name"],
                 "credential": "[temporary session credential]" if credential_configured else "[not configured]"},
        "body": spec["safe_body"], "content_type": spec["content_type"],
        "timeout_seconds": spec["timeout_seconds"], "stream": spec["stream"],
        "request_type": spec["request_type"],
        "multimodal_validation_id": spec["multimodal_validation_id"],
        "model": spec["model"], "channel_id": spec["channel_id"],
        "routing_policy": spec["routing_policy"],
        "model_selection_mode": spec["model_selection_mode"],
    }
    return {"valid": not blockers, "structurally_valid": True, "errors": [],
            "blocking_reasons": list(dict.fromkeys(blockers)), "warnings": warnings,
            "safe_preview": preview}


def execute_workbench(body: dict[str, Any], settings: UatSettings, api_key: str, *,
                      connection_verified: bool, call_logs: CallLogService,
                      capabilities: dict[str, Any] | None = None,
                      transport: Callable[[dict[str, Any], str], dict[str, Any]] = default_transport,
                      performance_recorder: Any | None = None) -> dict[str, Any]:
    end_to_end_started = time.perf_counter()
    stage_durations = {str(key): float(value) for key, value in
                       dict(body.get("_scheduler_stage_durations") or {}).items()}
    stage_started = time.perf_counter()
    validation = validate_workbench(
        body, settings, credential_configured=bool(api_key),
        connection_verified=connection_verified, capabilities=capabilities,
        for_execution=True)
    stage_durations["request_validation"] = (time.perf_counter()-stage_started)*1000
    if not validation["valid"]:
        raise WorkbenchError(validation["blocking_reasons"][0], 403)
    spec = normalize_request(body, settings)
    acceptance_run_id = str(body.get("acceptance_run_id") or "").strip() or None
    error_source = str(body.get("_error_source") or "provider_live")
    fault_id = str(body.get("_fault_id") or "").strip() or None
    is_fault_injected = bool(body.get("_is_fault_injected", False))
    traffic_proposal_id = str(body.get("_traffic_proposal_id") or "").strip() or None
    strategy_variant = str(body.get("_strategy_variant") or body.get("routing_policy") or "").strip() or None
    traffic_class = str(body.get("traffic_class") or "business").strip().casefold()
    if traffic_class not in {"business", "probe"}:
        raise WorkbenchError("invalid_traffic_class", 400)
    strategy_effect_run_id = str(body.get("strategy_effect_run_id") or "").strip() or None
    probe_run_id = str(body.get("probe_run_id") or "").strip() or None
    if traffic_class == "probe" and not probe_run_id:
        raise WorkbenchError("probe_run_id_required", 400)
    request_id = "REQ-" + uuid.uuid4().hex.upper()
    decision_id = "DEC-" + uuid.uuid4().hex.upper() if (spec["model"] or spec["routing_policy"]) else None
    stage_started = time.perf_counter()
    record_id = call_logs.start_execution(
        request_id=request_id, decision_id=decision_id,
        environment_id=settings.environment_id,
        requested_model=spec["model"] or "non_model_uat_http",
        stream=spec["stream"], channel_id=spec["channel_id"],
        channel_source=(str(body.get("_channel_source") or "unknown")
                        if spec["channel_id"] else "unknown"),
        configuration_version=str(body.get("_configuration_version") or "") or None,
        endpoint_type=f"uat_http_{spec['method'].lower()}",
        acceptance_run_id=acceptance_run_id, error_source=error_source,
        is_fault_injected=is_fault_injected, fault_id=fault_id,
        fallback_used=False, output_started=False,
        traffic_proposal_id=traffic_proposal_id, strategy_variant=strategy_variant,
        traffic_class=traffic_class, strategy_effect_run_id=strategy_effect_run_id,
        probe_run_id=probe_run_id)
    stage_durations["decision_persistence"] = (time.perf_counter()-stage_started)*1000
    stage_started = time.perf_counter()
    fallback_candidates=list((body.get("_routing_decision") or {}).get("candidates") or [])
    stage_durations["fallback_plan"] = (time.perf_counter()-stage_started)*1000
    started = time.perf_counter()

    def record_performance(*,successful:bool,provider_latency:float|None,
                           provider_request_id_value:str|None,error_category_value:str|None) -> None:
        if performance_recorder is None:return
        measured=sum(value for key,value in stage_durations.items() if key!="scheduler_total")
        stage_durations["scheduler_total"]=measured
        try:
            performance_recorder.record_trace(local_request_id=request_id,
              provider_request_id=provider_request_id_value,decision_id=decision_id,
              environment_id=settings.environment_id,tenant_id=str(body.get("_tenant_id") or "tenant_local_dev_v1"),
              strategy_id=strategy_variant or ("automatic" if body.get("model_selection_mode")=="automatic" else "specified_model"),
              model_id=spec["model"] or None,traffic_class=traffic_class,stream=spec["stream"],
              stage_durations=stage_durations,provider_latency_ms=provider_latency,
              end_to_end_ms=(time.perf_counter()-end_to_end_started)*1000,success=successful,
              error_category=error_category_value,configuration_version=str(body.get("_configuration_version") or "") or None,
              metrics_snapshot_id=str(body.get("_metrics_snapshot_id") or "") or None,
              candidate_count=len(fallback_candidates) if fallback_candidates else (1 if spec["model"] else 0),
              filtered_count=len((body.get("_routing_decision") or {}).get("excluded") or []),
              source_type="realtime_execution")
        except Exception:
            # Performance telemetry must not change a real UAT outcome.
            LOGGER.warning(
                "scheduler_performance_telemetry_write_failed request_id=%s decision_id=%s",
                request_id,
                decision_id,
                exc_info=True,
            )
    try:
        observed = transport(spec, api_key)
        fault_metadata = (observed.get("_fault_metadata")
                          if isinstance(observed.get("_fault_metadata"), dict) else None)
        if fault_metadata:
            error_source = str(fault_metadata.get("error_source") or error_source)
            is_fault_injected = bool(fault_metadata.get("is_fault_injected"))
            fault_id = str(fault_metadata.get("fault_id") or "").strip() or fault_id
        status = int(observed.get("status") or 0)
        elapsed = float(observed.get("elapsed_ms") or ((time.perf_counter() - started) * 1000))
        headers = {str(k).casefold(): str(v) for k, v in (observed.get("headers") or {}).items()}
        body_bytes = observed.get("body") or b""
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode()
        response_payload: dict[str, Any] = {}
        if spec["method"] != "HEAD" and "json" in headers.get("content-type", "").casefold():
            try:
                parsed_payload = json.loads(body_bytes.decode("utf-8", "replace"))
                if isinstance(parsed_payload, dict):
                    response_payload = parsed_payload
            except json.JSONDecodeError:
                response_payload = {}
        stream_chunk_count = 0
        stream_parse_errors = 0
        if spec["stream"] and body_bytes:
            for line in body_bytes.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:"):
                    continue
                raw_chunk = line[5:].strip()
                if not raw_chunk or raw_chunk == "[DONE]":
                    continue
                try:
                    chunk_payload = json.loads(raw_chunk)
                except json.JSONDecodeError:
                    stream_parse_errors += 1
                    continue
                if not isinstance(chunk_payload, dict):
                    continue
                stream_chunk_count += 1
                for field in ("id", "model", "usage"):
                    if chunk_payload.get(field) is not None:
                        response_payload[field] = chunk_payload[field]
        usage = response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        actual_model = str(response_payload.get("model") or "").strip() or None
        provider_response_id = str(response_payload.get("id") or "").strip() or (
            headers.get("x-response-id") or None)
        provider_request_id = (headers.get("x-oneapi-request-id") or
                               headers.get("request-id") or
                               headers.get("x-request-id") or None)
        provider_trace_id = (headers.get("x-trace-id") or headers.get("trace-id") or
                             headers.get("x-ds-trace-id") or None)
        sent_client_correlation_id = next((str(value) for name, value in
            spec["headers"].items() if str(name).casefold() ==
            "x-client-request-id"), None)
        client_correlation_id = sent_client_correlation_id
        provider_identifiers = {
            key: value for key, value in headers.items()
            if key in SAFE_RESPONSE_HEADERS and (
                "request-id" in key or "trace-id" in key or "response-id" in key
            ) and value
        }
        if provider_response_id:
            provider_identifiers["response_body.id"] = provider_response_id
        response_id = provider_response_id
        provider_channel_id = headers.get("x-channel-id")
        channel_id = provider_channel_id or spec["channel_id"]
        channel_source = "provider_log" if provider_channel_id else (
            str(body.get("_channel_source") or "unknown") if channel_id else "unknown")
        provider = headers.get("x-provider")
        input_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
        cached_input_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details.get("cached_tokens"), int) else None
        raw_cost = (usage.get("cost") if usage.get("cost") is not None else
                    response_payload.get("cost") if response_payload.get("cost") is not None else
                    headers.get("x-request-cost"))
        try:
            cost_amount = Decimal(str(raw_cost)) if raw_cost not in (None, "") else None
        except Exception:
            cost_amount = None
        currency = (str(response_payload.get("currency") or headers.get("x-cost-currency") or "").upper()
                    or ("CNY" if cost_amount is not None else None))
        safe_text = "" if spec["method"] == "HEAD" else redact(body_bytes[:MAX_RESPONSE_BYTES].decode("utf-8", "replace"))
        successful = 200 <= status < 300
        if successful and spec["stream"] and stream_parse_errors and stream_chunk_count == 0:
            successful = False
            status = 502
        usage_inconsistent = bool(
            isinstance(usage.get("total_tokens"), int) and
            isinstance(input_tokens, int) and isinstance(output_tokens, int) and
            usage["total_tokens"] != input_tokens + output_tokens)
        category = ("sse_protocol_error" if stream_parse_errors and stream_chunk_count == 0 else
                    None if successful else ("user_authentication_error" if status in {401, 403} else
                                            "rate_limited" if status == 429 else
                                            "upstream_timeout" if status == 524 else
                                            "upstream_5xx" if status >= 500 else "user_parameter_error"))
        first_token_latency_ms = observed.get("first_token_latency_ms")
        stage_started = time.perf_counter()
        call_logs.finish_execution(
            record_id, status="SUCCESS" if successful else "FAILED",
            response_id=response_id, provider_request_id=provider_request_id,
            provider_response_id=provider_response_id,
            provider_trace_id=provider_trace_id,
            client_correlation_id=client_correlation_id,
            provider_identifiers=provider_identifiers,
            actual_model=actual_model, http_status=status,
            error_code=None if successful else f"http_{status}",
            error_category=category, retryable=False, total_latency_ms=elapsed,
            first_token_latency_ms=first_token_latency_ms, input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens, output_tokens=output_tokens,
            channel_id=channel_id, provider=provider, total_attempts=1,
            channel_source=channel_source,
            provider_log_id=(headers.get("x-provider-log-id") or headers.get("x-log-id")),
            cost_amount=cost_amount, currency=currency,
            error_source=error_source, is_fault_injected=is_fault_injected,
            fault_id=fault_id, fallback_used=False,
            output_started=bool(first_token_latency_ms is not None or (spec["stream"] and body_bytes)),
            traffic_proposal_id=traffic_proposal_id, strategy_variant=strategy_variant,
            cost_source="actual_provider_cost" if cost_amount is not None else None)
        stage_durations["audit_write"] = (time.perf_counter()-stage_started)*1000
        record_performance(successful=successful,provider_latency=elapsed,
          provider_request_id_value=provider_request_id,error_category_value=category)
        return {
            "execution_status": "success" if successful else "failed",
            "request_id": request_id, "decision_id": decision_id,
            "local_request_id": request_id,
            "response_id": response_id,
            "provider_request_id": provider_request_id,
            "provider_response_id": provider_response_id,
            "provider_trace_id": provider_trace_id,
            "client_correlation_id": client_correlation_id,
            "requested_model": spec["model"],
            "actual_model": actual_model, "channel_id": channel_id,
            "channel_source": channel_source, "provider": provider,
            "http_status": status, "method": spec["method"], "path": spec["path"],
            "response_headers": {k: v for k, v in headers.items() if k in SAFE_RESPONSE_HEADERS},
            "response_body": None if spec["method"] == "HEAD" else safe_text,
            "response_body_truncated": len(body_bytes) > MAX_RESPONSE_BYTES,
            "total_latency_ms": elapsed, "network_called": True,
            "first_token_latency_ms": first_token_latency_ms,
            "input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get("total_tokens") if isinstance(
                usage.get("total_tokens"), int) else None,
            "cost_amount": str(cost_amount) if cost_amount is not None else None,
            "currency": currency,
            "attempts": 1, "retry_used": False, "fallback_used": False,
            "acceptance_run_id": acceptance_run_id,
            "error_source": error_source, "is_fault_injected": is_fault_injected,
            "fault_id": fault_id, "traffic_proposal_id": traffic_proposal_id,
            "fault_audit_id": (fault_metadata or {}).get("audit_id"),
            "fault_affects_circuit": (fault_metadata or {}).get("affects_circuit"),
            "strategy_variant": strategy_variant,
            "traffic_class": traffic_class,
            "strategy_effect_run_id": strategy_effect_run_id,
            "probe_run_id": probe_run_id,
            "error": None if successful else runtime_error(category or "unknown", f"HTTP {status}"),
            "usage_status": ("inconsistent" if usage_inconsistent else
                             "missing" if not usage else "reported"),
            "stream_parse_errors": stream_parse_errors,
            "created_at": utcnow(),
        }
    except WorkbenchError as exc:
        interrupted = exc.code == "uat_stream_cancelled"
        provider_elapsed=(time.perf_counter()-started)*1000
        stage_started=time.perf_counter()
        call_logs.finish_execution(record_id, status="INTERRUPTED" if interrupted else "FAILED",
                                   error_code="client_stream_cancelled" if interrupted else "transport_policy_error",
                                   error_category="cancelled" if interrupted else "protocol_conversion_error", retryable=False,
                                   total_latency_ms=provider_elapsed,
                                   error_source=error_source,is_fault_injected=is_fault_injected,
                                   fault_id=fault_id,traffic_proposal_id=traffic_proposal_id,
                                   strategy_variant=strategy_variant)
        stage_durations["audit_write"] = (time.perf_counter()-stage_started)*1000
        record_performance(successful=False,provider_latency=provider_elapsed,
          provider_request_id_value=None,error_category_value="protocol_conversion_error")
        raise
    except Exception as exc:
        provider_elapsed=(time.perf_counter()-started)*1000
        stage_started=time.perf_counter()
        call_logs.finish_execution(record_id, status="FAILED", error_code="transport_error",
                                   error_category="network_timeout" if isinstance(exc, TimeoutError) else "unknown",
                                   retryable=False, total_latency_ms=provider_elapsed,
                                   error_source=error_source,is_fault_injected=is_fault_injected,
                                   fault_id=fault_id,traffic_proposal_id=traffic_proposal_id,
                                   strategy_variant=strategy_variant)
        stage_durations["audit_write"] = (time.perf_counter()-stage_started)*1000
        record_performance(successful=False,provider_latency=provider_elapsed,
          provider_request_id_value=None,error_category_value="network_timeout" if isinstance(exc,TimeoutError) else "unknown")
        raise WorkbenchError("uat_transport_failed", 502) from exc


def contains_secret(value: Any, secret: str) -> bool:
    return bool(secret and secret in json.dumps(value, ensure_ascii=False, default=str))
