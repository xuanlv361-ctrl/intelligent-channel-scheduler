"""Synchronize every Provider billing page through the authenticated operator Chrome.

Only response JSON bodies from the approved domestic-UAT billing API are read.
Request headers, cookies, browser storage and credentials are never inspected.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import websocket

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.domestic_uat_chrome_manager import DomesticUatChromeManager
from backend.realtime_log_sync_service import RealtimeLogSyncService
from backend.live_acceptance_service import LiveAcceptanceService
from backend.tenant_security import TenantScope

DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
OUT = ROOT / "evidence" / "provider_cost_sync"


class Runtime:
    def active(self, environment: str, key: str):
        return None


class Cdp:
    def __init__(self, url: str) -> None:
        self.socket = websocket.create_connection(url, timeout=30,
                                                  suppress_origin=True)
        self.next_id = 1
        self.events: list[dict] = []

    def close(self) -> None:
        self.socket.close()

    def command(self, method: str, params: dict | None = None) -> dict:
        message_id = self.next_id
        self.next_id += 1
        self.socket.send(json.dumps({"id": message_id, "method": method,
                                     "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == message_id:
                if message.get("error"):
                    raise RuntimeError(f"cdp_command_failed:{method}")
                return message.get("result") or {}
            self.events.append(message)

    def billing_response(self, expected_page: int | None = None) -> tuple[dict, str]:
        while True:
            event = (self.events.pop(0) if self.events else
                     json.loads(self.socket.recv()))
            if event.get("method") != "Network.responseReceived":
                continue
            params = event.get("params") or {}
            response = params.get("response") or {}
            parsed = urlsplit(str(response.get("url") or ""))
            if parsed.hostname != "uat.weimeta.cn" or parsed.path != "/api/log/self":
                continue
            page_values = parse_qs(parsed.query).get("p") or []
            if expected_page is not None and (
                    not page_values or int(page_values[0]) != expected_page):
                continue
            if int(response.get("status") or 0) != 200:
                raise RuntimeError(f"billing_http_{int(response.get('status') or 0)}")
            request_id = str(params["requestId"])
            result = self.command("Network.getResponseBody", {"requestId": request_id})
            return json.loads(str(result.get("body") or "{}")), parsed.query


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def main() -> None:
    manager = DomesticUatChromeManager()
    target = next(item for item in manager._request_json("/json/list") if
                  urlsplit(str(item.get("url") or "")).path ==
                  "/console/billing/logs")
    cdp = Cdp(str(target["webSocketDebuggerUrl"]))
    pages: list[dict] = []
    page_meta: list[dict] = []
    try:
        cdp.command("Network.enable")
        cdp.command("Page.reload", {"ignoreCache": True})
        payload, query = cdp.billing_response()
        current_page = int((parse_qs(query).get("p") or [1])[0])
        if current_page != 1:
            clicked = cdp.command("Runtime.evaluate", {"expression": """
              (() => { const n=document.querySelector('[aria-label="Page 1"]');
                if(!n) return false; n.click(); return true; })()
            """, "returnByValue": True})
            if not ((clicked.get("result") or {}).get("value")):
                raise RuntimeError("billing_page_one_control_missing")
            payload, query = cdp.billing_response(1)
        pages.append(payload)
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        page_size = int(data.get("page_size") or len(data.get("items") or []) or 10)
        total_pages = max(1, math.ceil(total / page_size))
        page_meta.append({"page": 1, "query": parse_qs(query),
                          "count": len(data.get("items") or []),
                          "payload_sha256": sha(payload)})
        time.sleep(0.5)
        for page in range(2, total_pages + 1):
            clicked = cdp.command("Runtime.evaluate", {"expression": f"""
              (() => {{ const direct=document.querySelector('[aria-label="Page {page}"]');
                if(direct) {{ direct.click(); return true; }}
                const n=document.querySelector('[aria-label="Next"]');
                if(!n || n.getAttribute('aria-disabled')==='true' ||
                   String(n.className).includes('disabled')) return false;
                n.click(); return true; }})()
            """, "returnByValue": True})
            if not ((clicked.get("result") or {}).get("value")):
                raise RuntimeError(f"billing_pagination_stopped_before_page_{page}")
            payload, query = cdp.billing_response(page)
            time.sleep(0.2)
            pages.append(payload)
            data = payload.get("data") or {}
            page_meta.append({"page": page, "query": parse_qs(query),
                              "count": len(data.get("items") or []),
                              "payload_sha256": sha(payload)})
    finally:
        cdp.close()

    now = datetime.now(timezone.utc)
    sync = RealtimeLogSyncService(DB, ROOT / "output" / "uat_executions.jsonl",
                                  Runtime())
    for prior in sync.list_jobs("china_uat"):
        if prior.get("state") in {"syncing", "synchronizing",
                                  "waiting_for_manual_login",
                                  "waiting_for_operator_confirmation"}:
            sync.stop(prior["sync_job_id"])
    job = sync.create_job("china_uat", (now - timedelta(days=6)).isoformat(),
                          (now + timedelta(minutes=1)).isoformat(),
                          "Asia/Shanghai", 5, 1000)
    job_id = str(job["sync_job_id"])
    sync.update_job(job_id, state="waiting_for_manual_login",
                    browser_context_active=1,
                    current_safe_url=job["safe_log_page_url"])
    sync.confirm_login(job_id, True)
    totals = {"observed": 0, "inserted": 0, "duplicate": 0,
              "correlated": 0, "ambiguous": 0}
    for payload in pages:
        result = sync.ingest(job_id, payload,
            "https://uat.weimeta.cn/api/log/self",
            billing_context={"quota_per_unit": "500000",
                             "quota_display_type": "CNY",
                             "usd_exchange_rate": "7.3"})
        for source, target_name in (("collected_count", "observed"),
                                    ("inserted_count", "inserted"),
                                    ("duplicate_count", "duplicate"),
                                    ("correlated_count", "correlated"),
                                    ("ambiguous_count", "ambiguous")):
            # The service returns job-level cumulative counters after every
            # page, not per-page deltas.
            totals[target_name] = max(totals[target_name],
                                      int(result.get(source) or 0))
    final = sync.finalize_batch(job_id, end_reason="provider_no_more_records")
    strategy_reconciliation = LiveAcceptanceService(
        DB, TenantScope.local_development()).reconcile_strategy_actual_costs()
    evidence = {
        "schema_version": "domestic_uat_billing_full_pagination_v1",
        "sync_batch_id": "PBS-" + uuid.uuid4().hex[:24].upper(),
        "sync_job_id": job_id, "captured_at": now.isoformat(),
        "environment_id": "china_uat", "source": "/api/log/self",
        "credentials_captured": False, "request_headers_captured": False,
        "page_count": len(pages), "provider_total": total,
        "page_size": page_size, "pages": page_meta, "totals": totals,
        "strategy_cost_reconciliation": strategy_reconciliation,
        "end_reason": "provider_no_more_records",
        "watermark": final.get("watermark_utc"),
        "source_cursor": final.get("source_cursor"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    target_path = OUT / f"{evidence['sync_batch_id']}.json"
    target_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2,
                                      sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "evidence_file": str(target_path.relative_to(ROOT)),
        "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
        "page_count": len(pages), "provider_total": total, **totals,
        "strategy_cost_updates": strategy_reconciliation["updated_count"],
        "end_reason": evidence["end_reason"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
