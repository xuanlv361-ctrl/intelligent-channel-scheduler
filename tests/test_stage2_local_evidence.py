import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_stage2_local_evidence import build  # noqa: E402


def test_stage2_evidence_builder_executes_all_contracts_without_network(tmp_path):
    output = tmp_path / "evidence"
    manifest = build(output)
    assert manifest["status"] == "passed"
    assert manifest["network_calls"] == 0
    assert manifest["completion_calls"] == 0

    fixed = json.loads((output / "fixed_scenario_evidence.json").read_text(encoding="utf-8"))
    reconstruction = json.loads((output / "decision_reconstruction_evidence.json").read_text(encoding="utf-8"))
    errors = json.loads((output / "error_classification_evidence.json").read_text(encoding="utf-8"))
    assert (fixed["passed"], fixed["total"]) == (13, 13)
    assert (reconstruction["passed"], reconstruction["total"]) == (4, 4)
    assert {row["case"] for row in reconstruction["cases"]} == {
        "success", "filtered", "fallback", "all_unavailable"
    }
    assert (errors["passed"], errors["total"]) == (13, 13)
    assert all(row["assertion_passed"] for row in fixed["scenarios"])


def test_stage2_evidence_contains_no_common_credential_material(tmp_path):
    output = tmp_path / "evidence"
    build(output)
    rendered = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in output.rglob("*") if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}
    ).casefold()
    assert "authorization:" not in rendered
    assert "bearer " not in rendered
    assert "api_key" not in rendered
    assert "cookie:" not in rendered
