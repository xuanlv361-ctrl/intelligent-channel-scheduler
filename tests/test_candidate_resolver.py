import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_resolver import CandidateResolver  # noqa: E402


def test_mock_modes_use_only_mock_catalog():
    resolver = CandidateResolver()
    for mode in ("simulation", "mock_execute"):
        result = resolver.resolve(mode)
        assert result.raw_candidate_count == 8
        assert result.catalog_path == "data/mock_channel_catalog_v3.csv"
        assert {row["is_mock"] for row in result.candidates} == {"TRUE"}
        assert result.source_type == "simulation_parameter"


def test_real_shadow_reuses_real_adapter_and_keeps_real_only():
    result = CandidateResolver().resolve("real_shadow")
    assert len(result.candidates) == 5
    assert result.catalog_path == "data/channel_catalog_real_v1.csv"
    assert {row["is_mock"] for row in result.candidates} == {"FALSE"}
    assert all(row["shadow_eligible"] == "TRUE" for row in result.candidates)


def test_real_execute_has_no_routing_eligible_candidates():
    result = CandidateResolver().resolve("real_execute")
    assert result.raw_candidate_count == 5 and result.candidates == []


def test_resolution_is_stable_and_does_not_mutate_previous_result():
    resolver = CandidateResolver()
    first = resolver.resolve("simulation")
    before = copy.deepcopy(first.candidates)
    resolver.resolve("simulation")
    assert first.candidates == before


def test_catalog_metadata_is_recorded():
    result = CandidateResolver().resolve("real_shadow")
    assert result.catalog_version == "real-v1.0.0" and len(result.catalog_sha256) == 64
