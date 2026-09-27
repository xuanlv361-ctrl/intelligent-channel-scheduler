"""Run bounded China-UAT multimodal probes using the encrypted Windows vault."""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import requests

from backend.acceptance_run_service import AcceptanceRunService
from backend.call_log_service import CallLogService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope,
)
from backend.uat_http_workbench_service import execute_workbench
from backend.uat_service import UatSettings


ROOT = Path(__file__).resolve().parents[1]
DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
ASSETS = ROOT / "test-assets" / "multimodal"
SCOPE = TenantScope.local_development()


def credential() -> str:
    scope = CredentialScope(
        "windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
        DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat",
    )
    loaded = PersistentCredentialVault().load(scope, required=True)
    assert loaded is not None
    return loaded[0]


def safe_result(result: dict) -> dict:
    response = str(result.get("response_body") or "")
    return {
        key: result.get(key) for key in (
            "execution_status", "request_id", "decision_id", "response_id",
            "requested_model", "actual_model", "channel_id", "provider",
            "http_status", "method", "path", "total_latency_ms",
            "first_token_latency_ms", "input_tokens", "cached_input_tokens",
            "output_tokens", "total_tokens", "cost_amount", "currency",
            "error_source", "is_fault_injected", "created_at",
        )
    } | {"response_excerpt": response[:800], "response_length": len(response)}


def workbench_case(key: str, run_id: str, *, model: str, path: str,
                   payload: dict, request_type: str, timeout: int = 60) -> dict:
    body = {
        "method": "POST", "environment_id": "china_uat", "path": path,
        "query_params": [], "headers": [],
        "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
        "body": {"type": "json", "value": payload}, "content_type": "application/json",
        "timeout_seconds": timeout, "stream": False, "model": model,
        "model_selection_mode": "specified", "routing_policy": "direct_model",
        "request_type": request_type, "acceptance_run_id": run_id,
        "_strategy_variant": "multimodal_live_probe",
    }
    result = execute_workbench(
        body, replace(UatSettings.load("china_uat"), enabled=True), key, connection_verified=True,
        call_logs=CallLogService(DB, SCOPE), capabilities=None,
    )
    AcceptanceRunService(DB, SCOPE).link_evidence(
        run_id, request_id=result.get("request_id"), decision_id=result.get("decision_id"))
    return safe_result(result)


def audio_case(key: str, run_id: str) -> dict:
    logs = CallLogService(DB, SCOPE)
    request_id = "REQ-" + uuid.uuid4().hex.upper()
    decision_id = "DEC-" + uuid.uuid4().hex.upper()
    record_id = logs.start_execution(
        request_id=request_id, decision_id=decision_id, environment_id="china_uat",
        requested_model="whisper-1", stream=False, endpoint_type="uat_http_post_audio_transcriptions",
        acceptance_run_id=run_id, error_source="provider_live", is_fault_injected=False,
        strategy_variant="multimodal_live_probe")
    started = time.perf_counter()
    path = ASSETS / "test-audio-10s.wav"
    try:
        with path.open("rb") as handle:
            response = requests.post(
                "https://api-uat.weimeta.cn/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data={"model": "whisper-1", "language": "zh"},
                files={"file": (path.name, handle, "audio/wav")}, timeout=60,
                allow_redirects=False,
            )
        elapsed = (time.perf_counter() - started) * 1000
        try: payload = response.json()
        except ValueError: payload = {}
        success = 200 <= response.status_code < 300
        logs.finish_execution(
            record_id, status="SUCCESS" if success else "FAILED",
            response_id=response.headers.get("x-request-id"), http_status=response.status_code,
            actual_model="whisper-1" if success else None,
            error_code=None if success else f"http_{response.status_code}",
            error_category=None if success else (
                "user_authentication_error" if response.status_code in {401, 403}
                else "rate_limited" if response.status_code == 429
                else "upstream_5xx" if response.status_code >= 500
                else "user_parameter_error"),
            retryable=False, total_latency_ms=elapsed, total_attempts=1,
            error_source="provider_live", is_fault_injected=False,
            strategy_variant="multimodal_live_probe")
        AcceptanceRunService(DB, SCOPE).link_evidence(
            run_id, request_id=request_id, decision_id=decision_id)
        return {
            "execution_status": "success" if success else "failed",
            "request_id": request_id, "decision_id": decision_id,
            "response_id": response.headers.get("x-request-id"), "requested_model": "whisper-1",
            "actual_model": "whisper-1" if success else None, "http_status": response.status_code,
            "path": "/v1/audio/transcriptions", "total_latency_ms": elapsed,
            "response_excerpt": json.dumps(payload, ensure_ascii=False)[:800],
        }
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logs.finish_execution(
            record_id, status="FAILED", error_code="transport_error",
            error_category="network_timeout" if isinstance(exc, requests.Timeout) else "network_error",
            retryable=False, total_latency_ms=elapsed, error_source="provider_live",
            is_fault_injected=False, strategy_variant="multimodal_live_probe")
        return {"execution_status": "failed", "request_id": request_id,
                "decision_id": decision_id, "http_status": None,
                "path": "/v1/audio/transcriptions", "total_latency_ms": elapsed,
                "error": type(exc).__name__}


def main() -> None:
    run = AcceptanceRunService(DB, SCOPE).active_run("china_uat")
    if not run: raise RuntimeError("active_acceptance_run_required")
    run_id = run["acceptance_run_id"]
    key = credential()
    image_data = base64.b64encode((ASSETS / "test-image-1920x1080.png").read_bytes()).decode()
    results = {
        "acceptance_run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "image": workbench_case(key, run_id, model="qwen3-vl-plus",
            path="/v1/chat/completions", request_type="image", payload={
                "model": "qwen3-vl-plus", "stream": False, "max_tokens": 400,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "识别图片中的中英文、数字、三种颜色图形和折线图，并简洁说明明暗区域。"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_data}},
                ]}],
            }),
        "audio": audio_case(key, run_id),
        "video_generation": workbench_case(key, run_id,
            model="dreamina-seedance-2-0-fast-260128", path="/v1/videos/generations",
            request_type="video", timeout=60, payload={
                "model": "dreamina-seedance-2-0-fast-260128",
                "prompt": "生成一段高质量短视频：从海面蒸发开始，水汽形成云，随后降雨，雨水经过河流回到海洋。画面清晰、阶段明确、无人物、无品牌标志。",
                "duration": 5, "resolution": "1080p",
            }),
    }
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence_dir = ROOT / "evidence" / "final_acceptance" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "multimodal_live.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__": main()
