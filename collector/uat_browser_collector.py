from __future__ import annotations
import json, signal, uuid
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .evidence_writer import EvidenceWriter
from .log_page_parser import in_date_range,parse_dom_table,parse_structured
from .network_response_capture import capture_json_response,require_allowed_url,safe_source_url
from .pagination_controller import PaginationController,PaginationLimits

@dataclass
class CollectorConfig:
    console_url:str
    log_pages:list[str]
    date_from:str|None=None
    date_to:str|None=None
    limits:PaginationLimits|None=None
    evidence_root:Path=Path("evidence/uat_browser_collector")
    checkpoint:Path=Path("data/uat_browser_collector_checkpoint.json")
    column_mapping:dict[str,str]|None=None
    local_import_url:str="http://127.0.0.1:8000/api/v1/collector/import/preview"
    environment_id:str="china_uat"
    source_type:str|None=None
    def __post_init__(self):
        allowed={"weimeta.ai"} if self.environment_id=="overseas" else {"uat.weimeta.cn","admin-uat.weimeta.cn"}
        expected_console="weimeta.ai" if self.environment_id=="overseas" else "uat.weimeta.cn"
        expected_source="measured_overseas_browser_collector" if self.environment_id=="overseas" else "measured_uat_browser_collector"
        if (urlsplit(self.console_url).hostname or "").lower()!=expected_console:
            raise ValueError("collector_environment_mismatch")
        if self.source_type not in (None,expected_source):
            raise ValueError("collector_source_type_mismatch")
        self.source_type=expected_source
        for url in [self.console_url,*self.log_pages]:
            parsed=urlsplit(url)
            if parsed.scheme!="https" or (parsed.hostname or "").lower() not in allowed:
                raise ValueError("collector_source_host_not_allowed")
        self.limits=self.limits or PaginationLimits()
        self.column_mapping=self.column_mapping or {}

class UatBrowserCollector:
    """Visible, non-persistent browser collector. Login is always performed by the operator."""
    def __init__(self,config:CollectorConfig):
        self.config=config;self.collection_id=f"COL-{uuid.uuid4().hex[:16].upper()}"
        self.writer=EvidenceWriter(config.evidence_root)
        self.pager=PaginationController(config.limits,config.checkpoint)
    def collect(self)->dict[str,Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Install Python Playwright and Chromium before running the collector.") from exc
        captured=[];rejected=duplicates=0;structure_recognized=False
        allowed_hosts={"weimeta.ai"} if self.config.environment_id=="overseas" else {"uat.weimeta.cn","admin-uat.weimeta.cn"}
        def require_context_url(url:str)->None:
            parsed=urlsplit(url)
            if parsed.scheme!="https" or (parsed.hostname or "").lower() not in allowed_hosts:
                raise ValueError("collector_source_host_not_allowed")
        signal.signal(signal.SIGINT,lambda *_:self.pager.stop())
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=False)
            context=browser.new_context()  # deliberately non-persistent; no storage_state is loaded or saved
            page=context.new_page()
            def response_seen(response):
                nonlocal rejected,duplicates,structure_recognized
                try:
                    require_context_url(response.url)
                    captured_response=capture_json_response(response.url,response.headers.get("content-type",""),response.body())
                    if not captured_response:return
                    now=datetime.now(timezone.utc).isoformat()
                    records=parse_structured(captured_response.payload,captured_response.source_url,now)
                    if records or isinstance(captured_response.payload,dict) and any(
                        key in captured_response.payload for key in ("data","items","records","list")):
                        structure_recognized=True
                    self.writer.write(self.collection_id,response.url,"structured_log_response",
                                      captured_response.content_type,captured_response.payload,len(records))
                    for record in records:
                        if not in_date_range(record,self.config.date_from,self.config.date_to):rejected+=1;continue
                        if not self.pager.unique(record):duplicates+=1;continue
                        captured.append(record)
                except Exception:
                    # Non-allowlisted, non-JSON, oversized and malformed responses are ignored.
                    return
            page.on("response",response_seen)
            page.goto(self.config.console_url)
            input("请在可见浏览器中手动完成 UAT 登录。确认正确的 UAT 账户和页面可见后按 Enter；不要向本工具输入密码：")
            require_context_url(page.url)
            if input("输入 UAT 确认继续只读采集（其他输入将取消）：").strip()!="UAT":
                context.close();browser.close();return {"status":"cancelled","collection_id":self.collection_id}
            for page_number,url in enumerate(self.config.log_pages,1):
                if not self.pager.should_continue(page_number-1,len(captured)):break
                page.goto(url);page.wait_for_load_state("domcontentloaded")
                before=len(captured)
                if before==len(captured):
                    html=page.content();now=datetime.now(timezone.utc).isoformat()
                    dom_records=parse_dom_table(html,self.config.column_mapping,safe_source_url(page.url),now)
                    if "<table" in html.casefold():structure_recognized=True
                    self.writer.write(self.collection_id,page.url,"visible_dom_table","text/html",
                                      {"table_records":dom_records},len(dom_records))
                    for record in dom_records:
                        if len(captured)>=self.config.limits.maximum_records:break
                        if not in_date_range(record,self.config.date_from,self.config.date_to):rejected+=1;continue
                        if not self.pager.unique(record):duplicates+=1;continue
                        captured.append(record)
                if captured:self.pager.save_checkpoint(page_number,captured[-1],datetime.now(timezone.utc).isoformat())
                self.pager.wait()
            context.close();browser.close()
        return {"status":"captured","collection_id":self.collection_id,"source_type":self.config.source_type,
                "environment":self.config.environment_id,"environment_id":self.config.environment_id,
                "structure_recognized":structure_recognized,
                "records_observed":bool(captured),"records":captured[:self.config.limits.maximum_records],
                "captured_count":min(len(captured),self.config.limits.maximum_records),
                "rejected_count":rejected,"duplicate_count":duplicates,
                "cookies_stored":0,"credentials_stored":0}
