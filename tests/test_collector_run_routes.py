from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.browser_import_service import BrowserImportService
from backend.collector_run_routes import build_collector_run_router
from backend.collector_run_service import CollectorRunService
from src.services.console_service import Store


class FakeSupervisor:
    def __init__(self): self.launched=[]; self.stopped=[]; self.alive=set()
    def launch(self, run_id):
        self.launched.append(run_id); self.alive.add(run_id); return 9876
    def is_alive(self, run_id): return run_id in self.alive
    def stop(self, run_id):
        self.stopped.append(run_id); self.alive.discard(run_id); return True


def client_for(tmp_path):
    path=tmp_path/"routes.sqlite3"
    runs=CollectorRunService(path); supervisor=FakeSupervisor()
    imports=BrowserImportService(path,Store(path))
    app=FastAPI()
    app.include_router(build_collector_run_router(
        runs,supervisor,imports,lambda _request:None,lambda _request:"session-hash"))
    return TestClient(app),runs,supervisor


def start(client, **updates):
    body={"environment_id":"china_uat","date_from":"2026-07-28","date_to":"2026-07-28",
          "maximum_pages":1,"maximum_records":20,"page_delay_ms":1500,
          "operator_confirmation":True}
    body.update(updates)
    return client.post("/api/v1/collector/runs/start",json=body)


def test_start_endpoint_launches_one_worker_and_rejects_browser_commands(tmp_path):
    client,runs,supervisor=client_for(tmp_path)
    response=start(client)
    assert response.status_code==200
    assert len(supervisor.launched)==1
    run=runs.get(response.json()["run_id"],include_private=True)
    assert run["status"]=="launching_browser" and run["worker_pid"]==9876
    assert start(client,command="calc.exe").status_code==422


def test_all_and_arbitrary_urls_are_rejected(tmp_path):
    client,_,_=client_for(tmp_path)
    assert start(client,environment_id="all").status_code==409
    assert start(client,console_url="https://evil.example").status_code==422


def test_login_confirmation_requires_exact_visible_log_page(tmp_path):
    client,runs,_=client_for(tmp_path)
    run_id=start(client).json()["run_id"]
    runs.transition(run_id,"awaiting_manual_login",last_heartbeat_at="2026-07-28T00:00:00+00:00")
    body={"environment_id":"china_uat","explicit_confirmation":True}
    assert client.post(f"/api/v1/collector/runs/{run_id}/confirm-login",json=body).json()["detail"]["code"]=="collector_log_page_not_visible"
    runs.update(run_id,current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    assert client.post(f"/api/v1/collector/runs/{run_id}/confirm-login",json=body).json()["status"]=="login_confirmed"


def test_stop_is_idempotent_and_only_stops_selected_run(tmp_path):
    client,_,supervisor=client_for(tmp_path)
    run_id=start(client).json()["run_id"]
    body={"environment_id":"china_uat","explicit_confirmation":True}
    first=client.post(f"/api/v1/collector/runs/{run_id}/stop",json=body)
    second=client.post(f"/api/v1/collector/runs/{run_id}/stop",json=body)
    assert first.json()["status"]=="stopped"
    assert second.json()["status"]=="stopped"
    assert supervisor.stopped==[run_id,run_id]
