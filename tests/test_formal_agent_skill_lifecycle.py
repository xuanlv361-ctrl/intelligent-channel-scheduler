from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_lifecycle_service import (
    FormalAgentSkillLifecycleService, SkillLifecycleError,
)
from formal_agent_skill_registry import FormalAgentSkillRegistry
from formal_agent_skill_runtime import FormalAgentSkillRuntime


class _Bindings:
    authorization_service = None


def test_business_skills_are_published_installed_and_enabled(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    result = service.list()
    assert result["registered_count"] == 10
    assert result["published_count"] == 10
    assert result["installed_count"] == 10
    assert result["enabled_count"] == 10
    assert {item["skill_id"] for item in result["items"]} == {
        "explain-routing-decision", "inspect-channel-health", "analyze-model-cost",
        "classify-provider-error", "collect-acceptance-evidence", "validate-uat-request",
        "execute-uat-request", "update-routing-policy", "operate-circuit-breaker",
        "propose-traffic-switch",
    }


def test_external_skill_full_install_lifecycle_is_persistent(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    imported = service.import_external(
        skill_id="frontend-design", display_name="Frontend Design", description="design",
        source_type="github_skill_md", source_uri="https://github.com/anthropics/skills",
        content="---\nname: frontend-design\n---\ninstructions", operator_id="operator",
        repository_commit="abc", license_name="Apache-2.0")
    assert imported["skill"]["version_status"] == "draft"
    service.transition_version("frontend-design", "0.1.0", "validate", "operator")
    service.transition_version("frontend-design", "0.1.0", "publish", "operator")
    service.install_action("frontend-design", "install", "operator", "0.1.0")
    service.install_action("frontend-design", "enable", "operator")
    service.create_version("frontend-design", version="0.2.0", content="improved instructions",
                           operator_id="operator", change_summary="improve guidance")
    service.transition_version("frontend-design", "0.2.0", "validate", "operator")
    service.transition_version("frontend-design", "0.2.0", "publish", "operator")
    assert service.install_action("frontend-design", "upgrade", "operator", "0.2.0")["installation"]["installed_version"] == "0.2.0"
    assert service.install_action("frontend-design", "rollback", "operator", "0.1.0")["installation"]["installed_version"] == "0.1.0"
    service.install_action("frontend-design", "disable", "operator")
    assert service.install_action("frontend-design", "enable", "operator")["installation"]["installation_status"] == "enabled"
    service.install_action("frontend-design", "disable", "operator")
    service.install_action("frontend-design", "uninstall", "operator")
    reopened = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    assert reopened.get("frontend-design")["installation"]["installation_status"] == "uninstalled"


def test_incomplete_toml_cannot_publish_or_enable(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    service.import_external(skill_id="frontend-toml", display_name="Frontend", description="agent",
        source_type="codex_agent_toml", source_uri="local-allowlisted", content="instructions",
        operator_id="operator", toml_source={"name": "Frontend"})
    with pytest.raises(SkillLifecycleError, match="security_review_incomplete"):
        service.transition_version("frontend-toml", "0.1.0", "validate", "operator")
    with pytest.raises(SkillLifecycleError, match="skill_not_validated"):
        service.install_action("frontend-toml", "install", "operator", "0.1.0")


def test_frontend_developer_toml_completes_audited_instruction_only_lifecycle(
        tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    service.import_external(
        skill_id="frontend-developer-toml", display_name="Frontend Developer",
        description="review frontend implementation", source_type="codex_agent_toml",
        source_uri="local-allowlisted", content="safe frontend instructions",
        operator_id="operator", toml_source={
            "name": "Frontend Developer", "description": "frontend guidance",
            "developer_instructions": "Review the supplied frontend task.",
        })
    validated = service.transition_version(
        "frontend-developer-toml", "0.1.0", "validate", "operator")
    assert validated["skill"]["version_status"] == "validated"
    assert validated["skill"]["binding"] == "instruction_only"
    assert set(validated["skill"]["requested_permissions"]) == {"read", "local_compute"}
    assert validated["skill"]["allowed_domains"] == []
    service.transition_version(
        "frontend-developer-toml", "0.1.0", "publish", "operator")
    service.install_action(
        "frontend-developer-toml", "install", "operator", "0.1.0")
    service.install_action("frontend-developer-toml", "enable", "operator")
    prepared = service.prepare_invocation(
        "frontend-developer-toml", {"task": "Review this component"})
    assert prepared["version"] == "0.1.0"
    service.install_action("frontend-developer-toml", "disable", "operator")
    service.install_action("frontend-developer-toml", "uninstall", "operator")
    assert service.get("frontend-developer-toml")["installation"][
        "installation_status"] == "uninstalled"
    operations = [row["operation"] for row in service.audits()]
    assert {"import", "validate", "publish", "install", "enable", "disable",
            "uninstall"} <= set(operations)


def test_legacy_safe_toml_is_deterministically_reviewed_on_validation(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    service.import_external(
        skill_id="legacy-frontend-toml", display_name="Legacy Frontend",
        description="legacy adapter record", source_type="codex_agent_toml",
        source_uri="local-allowlisted", content="safe legacy instructions",
        operator_id="operator", toml_source={
            "name": "Legacy Frontend", "description": "frontend guidance",
            "developer_instructions": "Review the supplied frontend task.",
        })
    with service.connect() as db:
        db.execute("""UPDATE formal_agent_skill_sources SET security_result_json=?
          WHERE skill_id=? AND tenant_id=? AND workspace_id=?""", (
            '{"integrity_verified":true,"publish_allowed":false,"scripts_reviewed":true,"source_known":true}',
            "legacy-frontend-toml", *service.scope.sql_parameters()))
    validated = service.transition_version(
        "legacy-frontend-toml", "0.1.0", "validate", "operator")
    assert validated["skill"]["version_status"] == "validated"
    security = json.loads(validated["source"]["security_result_json"])
    assert security["publish_allowed"] is True
    assert security["review_type"] == "deterministic_local_toml_adapter_v1"


def test_prepare_invocation_reports_precise_lifecycle_state_and_context(
        tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    with pytest.raises(SkillLifecycleError) as missing:
        service.prepare_invocation("missing-skill", {})
    assert missing.value.code == "skill_not_found"
    assert missing.value.required_action == "select_skill"

    service.import_external(
        skill_id="frontend-design", display_name="Frontend Design", description="design",
        source_type="github_skill_md", source_uri="https://github.com/anthropics/skills",
        content="---\nname: frontend-design\n---\ninstructions", operator_id="operator",
        repository_commit="abc", license_name="Apache-2.0")
    with pytest.raises(SkillLifecycleError) as draft:
        service.prepare_invocation("frontend-design", {})
    assert (draft.value.code, draft.value.lifecycle_status,
            draft.value.installation_status, draft.value.required_action) == (
                "skill_not_validated", "draft", "not_installed", "validate")

    service.transition_version("frontend-design", "0.1.0", "validate", "operator")
    with pytest.raises(SkillLifecycleError) as unpublished:
        service.prepare_invocation("frontend-design", {})
    assert unpublished.value.code == "skill_not_published"

    service.transition_version("frontend-design", "0.1.0", "publish", "operator")
    with pytest.raises(SkillLifecycleError) as uninstalled:
        service.prepare_invocation("frontend-design", {})
    assert uninstalled.value.code == "skill_not_installed"

    service.install_action("frontend-design", "install", "operator", "0.1.0")
    with pytest.raises(SkillLifecycleError) as installed:
        service.prepare_invocation("frontend-design", {})
    assert installed.value.code == "skill_not_enabled"

    service.install_action("frontend-design", "enable", "operator")
    service.create_version("frontend-design", version="0.2.0",
                           content="next draft", operator_id="operator",
                           change_summary="draft must not replace installed authority")
    assert service.prepare_invocation("frontend-design", {})["version"] == "0.1.0"
    with pytest.raises(SkillLifecycleError) as schema:
        service.prepare_invocation("frontend-design", {"task": 123})
    assert schema.value.code == "schema_validation_failed"
    assert schema.value.safe_detail["validation_code"]

    service.install_action("frontend-design", "disable", "operator")
    with pytest.raises(SkillLifecycleError) as disabled:
        service.prepare_invocation("frontend-design", {})
    assert disabled.value.code == "skill_disabled"
    assert disabled.value.version == "0.1.0"
    assert disabled.value.installation_status == "disabled"


def test_explicit_unknown_version_is_distinct_from_unknown_skill(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    with pytest.raises(SkillLifecycleError) as exc:
        service.install_action(
            "analyze-model-cost", "upgrade", "operator", "99.0.0")
    assert exc.value.code == "version_not_found"
    assert exc.value.version == "99.0.0"


def test_managed_runtime_generates_invocation_and_audit_ids(tmp_path: Path) -> None:
    database = tmp_path / "skills.db"
    lifecycle = FormalAgentSkillLifecycleService(database)
    runtime = FormalAgentSkillRuntime.build(
        bindings=_Bindings(), audit=FormalAgentSkillAuditService(database),
        registry=FormalAgentSkillRegistry(), lifecycle=lifecycle,
        managed_executor=lambda **_: {"status": "success", "request_count": 3,
                                      "network_called": False, "write_performed": False})
    result = runtime.invoke_managed_from_agent(
        skill_id="analyze-model-cost", arguments={"environment_id": "china_uat"},
        actor_id="operator")
    assert result["invocation_id"].startswith("FSI-")
    assert result["audit_id"].startswith("FAA-")
    assert lifecycle.invocations("analyze-model-cost")[0]["execution_result"] == "success"


def test_managed_executor_failure_is_redacted_audited_and_persisted(tmp_path: Path) -> None:
    database = tmp_path / "skills.db"
    lifecycle = FormalAgentSkillLifecycleService(database)
    marker = "sk-" + "must-not-be-persisted"

    def fail(**_kwargs):
        raise RuntimeError(f"provider failed token={marker}")

    audit = FormalAgentSkillAuditService(database)
    runtime = FormalAgentSkillRuntime.build(
        bindings=_Bindings(), audit=audit, registry=FormalAgentSkillRegistry(),
        lifecycle=lifecycle, managed_executor=fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        runtime.invoke_managed_from_agent(
            skill_id="analyze-model-cost",
            arguments={"environment_id": "china_uat"}, actor_id="operator")

    formal_row = audit.list()[0]
    invocation_row = lifecycle.invocations("analyze-model-cost")[0]
    assert formal_row["status"] == "blocked"
    assert invocation_row["execution_result"] == "failed"
    assert invocation_row["permission_decision"] == "allowed"
    assert marker not in str(formal_row)
    assert marker not in str(invocation_row)


def test_streaming_managed_runtime_persists_the_same_invocation_and_audit(tmp_path: Path) -> None:
    database = tmp_path / "skills.db"
    lifecycle = FormalAgentSkillLifecycleService(database)
    runtime = FormalAgentSkillRuntime.build(
        bindings=_Bindings(), audit=FormalAgentSkillAuditService(database),
        registry=FormalAgentSkillRegistry(), lifecycle=lifecycle,
        managed_executor=lambda **_: {})
    arguments = {"environment_id": "china_uat", "method": "POST",
                 "endpoint": "https://api-uat.weimeta.cn/v1/chat/completions",
                 "headers": [], "body": {"stream": True}, "model_id": "model-live"}
    context = runtime.begin_managed_invocation(
        skill_id="execute-uat-request", arguments=arguments, actor_id="operator")
    result = runtime.finish_managed_invocation(context, {
        "status": "success", "request_id": "REQ-LIVE",
        "decision_id": "DEC-LIVE", "network_called": True,
        "write_performed": False}, status="success")
    assert result["invocation_id"].startswith("FSI-")
    assert result["audit_id"].startswith("FAA-")
    persisted = lifecycle.invocations("execute-uat-request")[0]
    assert persisted["request_id"] == "REQ-LIVE"
    assert persisted["decision_id"] == "DEC-LIVE"
    assert persisted["network_called"] == 1


def test_writes_require_explicit_confirmation(tmp_path: Path) -> None:
    service = FormalAgentSkillLifecycleService(tmp_path / "skills.db")
    with pytest.raises(SkillLifecycleError, match="confirmation_required"):
        service.prepare_invocation("update-routing-policy", {
            "policy_id": "routing", "expected_version": "v1", "changes": {}, "reason": "test"})
