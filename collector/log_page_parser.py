from __future__ import annotations
import hashlib, json, re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from . import SOURCE_TYPE

FIELDS = ("logged_at","request_id","response_id","api_key_name","requested_model","actual_model",
          "channel_id","channel_name","stream","http_status","latency_ms","ttft_ms","input_tokens",
          "output_tokens","total_tokens","cost_cny","finish_reason","error_message")
ALIASES = {
 "logged_at":("logged_at","timestamp","created_at","time"), "request_id":("request_id","requestId"),
 "response_id":("response_id","responseId"), "api_key_name":("api_key_name","key_name"),
 "requested_model":("requested_model","request_model"), "actual_model":("actual_model","response_model"),
 "channel_id":("channel_id","channelId"), "channel_name":("channel_name","channelName"),
 "stream":("stream","is_stream"), "http_status":("http_status","status_code"),
 "latency_ms":("latency_ms","duration_ms"), "ttft_ms":("ttft_ms","first_token_ms"),
 "input_tokens":("input_tokens","prompt_tokens"), "output_tokens":("output_tokens","completion_tokens"),
 "total_tokens":("total_tokens",), "cost_cny":("cost_cny","cost"), "finish_reason":("finish_reason",),
 "error_message":("error_message","error")
}
SECRET_KEYS=re.compile(r"(authorization|cookie|password|secret|token_value|api_?key$|session|signed_?url|signature)",re.I)
BEARER=re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
KEYLIKE=re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{8,}\b",re.I)
IP=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k:("[REDACTED]" if SECRET_KEYS.search(str(k)) else redact(v)) for k,v in value.items()
                if str(k).lower() not in {"prompt","messages","response_body","request_body","content"}}
    if isinstance(value, list): return [redact(x) for x in value]
    if isinstance(value, str): return IP.sub("[REDACTED_IP]",KEYLIKE.sub("[REDACTED_KEY]",BEARER.sub("Bearer [REDACTED]",value)))
    return value

def _records(payload: Any) -> list[dict[str,Any]]:
    if isinstance(payload,list): return [x for x in payload if isinstance(x,dict)]
    if isinstance(payload,dict):
        for key in ("items","records","rows","list","data"):
            value=payload.get(key)
            if isinstance(value,list): return [x for x in value if isinstance(x,dict)]
            if isinstance(value,dict):
                nested=_records(value)
                if nested:return nested
    return []

def normalize_record(record: dict[str,Any], source_url: str, collected_at: str) -> dict[str,Any]:
    safe=redact(record)
    normalized: dict[str,Any]={}
    for field in FIELDS:
        normalized[field]=next((safe.get(alias) for alias in ALIASES[field] if safe.get(alias) not in ("",None)),None)
    canonical=json.dumps(safe,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    normalized.update({"source_url":source_url,"collected_at":collected_at,
      "raw_record_sha256":hashlib.sha256(canonical.encode()).hexdigest(),"source_type":SOURCE_TYPE,
      "environment":"uat","is_mock":False,"timestamp":normalized["logged_at"]})
    return normalized

def parse_structured(payload: Any, source_url: str, collected_at: str) -> list[dict[str,Any]]:
    return [normalize_record(x,source_url,collected_at) for x in _records(payload)]

class _TableParser(HTMLParser):
    def __init__(self): super().__init__();self.tables=[];self.table=None;self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=="table":self.table=[]
        elif self.table is not None and tag=="tr":self.row=[]
        elif self.row is not None and tag in {"th","td"}:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in {"th","td"} and self.cell is not None:self.row.append(" ".join("".join(self.cell).split()));self.cell=None
        elif tag=="tr" and self.row is not None:
            if self.row:self.table.append(self.row)
            self.row=None
        elif tag=="table" and self.table is not None:self.tables.append(self.table);self.table=None

def parse_dom_table(html: str, column_mapping: dict[str,str], source_url: str, collected_at: str) -> list[dict[str,Any]]:
    parser=_TableParser();parser.feed(html)
    if not parser.tables:return []
    table=max(parser.tables,key=len)
    if len(table)<2:return []
    headers=table[0]; result=[]
    for values in table[1:]:
        source={column_mapping.get(headers[i],headers[i]):value for i,value in enumerate(values) if i<len(headers)}
        result.append(normalize_record(source,source_url,collected_at))
    return result

def in_date_range(record: dict[str,Any], date_from: str|None, date_to: str|None) -> bool:
    value=record.get("logged_at")
    if not value:return True
    try: day=datetime.fromisoformat(str(value).replace("Z","+00:00")).date().isoformat()
    except ValueError:return False
    return (not date_from or day>=date_from) and (not date_to or day<=date_to)
