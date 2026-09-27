from __future__ import annotations
import json, time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

@dataclass
class PaginationLimits:
    maximum_pages:int=10
    maximum_records:int=500
    delay_ms:int=1500
    def __post_init__(self):
        if not 1<=self.maximum_pages<=100:raise ValueError("maximum_pages must be 1..100")
        if not 1<=self.maximum_records<=5000:raise ValueError("maximum_records must be 1..5000")
        if not 250<=self.delay_ms<=30000:raise ValueError("delay_ms must be 250..30000")

class PaginationController:
    def __init__(self,limits:PaginationLimits,checkpoint:Path|None=None):
        self.limits=limits;self.checkpoint=checkpoint;self.stop_event=Event();self.seen=set()
    def should_continue(self,page:int,records:int)->bool:
        return not self.stop_event.is_set() and page<self.limits.maximum_pages and records<self.limits.maximum_records
    def unique(self,record:dict)->bool:
        key=record.get("request_id") or record.get("response_id") or record.get("raw_record_sha256")
        if key in self.seen:return False
        self.seen.add(key);return True
    def wait(self): self.stop_event.wait(self.limits.delay_ms/1000)
    def stop(self):self.stop_event.set()
    def save_checkpoint(self,page:int,record:dict,successful_at:str):
        if not self.checkpoint:return
        payload={"last_collected_timestamp":record.get("logged_at"),"last_request_id":record.get("request_id"),
                 "last_page_number":page,"last_successful_collection_time":successful_at}
        self.checkpoint.parent.mkdir(parents=True,exist_ok=True)
        self.checkpoint.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    def load_checkpoint(self)->dict:
        if not self.checkpoint or not self.checkpoint.exists():return {}
        return json.loads(self.checkpoint.read_text(encoding="utf-8"))

