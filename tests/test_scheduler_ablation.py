import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import run_scheduler_ablation as ablation  # noqa: E402
from run_full_scheduler_benchmark import execute_variant, observation_digest  # noqa: E402
from scheduler import OFFLINE_COMPONENT_HOOKS  # noqa: E402


CONFIG = ROOT / "config" / "ablation_a0_a14_v1.json"


def definitions():
    return ablation.load_definitions(CONFIG)


def small_benchmark(benchmark):
    benchmark["dataset"].update({"request_count": 12, "seeds": [4101],
                                 "repetitions_per_seed": 1})
    return benchmark


def test_authoritative_mapping_is_one_to_one_and_safety_bypasses_are_bounded():
    definition, _ = definitions(); variants = definition["variants"]
    assert definition["definition_status"] == "authoritative_repository_baseline_v2"
    assert [row["id"] for row in variants] == [f"A{i}" for i in range(15)]
    assert {row["hook"] for row in variants[1:]} == set(OFFLINE_COMPONENT_HOOKS)
    assert [row["id"] for row in variants if row["safety_bypass"]] == ["A13", "A14"]
    assert definition["execution_boundary"]["network_allowed"] is False


def test_every_variant_changes_one_real_hook_and_exercises_it(tmp_path):
    definition, benchmark = definitions(); benchmark = small_benchmark(benchmark)
    for variant in definition["variants"]:
        rows, counts = execute_variant(
            benchmark, tmp_path / variant["id"], strategy="confidence_aware_v2",
            variant_id=variant["id"], disabled_hook=variant["hook"])
        expected = [] if variant["hook"] is None else [variant["hook"]]
        assert all(row["changed_hooks"] == expected for row in rows)
        if variant["hook"] is not None:
            assert counts[variant["hook"]] > 0
        assert not any(row["network_called"] for row in rows)


def test_a0_has_observation_parity_with_full_benchmark_baseline(tmp_path):
    _, benchmark = definitions(); benchmark = small_benchmark(benchmark)
    benchmark_rows, _ = execute_variant(
        benchmark, tmp_path / "benchmark", strategy="confidence_aware_v2", variant_id="A0")
    ablation_rows, _ = execute_variant(
        benchmark, tmp_path / "ablation", strategy="confidence_aware_v2", variant_id="A0")
    assert observation_digest(benchmark_rows) == observation_digest(ablation_rows)
    assert benchmark_rows == ablation_rows


def test_guard_bypasses_remain_offline_mock_only(tmp_path):
    definition, benchmark = definitions(); benchmark = small_benchmark(benchmark)
    for variant_id in ("A13", "A14"):
        variant = next(row for row in definition["variants"] if row["id"] == variant_id)
        rows, _ = execute_variant(
            benchmark, tmp_path / variant_id, strategy="confidence_aware_v2",
            variant_id=variant_id, disabled_hook=variant["hook"])
        assert all(row["changed_hooks"] == [variant["hook"]] for row in rows)
        assert not any(row["network_called"] for row in rows)


def test_unsafe_or_incomplete_mapping_is_rejected(tmp_path):
    definition = json.loads(CONFIG.read_text()); definition["variants"][1]["hook"] = "fallback"
    path = tmp_path / "bad.json"; path.write_text(json.dumps(definition))
    try:
        ablation.load_definitions(path)
    except ValueError as exc:
        assert "one-to-one" in str(exc)
    else:
        raise AssertionError("duplicate hook mapping accepted")


def test_five_role_packets_are_independent_unsigned_and_pending():
    root = ROOT / "docs" / "final_acceptance" / "five_role"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "PENDING_UNSIGNED"
    assert len(manifest["roles"]) == 5
    assert len({row["packet"] for row in manifest["roles"]}) == 5
    for role in manifest["roles"]:
        assert role["decision"] == "PENDING" and role["signature_reference"] is None
        text = (root / role["packet"]).read_text(encoding="utf-8")
        assert "PENDING / UNSIGNED" in text
        assert "Signature/approval reference: `<unsigned>`" in text
