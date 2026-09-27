import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from run_scheduler_overhead_benchmark import run  # noqa: E402


def test_overhead_benchmark_measures_both_real_local_runtime_shapes(tmp_path):
    result = run(tmp_path, 20)
    assert result["sample_count_per_variant"] == 20
    assert result["network_calls"] == 0
    assert result["provider_latency_included"] is False
    assert result["before"]["status"] == "ready"
    assert result["after"]["status"] == "ready"
    assert set(result["scheduler_total_comparison"]) == {"p50_ms", "p95_ms", "p99_ms", "max_ms"}
    persisted = json.loads((tmp_path / "scheduler_overhead_comparison.json").read_text(encoding="utf-8"))
    assert persisted["dataset"] == "fixed_local_simulation_requests_v1"
