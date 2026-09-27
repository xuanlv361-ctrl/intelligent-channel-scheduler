import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.price_catalog_service import PriceCatalogService
from backend.price_sync_supervisor import PriceSyncSupervisor


POLICY = ROOT / "config" / "price_sync_policy_v1.json"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def record(price="1"):
    return {"environment_id": "china_uat", "model_id": "model-a",
            "channel_id": "1", "currency": "CNY",
            "input_unit_price": price, "output_unit_price": "2",
            "billing_unit": "per_1m_tokens",
            "effective_from": "2026-07-01T00:00:00Z",
            "effective_until": None}


def build(tmp_path, clock, responses):
    calls = []
    def transport(_source, _timeout):
        calls.append(clock.value)
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return {"payload": {"schema_version": "price_catalog_v1",
                            "records": [value]}, "network_called": False}
    service = PriceCatalogService(tmp_path / "prices.sqlite3", POLICY,
                                  transport=transport, clock=clock,
                                  development_mode=True)
    return service, PriceSyncSupervisor(service, clock=clock), calls


def test_due_run_is_bounded_and_persists_restart_state(tmp_path):
    clock = Clock()
    service, supervisor, calls = build(tmp_path, clock, [record()])
    supervisor.configure("contract_mock", enabled=True)
    first = supervisor.run_due("contract_mock")
    assert first["status"] == "ready" and len(calls) == 1
    assert supervisor.run_due("contract_mock")["status"] == "not_due_or_unavailable"
    assert len(calls) == 1
    restarted = PriceSyncSupervisor(service, clock=clock)
    assert restarted.status("contract_mock")["last_catalog_version"] == first["catalog_version"]
    clock.value += timedelta(days=1)
    restarted.run_due("contract_mock")
    assert len(calls) == 2


def test_failure_uses_persistent_bounded_backoff(tmp_path):
    clock = Clock()
    service, supervisor, calls = build(tmp_path, clock, [TimeoutError("secret")])
    supervisor.configure("contract_mock", enabled=True)
    failed = supervisor.run_due("contract_mock")
    assert failed["status"] == "failed" and len(calls) == 1
    assert supervisor.status("contract_mock")["state"] == "backoff"
    assert supervisor.run_due("contract_mock")["status"] == "not_due_or_unavailable"
    assert len(calls) == 1


def test_changed_price_emits_safe_discrepancy_count(tmp_path):
    clock = Clock()
    _service, supervisor, calls = build(tmp_path, clock, [record("1"), record("3")])
    supervisor.configure("contract_mock", enabled=True)
    supervisor.run_due("contract_mock")
    clock.value += timedelta(days=1)
    changed = supervisor.run_due("contract_mock")
    assert len(calls) == 2
    assert changed["change_alert_count"] == 1
    assert supervisor.status("contract_mock")["last_change_alert_count"] == 1


def test_disabled_schedule_never_calls_transport(tmp_path):
    clock = Clock()
    _service, supervisor, calls = build(tmp_path, clock, [record()])
    supervisor.configure("contract_mock", enabled=False)
    assert supervisor.run_due("contract_mock", force=True)["network_called"] is False
    assert calls == []


def test_background_tick_runs_only_due_enabled_sources_once(tmp_path):
    clock = Clock()
    _service, supervisor, calls = build(tmp_path, clock, [record()])
    supervisor.configure("contract_mock", enabled=True)
    tick = supervisor.run_pending()
    assert tick["due_count"] == 1
    assert tick["attempted_count"] == 1
    assert tick["network_called"] is False
    assert len(calls) == 1
    second = supervisor.run_pending()
    assert second["due_count"] == 0 and len(calls) == 1
