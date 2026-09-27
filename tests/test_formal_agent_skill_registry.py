from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formal_agent_skill_registry import FormalAgentSkillError, FormalAgentSkillRegistry


def test_eight_canonical_skills_are_discoverable_and_hash_pinned():
    registry = FormalAgentSkillRegistry()
    discovered = registry.discover()
    assert len(discovered) == 8
    assert all(row["read_only"] and row["binding"].endswith(".read") for row in discovered)
    for skill_id, row in registry.skills.items():
        artifact = ROOT / row["artifact"]
        assert artifact.is_file()
        assert (artifact.parent / "agents" / "openai.yaml").is_file()
        canonical = artifact.read_text(encoding="utf-8").replace("\r\n", "\n").encode()
        assert hashlib.sha256(canonical).hexdigest() == row["artifact_sha256"]
        assert f"name: {skill_id}" in artifact.read_text(encoding="utf-8")


def test_artifact_drift_and_path_escape_fail_closed(tmp_path):
    document = json.loads((ROOT / "config" / "formal_agent_skill_registry_v1.json").read_text())
    document["skills"][0]["artifact_sha256"] = "0" * 64
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FormalAgentSkillError, match="artifact_hash_mismatch"):
        FormalAgentSkillRegistry(path)
    document["skills"][0]["artifact"] = "../evidence/protected/SKILL.md"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FormalAgentSkillError, match="path_escape|protected_evidence"):
        FormalAgentSkillRegistry(path)


@pytest.mark.parametrize("extra", [
    {"actual_channel": "forged"}, {"bypass": True}, {"force_closed": True},
    {"kill_switch_override": True}, {"api_key": "secret"},
])
def test_security_sensitive_argument_tampering_is_rejected(extra):
    registry = FormalAgentSkillRegistry()
    with pytest.raises(FormalAgentSkillError, match="additional_property"):
        registry.validate_arguments("inspect-circuit-breaker", extra)
