import copy
import json

import pytest

from backend.platform_environments import (
    DEFAULT_CONFIG_PATH,
    EnvironmentConfigurationError,
    load_platform_environments,
    parse_platform_environments,
)


def raw_config():
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def test_default_registry_has_confirmed_china_and_pending_overseas():
    registry = load_platform_environments()
    china = registry.require("china_uat")
    overseas = registry.require("overseas")

    assert registry.config_version == "v1.0.0"
    assert china.models_url() == "https://api-uat.weimeta.cn/v1/models"
    assert china.chat_completions_url() == (
        "https://api-uat.weimeta.cn/v1/chat/completions"
    )
    assert china.configuration_complete is True
    assert china.real_execution_supported is True
    assert overseas.console_base_url == "https://weimeta.ai"
    assert overseas.api_base_url == "https://api.weimeta.ai"
    assert overseas.models_path == "/v1/models"
    assert overseas.chat_completions_path == "/v1/chat/completions"
    assert overseas.allowed_api_hosts == ("api.weimeta.ai",)
    assert overseas.api_protocol == "openai_compatible"
    assert overseas.logs_page_url is None
    assert overseas.currency is None
    assert overseas.configuration_status == "documented_pending_live_validation"
    assert overseas.models_endpoint_status == "pending_key_validation"
    assert overseas.completion_endpoint_status == "not_validated"
    assert overseas.real_execution_supported is False
    assert overseas.configuration_complete is False


def test_unknown_environment_is_not_silently_mapped_to_china():
    registry = load_platform_environments()
    assert registry.get("unknown") is None
    with pytest.raises(EnvironmentConfigurationError, match="unsupported environment_id"):
        registry.require("unknown")


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_base_url", "http://api.example.com"),
        ("api_base_url", "https://user:secret@api.example.com"),
        ("api_base_url", "https://localhost"),
        ("api_base_url", "https://127.0.0.1"),
        ("api_base_url", "file:///tmp/models"),
    ],
)
def test_api_url_requires_safe_https(field, value):
    raw = raw_config()
    env = raw["environments"]["overseas"]
    env[field] = value
    env["allowed_api_hosts"] = ["api.example.com"]
    with pytest.raises(EnvironmentConfigurationError):
        parse_platform_environments(raw)


def test_api_and_console_hosts_require_exact_allowlist_matches():
    raw = raw_config()
    raw["environments"]["china_uat"]["allowed_api_hosts"] = ["weimeta.cn"]
    with pytest.raises(EnvironmentConfigurationError, match="explicitly allowlisted"):
        parse_platform_environments(raw)


@pytest.mark.parametrize("url", [
    "https://fake-api.weimeta.ai",
    "https://api.weimeta.ai.attacker.com",
    "http://api.weimeta.ai",
])
def test_overseas_api_requires_https_and_exact_official_host(url):
    raw = raw_config()
    raw["environments"]["overseas"]["api_base_url"] = url
    with pytest.raises(EnvironmentConfigurationError):
        parse_platform_environments(raw)

    raw = raw_config()
    raw["environments"]["overseas"]["console_base_url"] = "https://sub.weimeta.ai"
    with pytest.raises(EnvironmentConfigurationError, match="explicitly allowlisted"):
        parse_platform_environments(raw)


@pytest.mark.parametrize(
    "path",
    [
        "v1/models",
        "//evil.example/models",
        "/v1/models?token=secret",
        "/v1/user@example",
        r"/v1\secret",
    ],
)
def test_endpoint_paths_reject_unsafe_or_credential_bearing_values(path):
    raw = raw_config()
    raw["environments"]["overseas"]["models_path"] = path
    with pytest.raises(EnvironmentConfigurationError, match="credential-free path"):
        parse_platform_environments(raw)


def test_pending_environment_cannot_enable_real_execution():
    raw = raw_config()
    raw["environments"]["overseas"]["real_execution_supported"] = True
    with pytest.raises(EnvironmentConfigurationError, match="pending configuration"):
        parse_platform_environments(raw)


def test_real_execution_requires_complete_confirmed_api_configuration():
    raw = raw_config()
    env = raw["environments"]["overseas"]
    env.update(
        configuration_status="confirmed",
        real_execution_supported=True,
        api_base_url="https://api.example.com",
        allowed_api_hosts=["api.example.com"],
        models_path="/v1/models",
        chat_completions_path=None,
    )
    with pytest.raises(EnvironmentConfigurationError, match="requires confirmed API"):
        parse_platform_environments(raw)


def test_parser_does_not_mutate_input_and_registry_mapping_is_read_only():
    raw = raw_config()
    before = copy.deepcopy(raw)
    registry = parse_platform_environments(raw)
    assert raw == before
    with pytest.raises(TypeError):
        registry.environments["new"] = registry.require("china_uat")
