from __future__ import annotations
import hashlib,json,sqlite3,uuid
from datetime import datetime,timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from collector import ALLOWED_HOSTS,SOURCE_TYPE
from collector.log_page_parser import normalize_record,redact
from src.services.console_service import Store,import_preview
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope

OVERSEAS_SOURCE_TYPE = "measured_overseas_browser_collector"

def _environment(environment_id:str)->dict[str,Any]:
    environment_id="china_uat" if environment_id in ("uat","") else environment_id
    try:
        from backend.platform_environments import load_environment
        value=load_environment(environment_id)
        if hasattr(value,"model_dump"):value=value.model_dump()
        elif not isinstance(value,dict):value=vars(value)
        return dict(value)
    except (ImportError,ModuleNotFoundError):
        path=Path(__file__).parents[1]/"config"/"platform_environments_v1.json"
        if path.exists():return dict(json.loads(path.read_text(encoding="utf-8"))["environments"][environment_id])
        if environment_id=="china_uat":
            return {"display_name":"国内 UAT","logs_page_url":"https://uat.weimeta.cn/console/billing/logs",
                    "collector_allowed_hosts":sorted(ALLOWED_HOSTS)}
        if environment_id=="overseas":
            return {"display_name":"海外站","logs_page_url":None,"collector_allowed_hosts":["weimeta.ai"]}
        raise KeyError(environment_id)

def _collector_context(environment_id:str)->tuple[str,dict[str,Any],frozenset[str],str]:
    normalized="china_uat" if environment_id in ("uat","") else environment_id
    try:config=_environment(normalized)
    except KeyError as exc:raise ValueError("environment_not_supported") from exc
    log_url=config.get("logs_page_url") or config.get("log_page_url")
    if normalized=="overseas" and not log_url:raise ValueError("overseas_log_page_unconfirmed")
    if not log_url:raise ValueError("environment_log_page_unconfirmed")
    configured_host=(urlsplit(str(log_url)).hostname or "").lower()
    hosts=config.get("collector_allowed_hosts") or config.get("allowed_console_hosts")
    if not hosts:hosts=[configured_host] if normalized=="overseas" else sorted(ALLOWED_HOSTS)
    allowed=frozenset(str(host).lower() for host in hosts if host)
    if urlsplit(str(log_url)).scheme!="https" or configured_host not in allowed:
        raise ValueError("environment_log_page_not_allowlisted")
    return normalized,config,allowed,OVERSEAS_SOURCE_TYPE if normalized=="overseas" else SOURCE_TYPE

def _require_environment_url(url:str,allowed_hosts:frozenset[str])->str:
    parsed=urlsplit(url)
    if parsed.scheme!="https" or (parsed.hostname or "").lower() not in allowed_hosts:
        raise ValueError("collector_source_host_not_allowed")
    return url

class BrowserImportService:
    def __init__(self,db_path:Path,store:Store,runtime_settings:Callable[[],Any]|None=None,
                 authorization:AuthorizationService|None=None):
        self.db_path=db_path;self.store=store;self.runtime_settings=runtime_settings
        self.authorization=authorization;self._init()
    def _scope(self,principal:PrincipalContext|None,permission:str)->TenantScope:
        if self.authorization is None:
            if isinstance(principal,PrincipalContext) and principal.is_verified:
                return TenantScope(principal.tenant_id,principal.workspace_id)
            return TenantScope.local_development()
        try:
            verified=self.authorization.authorize(principal,permission)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        return TenantScope(verified.tenant_id,verified.workspace_id)
    def _runtime_log_page(self,environment_id:str)->dict[str,Any]|None:
        if not self.runtime_settings:return None
        return self.runtime_settings().active(environment_id,"logs_page_url")
    def connect(self):
        db=sqlite3.connect(self.db_path);db.row_factory=sqlite3.Row;return db
    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS collector_collections(
              collection_id TEXT PRIMARY KEY, collected_at TEXT, status TEXT, source_type TEXT,
              environment TEXT, record_count INTEGER, duplicate_count INTEGER, rejected_count INTEGER,
              payload_sha256 TEXT, payload TEXT, import_batch_id TEXT);
            CREATE TABLE IF NOT EXISTS collector_events(
              event_id TEXT PRIMARY KEY, collection_id TEXT, created_at TEXT, event_type TEXT, details TEXT);
            """)
            columns={row[1] for row in db.execute("PRAGMA table_info(collector_collections)")}
            if "environment_id" not in columns:
                db.execute("ALTER TABLE collector_collections ADD COLUMN environment_id TEXT")
                db.execute("UPDATE collector_collections SET environment_id='china_uat' WHERE environment_id IS NULL")
            for table in ("collector_collections","collector_events"):
                existing={row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column,value in (("tenant_id",TenantScope.local_development().tenant_id),
                                     ("workspace_id",TenantScope.local_development().workspace_id)):
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{value}'")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_collector_collections_scope
              ON collector_collections(tenant_id,workspace_id,environment_id,collected_at)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_collector_events_scope
              ON collector_events(tenant_id,workspace_id,collection_id,created_at)""")
    @staticmethod
    def _key(row:dict[str,Any])->tuple[str,str]:
        if row.get("request_id"):return "request_id",str(row["request_id"])
        if row.get("response_id"):return "response_id",str(row["response_id"])
        if row.get("raw_record_sha256"):return "raw_record_sha256",str(row["raw_record_sha256"])
        parts=[row.get(x) for x in ("logged_at","requested_model","channel_id","http_status")]
        return ("composite","|".join("" if x is None else str(x) for x in parts)) if sum(x not in (None,"") for x in parts)>=2 else ("ambiguous",str(uuid.uuid4()))
    def preview(self,body:dict[str,Any],*,principal:PrincipalContext|None=None,
                permission:str="collector.oneshot.execute")->dict[str,Any]:
        scope=self._scope(principal,permission)
        collection_id=str(body.get("collection_id") or f"COL-{uuid.uuid4().hex[:16].upper()}")
        raw_environment=body.get("environment_id") or body.get("environment")
        if raw_environment is None:raise ValueError("environment_id_required")
        requested_environment=str(raw_environment)
        runtime=self._runtime_log_page("overseas") if requested_environment=="overseas" else None
        if requested_environment=="overseas" and self.runtime_settings:
            if not runtime:raise ValueError("overseas_log_page_unconfirmed")
            config={"display_name":"海外站","logs_page_url":runtime["setting_value"],
                    "allowed_console_hosts":["weimeta.ai"]}
            environment_id="overseas";allowed_hosts=frozenset({"weimeta.ai"})
            source_type=OVERSEAS_SOURCE_TYPE
        else:
            environment_id,config,allowed_hosts,source_type=_collector_context(requested_environment)
        if body.get("source_type") not in (None,source_type):
            raise ValueError("Collector imports must be genuine UAT browser evidence.")
        now=datetime.now(timezone.utc).isoformat()
        known={self._key(x) for x in self.store.records()
               if str(x.get("environment_id") or "china_uat")==environment_id}
        seen=set();accepted=[];duplicates=0;rejected=0;warnings=[]
        for source in body.get("records") or []:
            if not isinstance(source,dict):rejected+=1;continue
            url=source.get("source_url")
            if environment_id=="overseas" and str(url)!=str(config["logs_page_url"]):
                rejected+=1;warnings.append("来源地址与已确认的海外日志页不一致");continue
            try:_require_environment_url(str(url),allowed_hosts)
            except ValueError:rejected+=1;warnings.append("发现非允许来源，记录已拒绝");continue
            row=normalize_record(redact(source),str(url),str(source.get("collected_at") or now))
            row["environment_id"]=environment_id;row["environment"]=environment_id;row["source_type"]=source_type
            key=self._key(row)
            if key in seen or key in known:duplicates+=1;continue
            seen.add(key);accepted.append(row)
            if key[0]=="ambiguous":warnings.append("存在无法可靠去重的记录，未自动合并")
        raw=json.dumps(accepted,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        digest=hashlib.sha256(raw).hexdigest()
        result={"collection_id":collection_id,"collected_at":now,"status":"previewed","source_type":source_type,
          "environment_id":environment_id,"environment":environment_id,"environment_name":config.get("display_name"),
          "is_mock":False,"allowed_hosts":sorted(allowed_hosts),"records":accepted,
          "record_count":len(accepted),"captured_count":len(accepted),"duplicate_count":duplicates,
          "rejected_count":rejected,"warnings":sorted(set(warnings)),"payload_sha256":digest,
          "cookies_stored":0,"credentials_stored":0}
        with self.connect() as db:
            existing=db.execute("""SELECT payload_sha256,status,import_batch_id
              FROM collector_collections WHERE collection_id=? AND tenant_id=? AND workspace_id=?""",
              (collection_id,*scope.sql_parameters())).fetchone()
            if existing and existing["payload_sha256"]!=digest:raise ValueError("Collection ID already exists with different immutable content.")
            db.execute("""INSERT OR IGNORE INTO collector_collections
              (collection_id,collected_at,status,source_type,environment,record_count,duplicate_count,rejected_count,
               payload_sha256,payload,import_batch_id,environment_id,tenant_id,workspace_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              """,(collection_id,now,"previewed",source_type,environment_id,len(accepted),duplicates,rejected,digest,
                    json.dumps(result,ensure_ascii=False),None,environment_id,*scope.sql_parameters()))
            db.execute("""INSERT INTO collector_events(
              event_id,collection_id,created_at,event_type,details,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)""",(str(uuid.uuid4()),collection_id,now,"previewed",
              json.dumps({"payload_sha256":digest}),*scope.sql_parameters()))
        if environment_id=="overseas" and self.runtime_settings:
            recognized=body.get("structure_recognized") is True
            self.runtime_settings().mark_live_validation(
                recognized, bool(accepted), len(accepted), collection_id)
        return result
    def confirm(self,collection_id:str,payload_sha256:str,confirmed:bool,
                environment_id:str|None=None,*,principal:PrincipalContext|None=None)->dict[str,Any]:
        scope=self._scope(principal,"collector.oneshot.execute")
        if not confirmed:raise ValueError("Explicit read-only import confirmation is required.")
        with self.connect() as db:
            row=db.execute("""SELECT * FROM collector_collections WHERE collection_id=?
              AND tenant_id=? AND workspace_id=?""",(collection_id,*scope.sql_parameters())).fetchone()
        if not row:raise LookupError("Collection not found.")
        if not environment_id:raise ValueError("environment_id_required")
        normalized="china_uat" if environment_id=="uat" else environment_id
        if row["environment_id"]!=normalized:raise ValueError("collector_environment_mismatch")
        if row["payload_sha256"]!=payload_sha256:raise ValueError("Immutable payload SHA-256 mismatch.")
        if row["import_batch_id"]:
            return {**json.loads(row["payload"]),"status":"confirmed","import_batch_id":row["import_batch_id"],
                    "idempotent":True,"downstream_refresh":["overview","health","cost","errors","mappings","compatibility","shadow","reports"]}
        preview=json.loads(row["payload"])
        content=json.dumps(preview["records"],ensure_ascii=False).encode()
        batch=import_preview(f"{collection_id}.json",content,source_type=str(preview["source_type"]))
        # The generic CSV/JSON validator uses empty strings for blank cells.
        # Restore evidence-model nulls for browser-collected fields after validation.
        by_hash={item["raw_record_sha256"]:item for item in preview["records"]}
        for item in batch.get("normalized_rows",[]):
            original=by_hash.get(item.get("raw_record_sha256"),{})
            for field in ("logged_at","request_id","response_id","api_key_name","requested_model","actual_model",
                          "channel_id","channel_name","stream","http_status","latency_ms","ttft_ms","input_tokens",
                          "output_tokens","total_tokens","cost_cny","finish_reason","error_message"):
                if original.get(field) is None:item[field]=None
        batch["normalized_preview"]=batch.get("normalized_rows",[])[:100]
        self.store.save_batch(batch)
        now=datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("""UPDATE collector_collections SET status='confirmed',import_batch_id=?
              WHERE collection_id=? AND tenant_id=? AND workspace_id=?""",
              (batch["batch_id"],collection_id,*scope.sql_parameters()))
            db.execute("""INSERT INTO collector_events(
              event_id,collection_id,created_at,event_type,details,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)""",(str(uuid.uuid4()),collection_id,now,"confirmed",
              json.dumps({"batch_id":batch["batch_id"]}),*scope.sql_parameters()))
        return {**preview,"status":"confirmed","import_batch_id":batch["batch_id"],"idempotent":False,
                "imported_count":batch["imported_row_count"],
                "downstream_refresh":["overview","health","cost","errors","mappings","compatibility","shadow","reports"]}
    def collections(self,environment_id:str,*,principal:PrincipalContext|None=None)->list[dict[str,Any]]:
        scope=self._scope(principal,"evidence.read")
        if environment_id not in {"china_uat","overseas"}:
            raise ValueError("concrete_environment_required")
        with self.connect() as db:
            return [dict(x) for x in db.execute("""SELECT collection_id,collected_at,status,source_type,environment,
              environment_id,
              record_count,duplicate_count,rejected_count,payload_sha256,import_batch_id
              FROM collector_collections WHERE environment_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY collected_at DESC""",(environment_id,*scope.sql_parameters()))]
    def get(self,collection_id:str,environment_id:str,*,principal:PrincipalContext|None=None)->dict[str,Any]|None:
        scope=self._scope(principal,"evidence.read")
        with self.connect() as db:row=db.execute("""SELECT payload,status,import_batch_id
          FROM collector_collections WHERE collection_id=? AND environment_id=?
          AND tenant_id=? AND workspace_id=?""",
          (collection_id,environment_id,*scope.sql_parameters())).fetchone()
        return ({**json.loads(row["payload"]),"status":row["status"],"import_batch_id":row["import_batch_id"]} if row else None)
