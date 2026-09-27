import ast
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import build_measurement_payloads_v2 as builder  # noqa: E402


PROTECTED = [
    ROOT / "output" / "real_measurement_plan_v2.csv",
    ROOT / "data" / "real_channel_measurements_v1.csv",
    ROOT / "data" / "candidate_channels_v1.csv",
    ROOT / "config" / "strategy_catalog_v2.json",
    ROOT / "src" / "strategy_engine.py",
    ROOT / "src" / "scheduler.py",
]


def generated():
    return builder.generate()


def profile_map():
    return builder.load_profiles(builder.read_csv(builder.DEFAULT_PROFILES_PATH))


def test_profiles_are_fixed_and_operational():
    profiles = profile_map()
    assert profiles["P01"]["prompt_template"] == "用一句话说明水循环。"
    assert profiles["P01"]["expected_output_requirement"] == "一句话，不超过80个汉字"
    p02 = profiles["P02"]
    assert "固定材料开始：" in p02["prompt_template"]
    assert "固定材料结束。" in p02["prompt_template"]
    material = p02["prompt_template"].split("固定材料开始：", 1)[1].split("固定材料结束。", 1)[0]
    assert 400 <= len(material) <= 600
    assert p02["input_content_version"] == "medium-context-v1"
    assert "三条摘要" in p02["expected_output_requirement"]
    p03 = profiles["P03"]
    assert "600～800个汉字" in p03["prompt_template"]
    for required in ("标题", "引言", "4个", "结语"):
        assert required in p03["prompt_template"]
    assert profiles["P04"]["prompt_template"] == "分步骤解释冒泡排序，包括基本思想、过程、复杂度和适用场景。"
    assert "流式返回" not in profiles["P04"]["prompt_template"]
    assert profiles["P04"]["stream"] == "TRUE"
    assert "Prompt文字不能替代stream=true" in profiles["P04"]["execution_tool_requirement"]
    assert all(row["requested_temperature"] == "" for row in profiles.values())
    assert all(row["parameter_policy"] == "omit_if_unsupported" for row in profiles.values())


def test_60_payloads_match_plan_one_to_one():
    payloads, _ = generated()
    plans = builder.read_csv(builder.DEFAULT_PLAN_PATH)
    assert len(payloads) == len(plans) == 60
    assert [row["plan_id"] for row in payloads] == [row["plan_id"] for row in plans]
    assert len({row["plan_id"] for row in payloads}) == 60
    for payload, plan in zip(payloads, plans):
        for field in (
            "plan_id", "session_id", "channel_id", "channel_name",
            "request_profile_id", "requested_model",
        ):
            assert payload[field] == plan[field]


def test_stream_is_transport_field_not_inferred_from_prompt():
    payloads, _ = generated()
    assert all(row["stream"] is True for row in payloads if row["request_profile_id"] == "P04")
    assert all(row["stream"] is False for row in payloads if row["request_profile_id"] != "P04")
    profiles = profile_map()
    modified = dict(profiles["P01"])
    modified["prompt_template"] += " 文字中提到stream=true，但不得改变传输参数。"
    copied = dict(profiles)
    copied["P01"] = modified
    rebuilt = builder.build_payloads(builder.read_csv(builder.DEFAULT_PLAN_PATH), copied)
    assert all(row["stream"] is False for row in rebuilt if row["request_profile_id"] == "P01")


def test_every_channel_uses_identical_profile_content():
    payloads, _ = generated()
    contents = defaultdict(set)
    parameters = defaultdict(set)
    for row in payloads:
        profile = row["request_profile_id"]
        contents[profile].add(row["messages"][0]["content"])
        parameters[profile].add((row["stream"], row["max_tokens"], row.get("temperature")))
    assert all(len(values) == 1 for values in contents.values())
    assert all(len(values) == 1 for values in parameters.values())


def test_no_actual_results_secrets_or_temperature():
    payloads, readiness = generated()
    forbidden = builder.ACTUAL_FIELDS | {"latency_ms", "cost_cny", "ttft_ms"}
    assert all(not (forbidden & row.keys()) for row in payloads)
    assert all("temperature" not in row for row in payloads)
    assert all(row["execution_status"] == "not_executed" for row in payloads)
    assert all(row["source_type"] == "planned_uat" and row["is_mock"] is False for row in payloads)
    text = json.dumps([payloads, readiness], ensure_ascii=False).lower()
    assert not any(term in text for term in builder.SECRET_TERMS)
    assert readiness["real_api_calls_performed"] == 0
    assert readiness["ready_plan_count"] == 60


def test_readiness_uses_confirmed_manual_uat_capabilities_only():
    capabilities = builder.read_json(builder.DEFAULT_CAPABILITIES_PATH)
    readiness = builder.build_readiness(capabilities)
    assert readiness["overall_status"] == "ready"
    assert {row["readiness_status"] for row in readiness["profiles"]} == {"ready"}
    assert readiness["ready_profile_count"] == 4
    assert readiness["ready_plan_count"] == 60
    assert readiness["blocked_plan_count"] == 0
    assert readiness["scheduler_real_execution_authorized"] is False
    assert readiness["automatic_routing_authorized"] is False
    capability_names = {
        "supports_target_channel",
        "supports_custom_prompt", "supports_max_tokens", "supports_stream",
        "exposes_http_status", "exposes_request_id", "exposes_actual_model", "exposes_ttft",
    }
    for name in capability_names:
        item = capabilities["capabilities"][name]
        assert item["value"] is True
        assert item["confirmation_status"] == "confirmed"
        assert item["confirmation_source"] == "manual_uat_tool_verification"
        assert item["confirmed_by"] == "user"
        assert item["scope"] == "uat_manual_measurement_only"


def test_dry_run_writes_nothing(tmp_path):
    output = tmp_path / "payloads.json"
    readiness = tmp_path / "readiness.json"
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "build_measurement_payloads_v2.py"),
            "--dry-run", "--output", str(output), "--readiness-output", str(readiness),
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert not output.exists() and not readiness.exists()


def test_repeat_generation_is_byte_stable(tmp_path):
    output = tmp_path / "payloads.json"
    readiness = tmp_path / "readiness.json"
    command = [
        sys.executable, str(ROOT / "src" / "build_measurement_payloads_v2.py"),
        "--output", str(output), "--readiness-output", str(readiness),
    ]
    subprocess.run(command, check=True, capture_output=True)
    first = (output.read_bytes(), readiness.read_bytes())
    subprocess.run(command, check=True, capture_output=True)
    assert first == (output.read_bytes(), readiness.read_bytes())


def test_readiness_only_does_not_touch_payload(tmp_path):
    output = tmp_path / "protected-payload.json"
    output.write_text("unchanged", encoding="utf-8")
    readiness = tmp_path / "readiness.json"
    subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "build_measurement_payloads_v2.py"),
            "--readiness-only", "--output", str(output),
            "--readiness-output", str(readiness),
        ],
        check=True, capture_output=True,
    )
    assert output.read_text(encoding="utf-8") == "unchanged"
    assert json.loads(readiness.read_text(encoding="utf-8"))["ready_plan_count"] == 60


def test_plan_stream_mismatch_is_rejected():
    plans = builder.read_csv(builder.DEFAULT_PLAN_PATH)
    plans[0] = dict(plans[0], stream="TRUE")
    with pytest.raises(builder.PayloadValidationError, match="stream mismatch"):
        builder.build_payloads(plans, profile_map())


def test_source_has_no_network_imports():
    tree = ast.parse((ROOT / "src" / "build_measurement_payloads_v2.py").read_text(encoding="utf-8"))
    forbidden = {"requests", "httpx", "urllib", "aiohttp"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & forbidden)


def test_protected_files_are_not_modified_by_generation(tmp_path):
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "build_measurement_payloads_v2.py"),
            "--output", str(tmp_path / "payloads.json"),
            "--readiness-output", str(tmp_path / "readiness.json"),
        ],
        check=True, capture_output=True,
    )
    after = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    assert before == after
