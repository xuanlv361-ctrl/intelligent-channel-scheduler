"""Public, sanitized error contract for managed Agent Skill operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ErrorDefinition:
    status: int
    message: str
    required_action: str | None = None


ERRORS: dict[str, ErrorDefinition] = {
    "skill_not_found": ErrorDefinition(404, "未找到指定的 Skill。", "select_skill"),
    "skill_not_validated": ErrorDefinition(409, "当前 Skill 仍为草稿，请先完成安全检查和 Schema 校验。", "validate"),
    "skill_external_security_review_incomplete": ErrorDefinition(409, "当前 Skill 的安全检查尚未完成。", "security_review"),
    "skill_not_published": ErrorDefinition(409, "当前版本尚未发布，请先在“发布与安装”中发布。", "publish"),
    "skill_not_installed": ErrorDefinition(409, "当前 Skill 尚未安装，请先安装已发布版本。", "install"),
    "skill_not_enabled": ErrorDefinition(409, "当前 Skill 尚未启用。", "enable"),
    "skill_disabled": ErrorDefinition(409, "当前 Skill 已停用，请重新启用后调用。", "enable"),
    "version_not_found": ErrorDefinition(404, "未找到指定的 Skill 版本。", "select_version"),
    "skill_version_not_found": ErrorDefinition(404, "未找到指定的 Skill 版本。", "select_version"),
    "installation_not_found": ErrorDefinition(404, "未找到指定的 Skill 安装记录。", "install"),
    "skill_installation_not_found": ErrorDefinition(404, "未找到指定的 Skill 安装记录。", "install"),
    "permission_denied": ErrorDefinition(403, "当前用户没有执行此操作所需的权限。", "request_permission"),
    "principal_role_not_authorized": ErrorDefinition(403, "当前用户没有执行此操作所需的权限。", "request_permission"),
    "confirmation_required": ErrorDefinition(409, "该操作需要用户明确确认。", "confirm"),
    "skill_confirmation_required": ErrorDefinition(409, "该操作需要用户明确确认。", "confirm"),
    "approval_required": ErrorDefinition(409, "该操作需要有效审批。", "provide_approval"),
    "invalid_input": ErrorDefinition(422, "输入参数不合法，请检查表单内容。", "correct_input"),
    "schema_validation_failed": ErrorDefinition(422, "输入内容不符合当前 Skill 的 Schema。", "correct_input"),
    "instruction_skill_task_required": ErrorDefinition(422, "请输入需要 Skill 完成的真实任务。", "provide_task"),
    "execution_engine_not_configured": ErrorDefinition(409, "尚未配置可执行 instruction-only Skill 的模型引擎。", "configure_execution_engine"),
    "instruction_skill_empty_result": ErrorDefinition(502, "执行引擎未返回可展示的业务结果。", "review_details"),
    "instruction_skill_upstream_failed": ErrorDefinition(502, "国内 UAT 模型执行失败，请查看本次请求的状态与错误分类。", "review_details"),
    "request_id_or_error_id_required": ErrorDefinition(422, "请至少提供 Request ID 或错误 ID。", "correct_input"),
    "host_not_configured": ErrorDefinition(409, "尚未配置真实外部 Agent Host。", "configure_host"),
    "host_input_invalid": ErrorDefinition(422, "Host 配置不完整或格式不正确。", "correct_host"),
    "host_url_invalid": ErrorDefinition(422, "Host 地址格式不正确，仅支持经过批准的 HTTPS 地址。", "correct_host"),
    "host_url_not_allowed": ErrorDefinition(403, "Host 地址不在允许列表中。", "request_host_allowlist"),
    "host_dns_resolution_failed": ErrorDefinition(502, "Host 域名无法解析。", "check_dns"),
    "host_address_not_allowed": ErrorDefinition(403, "Host 地址解析到了不允许访问的网络。", "correct_host"),
    "host_auth_type_invalid": ErrorDefinition(422, "Host 鉴权类型不受支持。", "correct_auth"),
    "host_not_connected": ErrorDefinition(409, "Host 尚未通过连接测试。", "test_connection"),
    "host_not_enabled": ErrorDefinition(409, "当前外部 Agent Host 尚未启用。", "enable_host"),
    "host_authentication_failed": ErrorDefinition(401, "外部 Host 鉴权失败，请更新凭据后重新测试。", "rotate_credential"),
    "host_connection_failed": ErrorDefinition(502, "无法连接外部 Host，请检查地址、网络和 TLS 配置。", "test_connection"),
    "host_protocol_incompatible": ErrorDefinition(409, "外部 Host 的协议或响应结构不兼容。", "update_protocol"),
    "invalid_host_response": ErrorDefinition(502, "外部 Host 返回的数据结构无效。", "review_details"),
    "invocation_cancelled": ErrorDefinition(409, "外部 Host 调用已取消。", "retry"),
    "invocation_timeout": ErrorDefinition(504, "Skill 调用超时。", "retry"),
    "invocation_failed": ErrorDefinition(502, "Skill 调用失败，请查看技术详情和审计记录。", "review_details"),
}


def canonical_code(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or str(exc)).strip() or "invocation_failed"
    aliases = {
        "skill_enable_transition_invalid": "skill_not_installed",
        "skill_install_transition_invalid": "skill_not_published",
        "skill_disable_transition_invalid": "skill_not_enabled",
        "skill_uninstall_transition_invalid": "skill_not_installed",
        "skill_install_version_transition_invalid": "skill_not_published",
        "formal_skill_schema_invalid": "schema_validation_failed",
        "formal_skill_input_invalid": "schema_validation_failed",
        "formal_skill_permission_denied": "permission_denied",
    }
    return aliases.get(code, code if code in ERRORS else "invocation_failed")


def _safe_context(exc: Exception) -> dict[str, Any]:
    context: dict[str, Any] = {}
    getter = getattr(exc, "context", None)
    if callable(getter):
        candidate = getter()
        if isinstance(candidate, Mapping):
            context.update(candidate)
    candidate = getattr(exc, "safe_context", None)
    if isinstance(candidate, Mapping):
        context.update(candidate)
    for name in ("skill_id", "version", "lifecycle_status", "installation_status", "required_action", "safe_detail"):
        value = getattr(exc, name, None)
        if value is not None:
            context[name] = value
    return context


def build_error_detail(
    exc: Exception, *, skill_id: str | None = None, version: str | None = None,
    lifecycle_status: str | None = None, installation_status: str | None = None,
    safe_detail: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    context = _safe_context(exc)
    code = canonical_code(exc)
    definition = ERRORS[code]
    detail: dict[str, Any] = {
        "code": code,
        "message": definition.message,
        "skill_id": context.get("skill_id") or skill_id,
        "version": context.get("version") or version,
        "lifecycle_status": context.get("lifecycle_status") or lifecycle_status,
        "installation_status": context.get("installation_status") or installation_status,
        "required_action": context.get("required_action") or definition.required_action,
        "trace_id": f"SKERR-{uuid.uuid4()}",
    }
    merged = dict(context.get("safe_detail") or {})
    merged.update(dict(safe_detail or {}))
    for key in ("invocation_id", "audit_id", "host_id"):
        if context.get(key):
            merged[key] = context[key]
    if merged:
        sensitive = {"authorization", "cookie", "token", "api_key", "secret", "credential", "password"}
        detail["details"] = {str(key): value for key, value in merged.items() if str(key).lower() not in sensitive}
    return definition.status, detail
