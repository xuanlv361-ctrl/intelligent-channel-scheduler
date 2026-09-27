"""Bounded real-UAT protocol probe for the catalog-listed Seedance model."""
from __future__ import annotations

import json
from pathlib import Path

from backend.acceptance_run_service import AcceptanceRunService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope
from tools.run_multimodal_live_acceptance import workbench_case


DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"


def main() -> None:
    run = AcceptanceRunService(DB, TenantScope.local_development()).active_run("china_uat")
    if not run: raise RuntimeError("active_acceptance_run_required")
    scope = CredentialScope("windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
                            DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat")
    loaded = PersistentCredentialVault().load(scope, required=True)
    assert loaded is not None
    result = workbench_case(
        loaded[0], run["acceptance_run_id"],
        model="dreamina-seedance-2-0-fast-260128", path="/v1/videos",
        request_type="video", timeout=60,
        payload={
            "model": "dreamina-seedance-2-0-fast-260128",
            "prompt": "生成一段高质量短视频：从海面蒸发开始，水汽形成云，随后降雨，雨水经过河流回到海洋。画面清晰、阶段明确、无人物、无品牌标志。",
            "seconds": "4", "size": "1280x720",
        },
    )
    path = Path(__file__).resolve().parents[1] / "evidence/final_acceptance" / run["acceptance_run_id"] / "video_protocol_probe.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result.get(key) for key in (
        "execution_status", "request_id", "decision_id", "response_id",
        "http_status", "path", "total_latency_ms")}, ensure_ascii=True))


if __name__ == "__main__": main()
