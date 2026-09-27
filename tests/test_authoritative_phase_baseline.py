import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "config" / "authoritative_phase_baseline_v1.json"


def test_authoritative_baseline_is_versioned_and_complete():
    value = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert value["schema_version"] == "authoritative_phase_baseline_v1"
    assert value["source_bytes"] == 11731
    assert value["source_sha256"] == (
        "BD6176AD205EDDCE0165859DF666424521CBD43FED4DBA8415735F5B18EE1968"
    )
    rows = {row["id"]: row for row in value["workstreams"]}
    assert set(rows) == {
        "phase_0", "mode_2", "phase_2", "phase_3", "phase_4", "phase_5",
        "fgh", "multimodal", "exploration", "monitoring",
        "formal_agent_skill", "benchmark", "a0_a14", "five_role_acceptance",
    }
    assert rows["phase_0"]["starting_status"] == "implemented_and_verified"
    assert rows["phase_4"]["starting_status"] == "not_implemented"
    assert rows["fgh"]["requirement_ids"] == ["ADV-017", "ADV-018", "ADV-019"]
    assert rows["five_role_acceptance"]["starting_status"] == "pending"


def test_baseline_source_binding_is_recorded_in_human_document():
    value = json.loads(BASELINE.read_text(encoding="utf-8"))
    document = (ROOT / "docs" / "AUTHORITATIVE_PHASE_BASELINE_V1.md").read_text(
        encoding="utf-8"
    )
    assert value["source_sha256"] in document
    assert "A0-A14" in document
    assert "Five-role" in document
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest()
