from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IdentityConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OIDCConfiguration:
    issuer: str | None
    audience: str | None
    client_id: str | None
    jwks_uri: str | None
    allowed_algorithms: tuple[str, ...]
    clock_skew_seconds: int
    required_claims: tuple[str, ...]
    role_claim: str
    tenant_claim: str
    workspace_claim: str
    status: str

    @property
    def configured(self) -> bool:
        required = (self.issuer, self.audience, self.client_id, self.jwks_uri)
        return all(required) and self.status == "configured"


@dataclass(frozen=True, slots=True)
class EnterpriseIdentityConfig:
    config_version: str
    deployment_mode: str
    development_identity_enabled: bool
    default_development_tenant_id: str
    default_development_workspace_id: str
    development_roles: tuple[str, ...]
    oidc: OIDCConfiguration
    external_secret_manager_status: str

    @classmethod
    def load(cls, path: str | Path) -> "EnterpriseIdentityConfig":
        try:
            raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
            oidc = raw["oidc"]
            result = cls(
                config_version=str(raw["config_version"]),
                deployment_mode=str(raw["deployment_mode"]),
                development_identity_enabled=bool(raw["development_identity_enabled"]),
                default_development_tenant_id=str(raw["default_development_tenant_id"]),
                default_development_workspace_id=str(raw["default_development_workspace_id"]),
                development_roles=tuple(str(role) for role in raw["development_roles"]),
                oidc=OIDCConfiguration(
                    issuer=oidc.get("issuer"), audience=oidc.get("audience"),
                    client_id=oidc.get("client_id"), jwks_uri=oidc.get("jwks_uri"),
                    allowed_algorithms=tuple(oidc["allowed_algorithms"]),
                    clock_skew_seconds=int(oidc["clock_skew_seconds"]),
                    required_claims=tuple(oidc["required_claims"]),
                    role_claim=str(oidc["role_claim"]),
                    tenant_claim=str(oidc["tenant_claim"]),
                    workspace_claim=str(oidc["workspace_claim"]),
                    status=str(oidc["status"]),
                ),
                external_secret_manager_status=str(raw["external_secret_manager_status"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdentityConfigurationError("enterprise_identity_config_invalid") from exc
        if result.deployment_mode not in {"development", "lan", "production"}:
            raise IdentityConfigurationError("enterprise_deployment_mode_invalid")
        if result.deployment_mode == "production" and result.development_identity_enabled:
            raise IdentityConfigurationError("development_identity_forbidden_in_production")
        if not result.development_roles or any(
                not role or role != role.strip() for role in result.development_roles):
            raise IdentityConfigurationError("development_identity_roles_invalid")
        if result.oidc.clock_skew_seconds < 0 or not result.oidc.allowed_algorithms:
            raise IdentityConfigurationError("oidc_configuration_invalid")
        if any(algorithm.casefold() == "none" for algorithm in result.oidc.allowed_algorithms):
            raise IdentityConfigurationError("oidc_unsigned_algorithm_forbidden")
        return result
