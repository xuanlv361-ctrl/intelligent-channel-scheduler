"""Versioned, strictly validated platform-environment configuration.

This module only reads local configuration. It never probes configured URLs.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "platform_environments_v1.json"
)
KNOWN_STATUSES = frozenset({
    "confirmed", "not_validated", "pending_key_validation",
    "pending_confirmation", "documented_pending_live_validation",
    "read_only_validated", "execution_validation_pending",
    "execution_validated", "blocked", "validation_failed",
})


class EnvironmentConfigurationError(ValueError):
    """Raised when an environment registry is unsafe or structurally invalid."""


@dataclass(frozen=True)
class PlatformEnvironment:
    environment_id: str
    display_name: str
    console_base_url: str
    api_base_url: str | None
    models_path: str | None
    chat_completions_path: str | None
    logs_page_url: str | None
    allowed_api_hosts: tuple[str, ...]
    allowed_console_hosts: tuple[str, ...]
    currency: str | None
    configuration_status: str
    models_endpoint_status: str
    completion_endpoint_status: str
    log_page_status: str
    currency_status: str
    api_protocol: str | None
    documentation_source: str | None
    documentation_checked_at: str | None
    real_execution_supported: bool

    @property
    def configuration_complete(self) -> bool:
        return bool(
            self.configuration_status == "confirmed"
            and self.api_base_url
            and self.models_path
            and self.chat_completions_path
            and self.allowed_api_hosts
        )

    def models_url(self) -> str | None:
        return _join_endpoint(self.api_base_url, self.models_path)

    @property
    def model_discovery_configured(self) -> bool:
        return bool(self.api_base_url and self.models_path and self.allowed_api_hosts)

    def chat_completions_url(self) -> str | None:
        return _join_endpoint(self.api_base_url, self.chat_completions_path)


@dataclass(frozen=True)
class PlatformEnvironmentRegistry:
    config_version: str
    environments: Mapping[str, PlatformEnvironment]

    def get(self, environment_id: str) -> PlatformEnvironment | None:
        return self.environments.get(environment_id)

    def require(self, environment_id: str) -> PlatformEnvironment:
        environment = self.get(environment_id)
        if environment is None:
            raise EnvironmentConfigurationError(
                f"unsupported environment_id: {environment_id!r}"
            )
        return environment


def load_platform_environments(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> PlatformEnvironmentRegistry:
    """Load and validate a registry without making any network request."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EnvironmentConfigurationError(
            f"unable to read environment configuration: {config_path}"
        ) from exc
    return parse_platform_environments(raw)


def parse_platform_environments(raw: Any) -> PlatformEnvironmentRegistry:
    if not isinstance(raw, dict):
        raise EnvironmentConfigurationError("environment configuration must be an object")
    version = raw.get("config_version")
    if not isinstance(version, str) or not version.startswith("v"):
        raise EnvironmentConfigurationError("config_version must be a versioned string")
    entries = raw.get("environments")
    if not isinstance(entries, dict) or not entries:
        raise EnvironmentConfigurationError("environments must be a non-empty object")

    parsed: dict[str, PlatformEnvironment] = {}
    for environment_id, item in entries.items():
        _validate_identifier(environment_id)
        if not isinstance(item, dict):
            raise EnvironmentConfigurationError(
                f"{environment_id}: environment must be an object"
            )
        parsed[environment_id] = _parse_environment(environment_id, item)
    return PlatformEnvironmentRegistry(version, MappingProxyType(parsed))


def _parse_environment(
    environment_id: str, item: dict[str, Any]
) -> PlatformEnvironment:
    display_name = _required_string(item, "display_name", environment_id)
    console_url = _validated_url(
        item.get("console_base_url"), f"{environment_id}.console_base_url"
    )
    api_url = _optional_url(
        item.get("api_base_url"), f"{environment_id}.api_base_url"
    )
    logs_url = _optional_url(
        item.get("logs_page_url"), f"{environment_id}.logs_page_url"
    )
    api_hosts = _host_tuple(item.get("allowed_api_hosts"), environment_id, "api")
    console_hosts = _host_tuple(
        item.get("allowed_console_hosts"), environment_id, "console"
    )
    _require_url_host(console_url, console_hosts, f"{environment_id}.console_base_url")
    if api_url is not None:
        _require_url_host(api_url, api_hosts, f"{environment_id}.api_base_url")
    if logs_url is not None:
        _require_url_host(logs_url, console_hosts, f"{environment_id}.logs_page_url")

    models_path = _optional_endpoint_path(item.get("models_path"), environment_id, "models_path")
    completion_path = _optional_endpoint_path(
        item.get("chat_completions_path"), environment_id, "chat_completions_path"
    )
    status = item.get("configuration_status")
    if status not in KNOWN_STATUSES:
        raise EnvironmentConfigurationError(
            f"{environment_id}.configuration_status is invalid"
        )
    endpoint_statuses = {}
    for field in ("models_endpoint_status", "completion_endpoint_status",
                  "log_page_status", "currency_status"):
        value = item.get(field, "confirmed" if status == "confirmed" else "pending_confirmation")
        if value not in KNOWN_STATUSES:
            raise EnvironmentConfigurationError(f"{environment_id}.{field} is invalid")
        endpoint_statuses[field] = value
    api_protocol = item.get("api_protocol")
    if api_protocol not in (None, "openai_compatible"):
        raise EnvironmentConfigurationError(f"{environment_id}.api_protocol is invalid")
    documentation_source = _optional_url(
        item.get("documentation_source"), f"{environment_id}.documentation_source"
    )
    documentation_checked_at = item.get("documentation_checked_at")
    if documentation_checked_at is not None and not isinstance(documentation_checked_at, str):
        raise EnvironmentConfigurationError(
            f"{environment_id}.documentation_checked_at must be a string or null"
        )
    real_execution = item.get("real_execution_supported")
    if not isinstance(real_execution, bool):
        raise EnvironmentConfigurationError(
            f"{environment_id}.real_execution_supported must be boolean"
        )
    currency = item.get("currency")
    if currency is not None and (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or currency.upper() != currency
    ):
        raise EnvironmentConfigurationError(
            f"{environment_id}.currency must be an uppercase ISO-style code or null"
        )
    if status != "confirmed" and real_execution:
        raise EnvironmentConfigurationError(
            f"{environment_id}: pending configuration cannot enable real execution"
        )
    if real_execution and not all((api_url, models_path, completion_path, api_hosts)):
        raise EnvironmentConfigurationError(
            f"{environment_id}: real execution requires confirmed API endpoints and hosts"
        )
    return PlatformEnvironment(
        environment_id=environment_id,
        display_name=display_name,
        console_base_url=console_url,
        api_base_url=api_url,
        models_path=models_path,
        chat_completions_path=completion_path,
        logs_page_url=logs_url,
        allowed_api_hosts=api_hosts,
        allowed_console_hosts=console_hosts,
        currency=currency,
        configuration_status=status,
        models_endpoint_status=endpoint_statuses["models_endpoint_status"],
        completion_endpoint_status=endpoint_statuses["completion_endpoint_status"],
        log_page_status=endpoint_statuses["log_page_status"],
        currency_status=endpoint_statuses["currency_status"],
        api_protocol=api_protocol,
        documentation_source=documentation_source,
        documentation_checked_at=documentation_checked_at,
        real_execution_supported=real_execution,
    )


def _validate_identifier(value: Any) -> None:
    if not isinstance(value, str) or not value or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value
    ):
        raise EnvironmentConfigurationError(f"invalid environment_id: {value!r}")


def _required_string(item: dict[str, Any], field: str, environment_id: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentConfigurationError(f"{environment_id}.{field} must be non-empty")
    return value.strip()


def _optional_url(value: Any, field: str) -> str | None:
    return None if value is None else _validated_url(value, field)


def _validated_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvironmentConfigurationError(f"{field} must be a non-empty HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EnvironmentConfigurationError(
            f"{field} must be HTTPS without credentials, query, or fragment"
        )
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise EnvironmentConfigurationError(f"{field} cannot use localhost")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise EnvironmentConfigurationError(f"{field} cannot use a private network address")
    return value.rstrip("/")


def _host_tuple(value: Any, environment_id: str, kind: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(host, str) or not host or host != host.lower()
        for host in value
    ):
        raise EnvironmentConfigurationError(
            f"{environment_id}.allowed_{kind}_hosts must be a lowercase host list"
        )
    result = tuple(dict.fromkeys(value))
    for host in result:
        parsed = urlsplit(f"https://{host}")
        if parsed.hostname != host or parsed.port is not None or parsed.path:
            raise EnvironmentConfigurationError(
                f"{environment_id}.allowed_{kind}_hosts contains an invalid host"
            )
    return result


def _require_url_host(url: str, hosts: tuple[str, ...], field: str) -> None:
    hostname = urlsplit(url).hostname
    if hostname not in hosts:
        raise EnvironmentConfigurationError(
            f"{field} host is not explicitly allowlisted"
        )


def _optional_endpoint_path(value: Any, environment_id: str, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "?" in value
        or "#" in value
        or "@" in value
        or "\\" in value
    ):
        raise EnvironmentConfigurationError(
            f"{environment_id}.{field} must be an absolute credential-free path or null"
        )
    return value


def _join_endpoint(base_url: str | None, path: str | None) -> str | None:
    if base_url is None or path is None:
        return None
    return f"{base_url.rstrip('/')}{path}"
