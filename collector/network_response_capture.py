from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from . import ALLOWED_HOSTS

class HostNotAllowed(ValueError): pass

def require_allowed_url(url: str, allowed_hosts: set[str] | frozenset[str] | None = None) -> str:
    parsed=urlsplit(url)
    hosts=allowed_hosts or ALLOWED_HOSTS
    if parsed.scheme!="https" or (parsed.hostname or "").lower() not in hosts:
        raise HostNotAllowed("Only the configured Weimeta UAT console hosts are allowed.")
    return url

def safe_source_url(url: str, allowed_hosts: set[str] | frozenset[str] | None = None) -> str:
    require_allowed_url(url,allowed_hosts);parsed=urlsplit(url)
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,"",""))

@dataclass(frozen=True)
class CapturedResponse:
    source_url:str
    content_type:str
    payload:Any

def capture_json_response(url:str,content_type:str,body:bytes,maximum_bytes:int=5_000_000,
                          allowed_hosts:set[str]|frozenset[str]|None=None)->CapturedResponse|None:
    require_allowed_url(url,allowed_hosts)
    if len(body)>maximum_bytes or "json" not in content_type.lower():return None
    try:payload=json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError):return None
    return CapturedResponse(safe_source_url(url,allowed_hosts),content_type.split(";")[0],payload)
