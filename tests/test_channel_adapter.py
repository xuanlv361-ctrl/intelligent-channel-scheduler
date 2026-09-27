import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from channel_adapter import BlockedRealChannelAdapter, MockChannelAdapter, RealExecutionBlockedError  # noqa: E402
from request_router import RuntimeRequest  # noqa: E402


def test_mock_adapter_returns_explicit_non_network_receipt():
    receipt = MockChannelAdapter().dispatch(request_id="REQ001", candidate_id="MOCK-DS-FAST").to_dict()
    assert receipt == {
        "adapter_name": "mock_channel_adapter_v1",
        "status": "mock_accepted",
        "candidate_id": "MOCK-DS-FAST",
        "request_id": "REQ001",
        "is_mock": True,
        "real_api_called": False,
    }


def test_mock_adapter_rejects_missing_identifiers():
    with pytest.raises(ValueError):
        MockChannelAdapter().dispatch(request_id="", candidate_id="MOCK-DS-FAST")


def test_adapter_source_has_no_network_clients():
    source = (ROOT / "src" / "channel_adapter.py").read_text(encoding="utf-8")
    assert all(token not in source for token in ("requests", "urllib", "http.client", "socket", "aiohttp"))


@pytest.mark.parametrize("mode", ["success", "timeout", "rate_limited", "authentication_failed", "upstream_error", "invalid_response"])
def test_runtime_mock_adapter_failure_modes(mode):
    request = RuntimeRequest("R", "m", False, 3, 4, "CNY", "fastest_first", "mock_execute")
    result = MockChannelAdapter(mode).send(request, {"candidate_id": "M", "latency_ms": "12"})
    assert result["status"] == mode and result["adapter_type"] == "mock"
    assert result["is_mock"] is True and result["network_called"] is False


def test_blocked_real_adapter_always_raises():
    with pytest.raises(RealExecutionBlockedError):
        BlockedRealChannelAdapter().send(None, {})
