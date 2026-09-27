from __future__ import annotations
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from . import COLLECTOR_VERSION
from .log_page_parser import redact
from .network_response_capture import safe_source_url

class EvidenceWriter:
    def __init__(self,root:Path):self.root=root
    def write(self,collection_id:str,source_url:str,page_type:str,content_type:str,payload:Any,record_count:int)->dict:
        safe=redact(payload);raw=json.dumps(safe,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        digest=hashlib.sha256(raw).hexdigest();now=datetime.now(timezone.utc).isoformat()
        envelope={"collection_id":collection_id,"collected_at":now,"source_url":safe_source_url(source_url),
          "source_page_type":page_type,"response_content_type":content_type,"record_count":record_count,
          "raw_payload_sha256":digest,"collector_version":COLLECTOR_VERSION,"payload":safe}
        folder=self.root/collection_id;folder.mkdir(parents=True,exist_ok=True)
        path=folder/f"{now.replace(':','-')}-{digest[:12]}.json"
        with path.open("x",encoding="utf-8") as handle:json.dump(envelope,handle,ensure_ascii=False,indent=2)
        return {k:v for k,v in envelope.items() if k!="payload"}|{"evidence_path":str(path)}

