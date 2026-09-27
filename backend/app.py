"""FastAPI application for the read-only Routing Quality Console."""
from __future__ import annotations
import asyncio, csv, hashlib, json, os, sys, ssl, time, urllib.error, urllib.request, uuid, queue, threading
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from dataclasses import asdict, replace
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from services.console_service import *
from backend.uat_service import UatSettings, UatStore, correlate, default_transport, estimate_cost, execute as execute_uat, status as uat_status, validate as validate_uat
from backend.uat_http_workbench_service import (
    ConnectionProof, CredentialConnectionRegistry, WorkbenchError,
    default_transport as default_workbench_transport,
    execute_workbench, normalize_request, validate_workbench,
)
from backend.credential_store import SessionCredentialStore, session_hash
from backend.browser_import_service import BrowserImportService
from backend.model_catalog import UatModelCatalog
from backend.platform_environments import load_platform_environments, parse_platform_environments, EnvironmentConfigurationError
from backend.environment_runtime_settings import EnvironmentRuntimeSettings, canonical_error_code
from backend.collector_routes import build_collector_router
from backend.collector_run_routes import build_collector_run_router
from backend.collector_run_service import CollectorRunService
from backend.collector_supervisor import CollectorSupervisor
from backend.search_service import UnifiedSearchService
from backend.realtime_log_sync_service import ACTIVE_STATES, RealtimeLogSyncService
from backend.realtime_log_sync_routes import build_realtime_log_sync_router
from backend.realtime_sync_supervisor import RealtimeSyncSupervisor
from backend.shadow_sync_service import ShadowSyncService
from backend.shadow_sync_routes import build_shadow_sync_router
from backend.log_sync_coordinator import LogSyncCoordinator
from backend.origin_security import (
    authorize_read_only_origin, detected_frontend_origin,
    load_frontend_origins, load_local_api_hosts, normalize_origin,
)
from backend.entry_security import EnterpriseIngressMiddleware, load_ingress_profile
from backend.enterprise_tenant_migrations import EnterpriseTenantMigrator
from backend.security.http import (
    CSRF_HEADER, EnterpriseHTTPError, EnterpriseHTTPRuntime,
    EnterpriseHTTPAuthorizationMiddleware, PrincipalPreResolutionMiddleware,
    annotate_route_permissions, clear_session_cookie, request_principal,
    permission_for_scope, set_session_cookie,
)
from backend.encrypted_session_vault import EncryptedSessionVault, VaultError
from backend.persistent_credential_vault import (
    CredentialScope, CredentialVaultError, PersistentCredentialVault,
)
from backend.persistent_session_service import PersistentSessionService
from backend.persistent_session_routes import build_persistent_session_router
from backend.domestic_uat_chrome_manager import DomesticUatChromeManager
from backend.uat_execution_control_service import (
    UatExecutionControlError, UatExecutionControlService,
)
from backend.uat_output_capability_service import (
    MODEL_IDS as UAT_SIX_MODEL_IDS, UatOutputCapabilityError,
    UatOutputCapabilityService,
)
from backend.tenant_security import TenantScope
from backend.incremental_metrics_service import (
    IncrementalMetricsService, MetricsError,
)
from backend.dynamic_metrics_service import DynamicMetricsService
from backend.model_capability_aggregation_service import ModelCapabilityAggregationService
from backend.call_log_service import CallLogError, CallLogService
from backend.observability_overview_service import ObservabilityOverviewService
from backend.acceptance_run_service import AcceptanceRunError, AcceptanceRunService
from backend.uat_fault_injection_service import (
    UatFaultInjectionError, UatFaultInjectionService,
)
from backend.historical_replay_service import (
    HistoricalReplayError, HistoricalReplayService,
)
from backend.config_review_service import ConfigReviewService
from backend.metrics_evidence_adapter import (
    MetricsEvidenceAdapter, MetricsSourceError,
)
from backend.statistical_confidence_service import StatisticalConfidenceService
from backend.sticky_routing_service import StickyRoutingService
from backend.sticky_routing_routes import build_sticky_routing_router
from backend.scheduler_attribution_service import SchedulerAttributionService
from backend.decision_reconstruction_service import DecisionReconstructionService
from backend.scheduler_overhead_service import SchedulerOverheadService
from backend.price_catalog_service import PriceCatalogService, PriceCatalogError
from backend.price_sync_supervisor import PriceSyncSupervisor
from backend.circuit_breaker_service import CircuitBreakerService, CircuitBreakerError
from backend.exploration_governance_service import ExplorationGovernanceService
from backend.capability_evidence_service import CapabilityEvidenceError, CapabilityEvidenceService
from backend.traffic_change_governance_service import (
    TrafficChangeGovernanceError, TrafficChangeGovernanceService,
)
from backend.probe_governance_service import ProbeGovernanceError,ProbeGovernanceService
from backend.live_acceptance_service import LiveAcceptanceService
from backend.continuous_probe_execution_service import (
    ContinuousProbeExecutionError, ContinuousProbeExecutionService,
)
from backend.high_cost_test_governance_service import HighCostTestGovernanceService
from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_routes import build_formal_agent_skill_router
from backend.formal_agent_skill_bindings import FormalAgentSkillBindings
from backend.formal_agent_skill_lifecycle_service import FormalAgentSkillLifecycleService
from backend.formal_agent_skill_lifecycle_routes import build_formal_agent_skill_lifecycle_router
from backend.agent_skill_host_service import AgentSkillHostService
from backend.formal_agent_skill_business_executor import FormalAgentSkillBusinessExecutor
from formal_agent_skill_registry import FormalAgentSkillRegistry
from formal_agent_skill_runtime import FormalAgentSkillRuntime
from real_channel_shadow_adapter import adapt_shadow_catalog
from strategy_engine import strategy_decision

INGRESS_PROFILE=load_ingress_profile()
FRONTEND_ORIGINS=frozenset(INGRESS_PROFILE.allowed_origins)
LOCAL_API_HOSTS=load_local_api_hosts()
app=FastAPI(
  title="Routing Quality Console API",version="1.2.0",
  docs_url=None if INGRESS_PROFILE.name=="production" else "/docs",
  redoc_url=None if INGRESS_PROFILE.name=="production" else "/redoc",
  openapi_url=None if INGRESS_PROFILE.name=="production" else "/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(FRONTEND_ORIGINS),
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Content-Type","Idempotency-Key",CSRF_HEADER,"X-Correlation-ID"],
    expose_headers=[CSRF_HEADER,"X-Correlation-ID"], allow_credentials=True,
)
DB_PATH=Path(os.environ.get("ROUTING_CONSOLE_DATABASE_PATH") or os.environ.get("ROUTING_CONSOLE_DB") or ROOT/"data"/"routing_quality_console.sqlite3")
# Tenant migration precedes every scope-bound service constructor. It is
# transactional/idempotent and prevents a service from observing legacy
# unscoped tables during startup.
def _ensure_enterprise_tenant_migration():
    global ENTERPRISE_TENANT_MIGRATION
    ENTERPRISE_TENANT_MIGRATION=EnterpriseTenantMigrator(DB_PATH).migrate()
    return ENTERPRISE_TENANT_MIGRATION
ENTERPRISE_TENANT_MIGRATION={"status":"pending_schema_initialization"}
ENTERPRISE_HTTP=EnterpriseHTTPRuntime.build(
  profile=INGRESS_PROFILE,database_path=DB_PATH,root=ROOT)
ENTERPRISE_AUTHORIZATION=ENTERPRISE_HTTP.authorization
_EXPLICIT_DEVELOPMENT_SERVICE_ADAPTER=INGRESS_PROFILE.name=="development"
STORE=Store(DB_PATH)
UAT_STORE=UatStore(DB_PATH)
# Scope-bound metric/price services are lazy. Eager global construction would
# bind a production process to one tenant and can observe a partially upgraded
# legacy schema before startup migrations finish.
INCREMENTAL_METRICS:IncrementalMetricsService|None=None
METRICS_ADAPTER:MetricsEvidenceAdapter|None=None
CALL_LOGS:CallLogService|None=None
RUNTIME_SETTINGS=EnvironmentRuntimeSettings(DB_PATH)
STICKY_ROUTING=StickyRoutingService(
  DB_PATH,ROOT/"config"/"sticky_routing_policy_v1.json")
SCHEDULER_ATTRIBUTION=SchedulerAttributionService(DB_PATH)
DECISION_RECONSTRUCTION=DecisionReconstructionService(
  ROOT/"output"/"scheduler_decision_logs_v1.jsonl",SCHEDULER_ATTRIBUTION)
SCHEDULER_OVERHEAD=SchedulerOverheadService(DB_PATH)
PRICE_CATALOG:PriceCatalogService|None=None
PRICE_SYNC_SUPERVISOR:PriceSyncSupervisor|None=None
CIRCUIT_BREAKERS=CircuitBreakerService(
  DB_PATH,ROOT/"config"/"circuit_breaker_policy_v1.json")
EXPLORATION_GOVERNANCE=ExplorationGovernanceService(
  DB_PATH,ROOT/"config"/"exploration_policy_v1.json")
CAPABILITY_EVIDENCE=CapabilityEvidenceService(
  DB_PATH,ROOT/"config"/"capability_evidence_policy_v1.json")
TRAFFIC_CHANGE_GOVERNANCE=TrafficChangeGovernanceService(
  DB_PATH,ROOT/"config"/"traffic_change_governance_policy_v1.json")
PROBE_GOVERNANCE=ProbeGovernanceService(
  DB_PATH,ROOT/"config"/"probe_governance_policy_v1.json")
HIGH_COST_TEST_GOVERNANCE=HighCostTestGovernanceService(
  DB_PATH,ROOT/"config"/"high_cost_test_governance_policy_v1.json")
FORMAL_AGENT_SKILL_REGISTRY=FormalAgentSkillRegistry()
FORMAL_AGENT_SKILL_AUDIT=FormalAgentSkillAuditService(DB_PATH)
FORMAL_AGENT_SKILL_LIFECYCLE=FormalAgentSkillLifecycleService(DB_PATH)
ACCEPTANCE_RUNS=AcceptanceRunService(DB_PATH)
UAT_FAULT_INJECTION=UatFaultInjectionService(DB_PATH)
MODEL_CAPABILITY_AGGREGATION:ModelCapabilityAggregationService|None=None
def current_runtime_settings()->EnvironmentRuntimeSettings:
    global RUNTIME_SETTINGS
    if RUNTIME_SETTINGS.path!=STORE.path:
      UatStore(STORE.path)
      RUNTIME_SETTINGS=EnvironmentRuntimeSettings(STORE.path)
    return RUNTIME_SETTINGS
def current_sticky_routing()->StickyRoutingService:
    global STICKY_ROUTING
    if STICKY_ROUTING.path!=STORE.path:
      STICKY_ROUTING=StickyRoutingService(
        STORE.path,ROOT/"config"/"sticky_routing_policy_v1.json")
    return STICKY_ROUTING
def current_scheduler_attribution()->SchedulerAttributionService:
    global SCHEDULER_ATTRIBUTION
    if SCHEDULER_ATTRIBUTION.path!=STORE.path:
      SCHEDULER_ATTRIBUTION=SchedulerAttributionService(STORE.path)
    return SCHEDULER_ATTRIBUTION
def current_decision_reconstruction()->DecisionReconstructionService:
    global DECISION_RECONSTRUCTION
    current=current_scheduler_attribution()
    if DECISION_RECONSTRUCTION.attribution_service.path!=current.path:
      DECISION_RECONSTRUCTION=DecisionReconstructionService(
        ROOT/"output"/"scheduler_decision_logs_v1.jsonl",current)
    return DECISION_RECONSTRUCTION
def current_scheduler_overhead()->SchedulerOverheadService:
    global SCHEDULER_OVERHEAD
    if SCHEDULER_OVERHEAD.recorder.path!=STORE.path:
      SCHEDULER_OVERHEAD=SchedulerOverheadService(STORE.path)
    return SCHEDULER_OVERHEAD
def _delegated_service_principal(request:Request,service_name:str,audience:str):
    return ENTERPRISE_HTTP.service_principal(
      request_principal(request),service_name,audience)

def current_price_catalog(request:Request|None=None)->PriceCatalogService:
    global PRICE_CATALOG,PRICE_SYNC_SUPERVISOR
    _ensure_enterprise_tenant_migration()
    if request is not None:
      principal=_delegated_service_principal(request,"price_sync","price-api")
      return PriceCatalogService(
        STORE.path,ROOT/"config"/"price_sync_policy_v1.json",
        principal=principal,authorization=ENTERPRISE_AUTHORIZATION)
    if PRICE_CATALOG is None or PRICE_CATALOG.path!=STORE.path:
      PRICE_CATALOG=PriceCatalogService(
        STORE.path,ROOT/"config"/"price_sync_policy_v1.json",
        development_mode=_EXPLICIT_DEVELOPMENT_SERVICE_ADAPTER)
      PRICE_SYNC_SUPERVISOR=PriceSyncSupervisor(PRICE_CATALOG)
    return PRICE_CATALOG
def current_price_sync_supervisor(request:Request|None=None)->PriceSyncSupervisor:
    global PRICE_SYNC_SUPERVISOR
    if request is not None:
      return PriceSyncSupervisor(current_price_catalog(request))
    current=current_price_catalog()
    if PRICE_SYNC_SUPERVISOR is None or PRICE_SYNC_SUPERVISOR.service.path!=current.path:
      PRICE_SYNC_SUPERVISOR=PriceSyncSupervisor(current)
    return PRICE_SYNC_SUPERVISOR
def current_circuit_breakers()->CircuitBreakerService:
    global CIRCUIT_BREAKERS
    if CIRCUIT_BREAKERS.path!=STORE.path:
      CIRCUIT_BREAKERS=CircuitBreakerService(
        STORE.path,ROOT/"config"/"circuit_breaker_policy_v1.json")
    return CIRCUIT_BREAKERS
def current_exploration_governance()->ExplorationGovernanceService:
    global EXPLORATION_GOVERNANCE
    if EXPLORATION_GOVERNANCE.path!=STORE.path:
      EXPLORATION_GOVERNANCE=ExplorationGovernanceService(
        STORE.path,ROOT/"config"/"exploration_policy_v1.json")
    return EXPLORATION_GOVERNANCE
def current_capability_evidence()->CapabilityEvidenceService:
    global CAPABILITY_EVIDENCE
    if CAPABILITY_EVIDENCE.path!=STORE.path:
      CAPABILITY_EVIDENCE=CapabilityEvidenceService(
        STORE.path,ROOT/"config"/"capability_evidence_policy_v1.json")
    return CAPABILITY_EVIDENCE
def current_traffic_change_governance()->TrafficChangeGovernanceService:
    global TRAFFIC_CHANGE_GOVERNANCE
    if TRAFFIC_CHANGE_GOVERNANCE.path!=STORE.path:
      TRAFFIC_CHANGE_GOVERNANCE=TrafficChangeGovernanceService(
        STORE.path,ROOT/"config"/"traffic_change_governance_policy_v1.json")
    return TRAFFIC_CHANGE_GOVERNANCE
def current_probe_governance()->ProbeGovernanceService:
    global PROBE_GOVERNANCE
    if PROBE_GOVERNANCE.path!=STORE.path:
      PROBE_GOVERNANCE=ProbeGovernanceService(
        STORE.path,ROOT/"config"/"probe_governance_policy_v1.json")
    return PROBE_GOVERNANCE

def current_live_acceptance(request:Request|None=None)->LiveAcceptanceService:
    if request is None:
      return LiveAcceptanceService(STORE.path)
    principal=request_principal(request)
    return LiveAcceptanceService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id))
def current_high_cost_test_governance()->HighCostTestGovernanceService:
    global HIGH_COST_TEST_GOVERNANCE
    if HIGH_COST_TEST_GOVERNANCE.path!=STORE.path:
      HIGH_COST_TEST_GOVERNANCE=HighCostTestGovernanceService(
        STORE.path,ROOT/"config"/"high_cost_test_governance_policy_v1.json")
    return HIGH_COST_TEST_GOVERNANCE
def current_acceptance_runs()->AcceptanceRunService:
    global ACCEPTANCE_RUNS
    if ACCEPTANCE_RUNS.path!=STORE.path:
      ACCEPTANCE_RUNS=AcceptanceRunService(STORE.path)
    return ACCEPTANCE_RUNS
def current_uat_fault_injection()->UatFaultInjectionService:
    global UAT_FAULT_INJECTION
    if Path(UAT_FAULT_INJECTION.path)!=STORE.path:
      UAT_FAULT_INJECTION=UatFaultInjectionService(STORE.path)
    return UAT_FAULT_INJECTION
def current_formal_agent_skill_audit()->FormalAgentSkillAuditService:
    global FORMAL_AGENT_SKILL_AUDIT
    if FORMAL_AGENT_SKILL_AUDIT.path!=STORE.path:
      FORMAL_AGENT_SKILL_AUDIT=FormalAgentSkillAuditService(STORE.path)
    return FORMAL_AGENT_SKILL_AUDIT
def current_formal_agent_skill_lifecycle(request:Request)->FormalAgentSkillLifecycleService:
    principal=request_principal(request)
    return FormalAgentSkillLifecycleService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id))
def current_agent_skill_host_service(request:Request)->AgentSkillHostService:
    principal=request_principal(request)
    configured={item.strip().casefold() for item in os.getenv(
      "AGENT_SKILL_HOST_ALLOWED_HOSTS","api-uat.weimeta.cn").split(",") if item.strip()}
    return AgentSkillHostService(
      STORE.path,scope=TenantScope(principal.tenant_id,principal.workspace_id),
      allowed_hosts=configured,
      allow_loopback_http=os.getenv("AGENT_SKILL_HOST_ALLOW_LOOPBACK_HTTP","false").lower()=="true",
      loopback_ports={int(item) for item in os.getenv(
        "AGENT_SKILL_HOST_LOOPBACK_PORTS","8010").split(",") if item.strip().isdigit()})
COLLECTOR_SERVICE=BrowserImportService(DB_PATH,STORE,current_runtime_settings)
COLLECTOR_RUNS=CollectorRunService(DB_PATH,current_runtime_settings)
COLLECTOR_SUPERVISOR=CollectorSupervisor(DB_PATH)
REALTIME_LOG_SYNC=RealtimeLogSyncService(
  DB_PATH,ROOT/"output"/"unified_uat_execution_v3.jsonl",RUNTIME_SETTINGS,
  maximum_range_hours=int(os.getenv("UAT_LOG_SYNC_MAX_RANGE_HOURS","168")),
  clock_skew_seconds=int(os.getenv("UAT_LOG_SYNC_CLOCK_SKEW_SECONDS","300")))
REALTIME_SYNC_SUPERVISOR=RealtimeSyncSupervisor(DB_PATH)
DOMESTIC_UAT_CHROME=DomesticUatChromeManager()
PERSISTENT_SESSION_ENABLED=os.getenv(
  "UAT_PERSISTENT_SESSION_ENABLED","false").lower()=="true"
try:
  persistent_origins={
    item.strip() for item in os.getenv(
      "UAT_PERSISTENT_SESSION_ALLOWED_ORIGINS",
      "https://uat.weimeta.cn,https://admin-uat.weimeta.cn").split(",")
    if item.strip()}
  persistent_keys=os.getenv("UAT_PERSISTENT_SESSION_ALLOWED_STORAGE_KEYS")
  PERSISTENT_VAULT=EncryptedSessionVault(
    approved_origins=persistent_origins,
    maximum_vault_bytes=int(os.getenv(
      "UAT_PERSISTENT_SESSION_MAX_VAULT_BYTES",str(4*1024*1024))),
    maximum_item_count=int(os.getenv(
      "UAT_PERSISTENT_SESSION_MAX_ITEM_COUNT","512")),
    maximum_value_bytes=int(os.getenv(
      "UAT_PERSISTENT_SESSION_MAX_VALUE_BYTES",str(256*1024))),
    allowed_storage_keys=(
      {item.strip() for item in persistent_keys.split(",") if item.strip()}
      if persistent_keys else None),
  ) if PERSISTENT_SESSION_ENABLED else None
except VaultError:
  PERSISTENT_VAULT=None
PERSISTENT_SESSIONS=PersistentSessionService(
  REALTIME_LOG_SYNC,PERSISTENT_VAULT,enabled=PERSISTENT_SESSION_ENABLED,
  default_ttl_hours=int(os.getenv("UAT_PERSISTENT_SESSION_TTL_HOURS","8")),
  maximum_ttl_hours=int(os.getenv("UAT_PERSISTENT_SESSION_MAX_TTL_HOURS","24")))
SHADOW_SYNC=ShadowSyncService(
  REALTIME_LOG_SYNC,PERSISTENT_SESSIONS,REALTIME_SYNC_SUPERVISOR.launch_persistent,
  freshness_seconds=int(os.getenv("SHADOW_LOG_SYNC_INTERVAL_SECONDS","60")),
  initial_range_hours=int(os.getenv("SHADOW_LOG_SYNC_INITIAL_RANGE_HOURS","24")))

def _recover_provider_session_automatically()->dict[str,Any]:
  """Reuse the dedicated Chrome login without reading or persisting credentials."""
  DOMESTIC_UAT_CHROME.open_or_focus()
  captured=None
  try:
    captured=DOMESTIC_UAT_CHROME.capture_authenticated_session()
    pairing=PERSISTENT_SESSIONS.start_reauthentication("china_uat",24,True)
    PERSISTENT_SESSIONS.save_storage_diagnostic(
      pairing["pairing_id"],captured["diagnostic"])
    PERSISTENT_SESSIONS.request_confirmation(pairing["pairing_id"],True)
    session=PERSISTENT_SESSIONS.complete_pairing(
      pairing["pairing_id"],captured["cookies"],captured["web_storage"],
      captured["indexed_db"],remote_expires_at=captured["remote_expires_at"])
    DOMESTIC_UAT_CHROME.mark_authenticated()
    return session
  finally:
    if captured:
      for key in ("cookies","web_storage","indexed_db"):
        values=captured.get(key)
        if isinstance(values,list):
          values.clear()

LOG_SYNC_COORDINATOR=LogSyncCoordinator(
  REALTIME_LOG_SYNC,SHADOW_SYNC,
  recover_session=_recover_provider_session_automatically,
  interval_seconds=int(os.getenv("SHADOW_LOG_SYNC_INTERVAL_SECONDS","60")))
# Compatibility constructors above first create/upgrade their complete legacy
# schemas. One transactional v2 pass then scopes the whole graph; strict
# request-scoped services are constructed only after this point.
ENTERPRISE_TENANT_MIGRATION=_ensure_enterprise_tenant_migration()
SESSION_CREDENTIALS=SessionCredentialStore(
    int(os.getenv("UAT_SESSION_CREDENTIAL_TTL_MINUTES","30")),
    int(os.getenv("UAT_MAX_ACTIVE_CREDENTIAL_SESSIONS","20")))
SESSION_ENABLED=os.getenv("UAT_SESSION_CREDENTIAL_ENABLED","true").lower()=="true"
COOKIE_NAME=os.getenv("UAT_SESSION_COOKIE_NAME","rqc_uat_session")
COOKIE_SECURE=os.getenv("UAT_SESSION_COOKIE_SECURE","false").lower()=="true"
ALLOW_LOCAL_COOKIE=os.getenv("UAT_ALLOW_LOCALHOST_INSECURE_COOKIE","true").lower()=="true"
WORKBENCH_CONNECTIONS=CredentialConnectionRegistry()
try:
  _credential_directory=os.getenv("UAT_PERSISTENT_CREDENTIAL_DIRECTORY")
  PERSISTENT_CREDENTIALS=PersistentCredentialVault(
    Path(_credential_directory) if _credential_directory else None)
  PERSISTENT_CREDENTIAL_ERROR=None
except CredentialVaultError as exc:
  PERSISTENT_CREDENTIALS=None
  PERSISTENT_CREDENTIAL_ERROR=str(exc)
MODEL_POLICY=json.loads((ROOT/"config"/"uat_execution_policy_v1.json").read_text(encoding="utf-8"))
MODEL_CATALOG=UatModelCatalog(int(MODEL_POLICY.get("model_catalog_cache_seconds",60)))
def _integration_test_transport(url:str,key:str,payload:dict,timeout:int)->dict:
    if os.environ.get("WEIMETA_TEST_TRANSPORT")!="1" or os.environ.get("ROUTING_CONSOLE_TEST_MODE")!="1":
      raise RuntimeError("mock transport is restricted to integration_test")
    return {"status":200,"headers":{"Content-Type":"application/json","X-Request-ID":"MOCK-REQUEST-ID"},
      "body":json.dumps({"id":"mock-response-id","model":"mock-observed-model","choices":[{"message":{"role":"assistant","content":"mock safe response"},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":8,"completion_tokens":12,"total_tokens":20}}).encode(),"elapsed_ms":25}
UAT_TRANSPORT=_integration_test_transport if os.environ.get("WEIMETA_TEST_TRANSPORT")=="1" else default_transport

def _integration_workbench_transport(spec:dict[str,Any],key:str)->dict[str,Any]:
    if os.environ.get("WEIMETA_TEST_TRANSPORT")!="1" or os.environ.get("ROUTING_CONSOLE_TEST_MODE")!="1":
      raise RuntimeError("mock transport is restricted to integration_test")
    return {"status":200,"headers":{"content-type":"application/json","x-request-id":"MOCK-WORKBENCH-REQUEST"},
      "body":json.dumps({"method":spec["method"],"path":spec["path"],"mock":True}).encode(),"elapsed_ms":12}
WORKBENCH_TRANSPORT=(_integration_workbench_transport
  if os.environ.get("WEIMETA_TEST_TRANSPORT")=="1" else default_workbench_transport)

def _integration_workbench_stream_transport(
    spec:dict[str,Any],key:str,on_chunk:Callable[[bytes],None]
)->dict[str,Any]:
    result=_integration_workbench_transport(spec,key)
    payload=(b'data: {"id":"MOCK-WORKBENCH-RESPONSE","model":"mock-observed-model",'
      b'"choices":[{"delta":{"content":"mock safe response"}}]}\n\n'
      b'data: [DONE]\n\n')
    on_chunk(payload)
    result.update({"body":payload,"headers":{"content-type":"text/event-stream",
      "x-request-id":"MOCK-WORKBENCH-REQUEST"},"first_token_latency_ms":1})
    return result

WORKBENCH_STREAM_TRANSPORT=(_integration_workbench_stream_transport
  if os.environ.get("WEIMETA_TEST_TRANSPORT")=="1" else default_workbench_transport)

# Managed probe executors are process-local owners of billable worker threads;
# all lifecycle state and evidence remains durable in SQLite.  A restart never
# silently resumes paid traffic: stale runs are reconciled as interrupted.
CONTINUOUS_PROBE_EXECUTORS:dict[tuple[str,str,str],ContinuousProbeExecutionService]={}
CONTINUOUS_PROBE_EXECUTOR_LOCK=threading.RLock()

def _audit_credential(action:str, sid:str|None, success:bool, source:str="session", category:str|None=None, target_host:str|None=None,environment_id:str="china_uat"):
    details={"session_id_hash":session_hash(sid or "missing"),"action":action,"credential_source":source,
             "timestamp":datetime.now(timezone.utc).isoformat(),"success":success}
    if category: details["failure_category"]=category
    if target_host: details["target_host"]=target_host
    with current_uat_store().connect() as db:
      details["environment_id"]=environment_id
      db.execute("""INSERT INTO audit_events(event_id,execution_id,event_type,created_at,actor,details,environment_id)
        VALUES(?,?,?,?,?,?,?)""",(str(uuid.uuid4()),None,action,details["timestamp"],"local_operator",json.dumps(details),environment_id))

def _secure_credential_request(request:Request):
    if request.headers.get("content-type","").split(";")[0].strip().lower()!="application/json":
      raise HTTPException(415,detail={"code":"JSON_REQUIRED","message":"Content-Type application/json is required."})
    origin=normalize_origin(request.headers.get("origin"))
    host=request.url.hostname
    local=host in {"127.0.0.1","localhost"}
    if origin not in FRONTEND_ORIGINS:
      detected=detected_frontend_origin(
        request.headers.get("origin"),request.headers.get("referer"))
      allowed="、".join(sorted(FRONTEND_ORIGINS))
      raise HTTPException(403,detail={
        "code":"ORIGIN_REJECTED",
        "message":f"前端来源不受允许。当前检测来源：{detected}。允许的本地开发来源：{allowed}。",
        "detected_origin":detected,
        "allowed_origins":sorted(FRONTEND_ORIGINS),
      })
    if request.url.scheme!="https" and not (local and ALLOW_LOCAL_COOKIE and not COOKIE_SECURE):
      raise HTTPException(403,detail={"code":"HTTPS_REQUIRED","message":"HTTPS is required outside explicit localhost development."})

def _secure_read_only_request(request:Request)->str:
    if request.method not in {"GET","HEAD"}:
      raise HTTPException(405,detail={
        "code":"READ_ONLY_METHOD_REQUIRED",
        "message":"该来源例外仅适用于明确注册的只读 GET/HEAD 接口。",
      })
    classification=authorize_read_only_origin(
      request.headers.get("origin"),request.headers.get("host"),
      peer_host=request.client.host if request.client else None,
      referer=request.headers.get("referer"),
      sec_fetch_site=request.headers.get("sec-fetch-site"),
      allowed_origins=FRONTEND_ORIGINS,
      allowed_local_hosts=LOCAL_API_HOSTS)
    if classification is None:
      raise HTTPException(403,detail={
        "code":"ORIGIN_REJECTED",
        "message":"只读会话状态请求的来源或本地 API Host 未获批准。",
        "detected_origin":detected_frontend_origin(
          request.headers.get("origin"),request.headers.get("referer")),
        "allowed_origins":sorted(FRONTEND_ORIGINS),
        "allowed_local_api_hosts":sorted(LOCAL_API_HOSTS),
      })
    return classification

def _credential_scope(request:Request,environment_id:str="china_uat")->CredentialScope:
    principal=request_principal(request)
    return CredentialScope(
      principal.principal_id,principal.tenant_id,principal.workspace_id,environment_id)

def _persistent_credential(request:Request,environment_id:str="china_uat"):
    if PERSISTENT_CREDENTIALS is None:
      if PERSISTENT_CREDENTIAL_ERROR:
        raise HTTPException(503,detail={"code":"CREDENTIAL_VAULT_UNAVAILABLE",
          "message":"Windows encrypted credential storage is unavailable."})
      return None
    try:
      return PERSISTENT_CREDENTIALS.load(_credential_scope(request,environment_id))
    except CredentialVaultError as exc:
      raise HTTPException(503,detail={"code":"CREDENTIAL_VAULT_ERROR",
        "message":"Encrypted credential storage could not be read.","reason":str(exc)}) from exc

def _resolved(request:Request,environment_id:str="china_uat"):
    settings=UatSettings.load(environment_id)
    saved_enabled=current_runtime_settings().execution_enabled(
      environment_id,principal=request_principal(request))
    if saved_enabled is not None:
      settings=replace(settings,enabled=saved_enabled)
    sid=request.cookies.get(COOKIE_NAME)
    persisted=_persistent_credential(request,environment_id)
    if persisted:
      key,meta=persisted
      return replace(settings,api_key=key),"windows_encrypted_vault",sid,meta
    key,meta=SESSION_CREDENTIALS.resolve(sid,environment_id=environment_id)
    if key:
      return replace(settings,api_key=key),"session",sid,meta
    return settings,("environment" if settings.api_key else "none"),sid,None

def current_uat_execution_control(request:Request)->UatExecutionControlService:
    principal=request_principal(request)
    return UatExecutionControlService(
      DB_PATH,TenantScope(principal.tenant_id,principal.workspace_id))

def _uat_runtime_constraints(request:Request)->dict[str,Any]:
    status=current_uat_execution_control(request).status()
    task=status.get("task") or {}
    return {"remaining_budget_cny":task.get("remaining_cost"),
      "execution_task_status":status.get("status")}

def current_uat_output_capabilities(request:Request)->UatOutputCapabilityService:
    principal=request_principal(request)
    return UatOutputCapabilityService(
      DB_PATH,TenantScope(principal.tenant_id,principal.workspace_id))

def uat_model_capabilities(request:Request|None=None,channel_id:str="unified-routing")->dict[str,dict[str,Any]]:
    if request is not None:
      return current_uat_output_capabilities(request).catalog(channel_id)
    payload=json.loads((ROOT/"config"/"uat_model_output_capabilities_v1.json").read_text(encoding="utf-8"))
    return {str(item["model_id"]):dict(item) for item in payload["models"]}

def executable_model_catalog(settings:UatSettings,request:Request|None=None,
                             channel_id:str="unified-routing")->dict[str,dict[str,Any]]|None:
    ids=MODEL_CATALOG.model_ids(settings.api_key)
    if ids is None:return None
    reviewed=uat_model_capabilities(request,channel_id)
    return {model_id:reviewed.get(model_id,{
      "model_id":model_id,"confirmed_max_output_tokens":None,
      "confirmed_channel_max_output_tokens":None,
      "evidence_source":"pending_confirmation","evidence_version":None,
      "fresh_until":None}) for model_id in ids}

def _models_transport(url:str,key:str,timeout:int)->dict:
    if os.environ.get("WEIMETA_TEST_TRANSPORT")=="1" and os.environ.get("ROUTING_CONSOLE_TEST_MODE")=="1":
      return {"status":200,"headers":{"content-type":"application/json"},
        "body":b'{"data":[{"id":"deepseek-v4-flash","owned_by":"integration_fixture"}]}'}
    started=time.perf_counter()
    req=urllib.request.Request(url,method="GET",headers={"Authorization":f"Bearer {key}","Accept":"application/json"})
    try:
      with urllib.request.urlopen(req,timeout=timeout,context=ssl.create_default_context()) as resp:
        return {"status":resp.status,"headers":{str(k).lower():v for k,v in resp.headers.items()},"body":resp.read(1_000_001),
          "elapsed_ms":round((time.perf_counter()-started)*1000,2)}
    except urllib.error.HTTPError as exc:
      return {"status":exc.code,"headers":{str(k).lower():v for k,v in exc.headers.items()},"body":exc.read(1_000_001),
        "elapsed_ms":round((time.perf_counter()-started)*1000,2)}
MODELS_TRANSPORT=_models_transport
def current_uat_store()->UatStore:
    return UAT_STORE if UAT_STORE.path==STORE.path else UatStore(STORE.path)
def current_incremental_metrics(request:Request|None=None)->IncrementalMetricsService:
    global INCREMENTAL_METRICS
    _ensure_enterprise_tenant_migration()
    if request is not None:
      principal=_delegated_service_principal(request,"metrics","metrics-api")
      return IncrementalMetricsService(
        STORE.path,principal=principal,authorization=ENTERPRISE_AUTHORIZATION)
    if INCREMENTAL_METRICS is None or INCREMENTAL_METRICS.path!=STORE.path:
      INCREMENTAL_METRICS=IncrementalMetricsService(
        STORE.path,development_mode=_EXPLICIT_DEVELOPMENT_SERVICE_ADAPTER)
    return INCREMENTAL_METRICS
def current_call_logs(request:Request|None=None)->CallLogService:
    global CALL_LOGS
    if request is not None:
      principal=request_principal(request)
      return CallLogService(
        STORE.path,TenantScope(principal.tenant_id,principal.workspace_id))
    if CALL_LOGS is None or CALL_LOGS.path!=STORE.path:
      CALL_LOGS=CallLogService(STORE.path)
    return CALL_LOGS
def current_historical_replay(request:Request)->HistoricalReplayService:
    principal=request_principal(request)
    return HistoricalReplayService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id))
def current_config_review(request:Request)->ConfigReviewService:
    principal=request_principal(request)
    return ConfigReviewService(
      STORE.path,ROOT/"config",TenantScope(principal.tenant_id,principal.workspace_id))
def current_statistical_confidence(
    request:Request|None=None)->StatisticalConfidenceService:
    return current_incremental_metrics(request).confidence
def current_metrics_adapter(request:Request|None=None)->MetricsEvidenceAdapter:
    global METRICS_ADAPTER
    if request is not None:
      metrics=current_incremental_metrics(request)
      return MetricsEvidenceAdapter(
        STORE.path,metrics,ROOT/"output"/"unified_uat_execution_v3.jsonl",
        source_allowlist={"standardized_call_logs"})
    if METRICS_ADAPTER is not None and METRICS_ADAPTER.database_path==STORE.path:
      return METRICS_ADAPTER
    METRICS_ADAPTER=MetricsEvidenceAdapter(
      STORE.path,current_incremental_metrics(),
      ROOT/"output"/"unified_uat_execution_v3.jsonl",
      source_allowlist={"standardized_call_logs"},
      development_mode=_EXPLICIT_DEVELOPMENT_SERVICE_ADAPTER)
    return METRICS_ADAPTER

def current_dynamic_metrics(request:Request|None=None)->DynamicMetricsService:
    if request is None:
      return DynamicMetricsService(
        STORE.path,TenantScope("tenant_local_dev_v1","workspace_local_dev_v1"))
    principal=request_principal(request)
    return DynamicMetricsService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id))

def current_model_capability_aggregation(
    request:Request|None=None)->ModelCapabilityAggregationService:
    """Return the canonical, tenant-scoped capability projection service."""
    global MODEL_CAPABILITY_AGGREGATION
    if request is None:
      scope=TenantScope("tenant_local_dev_v1","workspace_local_dev_v1")
      if (MODEL_CAPABILITY_AGGREGATION is None
          or MODEL_CAPABILITY_AGGREGATION.path!=STORE.path):
        MODEL_CAPABILITY_AGGREGATION=ModelCapabilityAggregationService(
          STORE.path,scope,acceptance_evidence_root=ROOT/"evidence"/"final_acceptance")
      return MODEL_CAPABILITY_AGGREGATION
    principal=request_principal(request)
    return ModelCapabilityAggregationService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id),
      acceptance_evidence_root=ROOT/"evidence"/"final_acceptance")

def current_formal_agent_skill_runtime(request:Request)->FormalAgentSkillRuntime:
    """Create the request-scoped Formal Runtime and managed compatibility layer."""
    bindings=FormalAgentSkillBindings.from_services(
      metrics=current_incremental_metrics(request),
      confidence=current_statistical_confidence(request),
      attribution=current_scheduler_attribution(),
      sticky=current_sticky_routing(),
      circuit=current_circuit_breakers(),
      exploration=current_exploration_governance(),
      capability=current_capability_evidence(),
      cost_configuration=lambda:{
        "availability":"ready",
        "price_catalog":current_price_catalog(request).catalog_status("approved_price_api"),
        "network_called":False,
      },
      authorization_service=ENTERPRISE_HTTP.authorization,
    )
    def execute_instruction_skill(*,skill_id:str,skill:dict[str,Any],
                                  arguments:dict[str,Any],principal:Any=None)->dict[str,Any]:
      environment_id="china_uat"
      task=str(arguments.get("task") or "").strip()
      model_id=str(arguments.get("model_id") or "").strip()
      if not model_id:
        raise ValueError("execution_engine_not_configured")
      settings,credential_source,_sid,_meta=_resolved(request,environment_id)
      if not settings.api_key or credential_source=="none":
        raise ValueError("execution_engine_not_configured")
      language=str(arguments.get("output_language") or "中文")
      max_tokens=max(64,min(int(arguments.get("max_output_tokens") or 1200),4000))
      instructions=str(skill.get("instructions") or skill.get("description") or "")
      format_requirement=(
        "请直接完成用户任务并返回可交付的 Markdown 正文。正文必须包含标题、摘要、分节内容、"
        "实现步骤、注意事项和测试清单；不要返回生命周期元数据或调用回执。")
      if skill_id=="mcp-builder":
        format_requirement+=(" 必须分别覆盖：MCP目标、工具列表、Resources、Prompts、输入输出Schema、"
                             "Transport、鉴权、权限、错误处理、安全边界、实现步骤、测试清单。")
      payload={"model":model_id,"messages":[
        {"role":"system","content":f"以下为已发布 Skill 指令：\n{instructions[:32000]}\n\n{format_requirement}\n输出语言：{language}"},
        {"role":"user","content":task}],"stream":False,"temperature":0.2,
        "max_tokens":max_tokens}
      body={"environment_id":environment_id,"method":"POST","base_url":settings.base_url,
        "path":"/v1/chat/completions","query_params":[],"headers":[],
        "auth":{"method":"bearer","header_name":"Authorization","prefix":"Bearer"},
        "body":{"type":"json","value":payload},
        "content_type":"application/json","timeout_seconds":120,"stream":False,
        "model":model_id,"model_selection_mode":"specified","routing_policy":"skill_instruction_execution",
        "execution_limits":{"max_attempts_per_request":1},"request_type":"text",
        "traffic_class":"business","_tenant_id":str(getattr(request_principal(request),"tenant_id",None) or "tenant_local_dev_v1")}
      result=execute_workbench(body,settings,settings.api_key,connection_verified=True,
        call_logs=current_call_logs(request),
        capabilities=current_uat_output_capabilities(request).catalog("unified-routing"),
        transport=_fault_aware_transport(environment_id,body,WORKBENCH_TRANSPORT),
        performance_recorder=current_scheduler_overhead().recorder)
      raw=str(result.get("response_body") or "")
      content=""
      try:
        parsed=json.loads(raw)
        choices=parsed.get("choices") if isinstance(parsed,dict) else None
        if isinstance(choices,list) and choices:
          message=choices[0].get("message") if isinstance(choices[0],dict) else None
          content=str((message or {}).get("content") or "")
      except (json.JSONDecodeError,TypeError,ValueError):
        content=raw
      if result.get("execution_status")!="success":
        raise ValueError("instruction_skill_upstream_failed")
      if not content.strip():
        raise ValueError("instruction_skill_empty_result")
      return {"status":"success","business_executed":True,"result_type":"markdown",
        "content":content.strip(),"result_summary":content.strip()[:300],
        "input_summary":task[:240],
        "execution_steps":["读取已发布Skill版本","完成权限与输入Schema校验",
          "调用国内UAT模型执行任务","校验并脱敏输出","写入调用与审计记录"],
        "data_source":"domestic_uat_live_model_response",
        "execution_engine":"domestic_uat_chat_completions","model_id":result.get("actual_model") or model_id,
        "host_id":"local-formal-runtime","request_id":result.get("request_id"),
        "response_id":result.get("response_id"),"decision_id":result.get("decision_id"),
        "latency_ms":result.get("total_latency_ms"),"input_tokens":result.get("input_tokens"),
        "output_tokens":result.get("output_tokens"),"cost_amount":result.get("cost_amount"),
        "currency":result.get("currency"),"http_status":result.get("http_status"),
        "network_called":True,"write_performed":False}
    managed=FormalAgentSkillBusinessExecutor(
      call_logs=current_call_logs(request),config_review=current_config_review(request),
      circuit_breakers=current_circuit_breakers(),
      traffic_governance=current_traffic_change_governance(),
      validate_uat=lambda arguments:validate_request_workbench(
        str(arguments.get("environment_id") or "china_uat"),arguments,request),
      execute_uat=lambda arguments:execute_request_workbench(
        str(arguments.get("environment_id") or "china_uat"),arguments,request),
      instruction_executor=execute_instruction_skill)
    return FormalAgentSkillRuntime.build(
      bindings=bindings,audit=current_formal_agent_skill_audit(),
      registry=FORMAL_AGENT_SKILL_REGISTRY,
      lifecycle=current_formal_agent_skill_lifecycle(request),
      managed_executor=managed)
app.include_router(build_collector_router(
  COLLECTOR_SERVICE,lambda:current_uat_store().usage_today()[0],current_runtime_settings))
app.include_router(build_collector_run_router(
  COLLECTOR_RUNS,COLLECTOR_SUPERVISOR,COLLECTOR_SERVICE,_secure_credential_request,
  lambda request:session_hash(request.cookies.get(COOKIE_NAME) or "anonymous-local-browser")))
app.include_router(build_realtime_log_sync_router(
  REALTIME_LOG_SYNC,REALTIME_SYNC_SUPERVISOR.launch,
  REALTIME_SYNC_SUPERVISOR.stop,_secure_credential_request,
  coordinator=LOG_SYNC_COORDINATOR))
app.include_router(build_persistent_session_router(
  PERSISTENT_SESSIONS,REALTIME_SYNC_SUPERVISOR.launch_pairing,
  REALTIME_SYNC_SUPERVISOR.launch_persistent,
  REALTIME_SYNC_SUPERVISOR.stop,_secure_credential_request,
  _secure_read_only_request,DOMESTIC_UAT_CHROME))
app.include_router(build_shadow_sync_router(SHADOW_SYNC,_secure_credential_request))
app.include_router(build_sticky_routing_router(
  current_sticky_routing,_secure_credential_request,
  lambda request,environment_id:(
    load_platform_environments().get(environment_id) is not None
    and _resolved(request,environment_id)[1] in {"session","environment"})))
app.include_router(build_formal_agent_skill_router(
  FORMAL_AGENT_SKILL_REGISTRY,current_formal_agent_skill_audit,
  {"status":"ready"},_secure_read_only_request,
  runtime=current_formal_agent_skill_runtime,
  secure_mutation_request=_secure_credential_request,
  authorization_service=ENTERPRISE_HTTP.authorization,
  principal_resolver=request_principal))
app.include_router(build_formal_agent_skill_lifecycle_router(
  current_formal_agent_skill_lifecycle,current_formal_agent_skill_runtime,
  _secure_read_only_request,_secure_credential_request,request_principal,
  current_agent_skill_host_service))
@asynccontextmanager
async def _application_lifespan(_application:FastAPI):
    refresh_task=None
    price_sync_task=None
    lease_reconcile_task=None
    shadow_sync_task=None
    historical_path=Path(os.getenv(
      "HISTORICAL_UAT_CSV_PATH",r"E:\lx\调用日志_20260730_172539.csv"))
    try:
      # Never restart paid traffic implicitly. Any orphaned worker lease from
      # a prior process is closed as an explicit interrupted run.
      await asyncio.to_thread(
        ContinuousProbeExecutionService(STORE.path).reconcile,
        stale_after_seconds=30)
    except Exception:
      pass
    if historical_path.is_file():
      await asyncio.to_thread(
        current_call_logs().initialize_historical_csv,historical_path)
    if os.getenv("METRICS_BACKGROUND_REFRESH_ENABLED","true").lower()=="true":
      refresh_seconds=max(10,int(os.getenv("METRICS_BACKGROUND_REFRESH_SECONDS","60")))
      async def refresh_metrics():
        while True:
          try:
            await asyncio.to_thread(current_metrics_adapter().synchronize_safely)
            await asyncio.to_thread(
              current_dynamic_metrics().ensure,current_metrics_adapter())
            await asyncio.to_thread(
              current_model_capability_aggregation().ensure,None,"china_uat")
            await asyncio.to_thread(current_call_logs().expire_stale_running)
          except Exception:
            pass
          await asyncio.sleep(refresh_seconds)
      refresh_task=asyncio.create_task(refresh_metrics())
    if os.getenv("PRICE_BACKGROUND_SYNC_ENABLED","false").lower()=="true":
      price_sync_seconds=max(60,int(os.getenv("PRICE_BACKGROUND_SYNC_SECONDS","3600")))
      allow_price_network=(
        os.getenv("PRICE_SYNC_NETWORK_ENABLED","false").lower()=="true")
      async def refresh_prices():
        while True:
          try:
            await asyncio.to_thread(
              current_price_sync_supervisor().run_pending,
              allow_network=allow_price_network)
          except Exception:
            pass
          await asyncio.sleep(price_sync_seconds)
      price_sync_task=asyncio.create_task(refresh_prices())
    async def reconcile_persistent_leases():
      while True:
        await asyncio.sleep(max(
          10,min(60,PERSISTENT_SESSIONS.lease_timeout_seconds//2)))
        try:
          for exited in REALTIME_SYNC_SUPERVISOR.reap_finished():
            try:
              job_id=exited["identifier"]
              if exited["kind"]=="pairing":
                pairing=PERSISTENT_SESSIONS.get_pairing(job_id,private=True)
                if pairing["state"] in {
                    "browser_starting","waiting_for_manual_login",
                    "waiting_for_confirmation","confirmation_requested"}:
                  PERSISTENT_SESSIONS.update_pairing(
                    job_id,state="authentication_validation_failed",
                    browser_state="closed",
                    failure_code="persistent_pairing_worker_exited",
                    failure_summary=(
                      "登录配对浏览器已退出；请重新开始配对。"))
                continue
              job=REALTIME_LOG_SYNC.get_job(job_id,private=True)
              if job["state"] in ACTIVE_STATES:
                error_code=(
                  "persistent_worker_exited"
                  if exited["kind"]=="persistent"
                  else "log_sync_browser_worker_exited")
                REALTIME_LOG_SYNC.update_job(
                  job_id,state="failed",
                  stopped_at=datetime.now(timezone.utc).isoformat(),
                  browser_context_active=0,
                  error_code=error_code,
                  safe_error_message=(
                    "同步工作进程已意外退出；本地任务已安全终止。"))
            except Exception:
              pass
          await asyncio.to_thread(
            PERSISTENT_SESSIONS.reconcile_leases,"periodic_sweeper")
        except Exception:
          pass
    lease_reconcile_task=asyncio.create_task(reconcile_persistent_leases())
    if os.getenv("SHADOW_LOG_AUTO_SYNC_ENABLED","true").lower()=="true":
      async def ensure_shadow_log_sync():
        while True:
          try:
            await asyncio.to_thread(
              LOG_SYNC_COORDINATOR.ensure,trigger="backend_startup_or_interval")
          except Exception:
            # The public dashboard exposes the safe persisted failure state;
            # credentials and remote response bodies are never logged here.
            pass
          await asyncio.sleep(SHADOW_SYNC.freshness_seconds)
      shadow_sync_task=asyncio.create_task(ensure_shadow_log_sync())
    for job_id in PERSISTENT_SESSIONS.auto_resume_candidates():
      try:
        REALTIME_SYNC_SUPERVISOR.launch_persistent(job_id)
      except Exception:
        REALTIME_LOG_SYNC.update_job(
          job_id,state="failed",stopped_at=datetime.now(timezone.utc).isoformat(),
          error_code="persistent_headless_launch_failed",
          safe_error_message="自动恢复无界面同步失败。")
    try:
      yield
    finally:
      with CONTINUOUS_PROBE_EXECUTOR_LOCK:
        probe_executors=list(CONTINUOUS_PROBE_EXECUTORS.values())
        CONTINUOUS_PROBE_EXECUTORS.clear()
      for executor in probe_executors:
        try:
          await asyncio.to_thread(executor.shutdown,10)
        except Exception:
          pass
      if shadow_sync_task is not None:
        shadow_sync_task.cancel()
        try:
          await shadow_sync_task
        except asyncio.CancelledError:
          pass
      if lease_reconcile_task is not None:
        lease_reconcile_task.cancel()
        try:
          await lease_reconcile_task
        except asyncio.CancelledError:
          pass
      await asyncio.to_thread(PERSISTENT_SESSIONS.shutdown_active_jobs)
      if refresh_task is not None:
        refresh_task.cancel()
        try:
          await refresh_task
        except asyncio.CancelledError:
          pass
      if price_sync_task is not None:
        price_sync_task.cancel()
        try:
          await price_sync_task
        except asyncio.CancelledError:
          pass
      COLLECTOR_SUPERVISOR.cleanup()
      REALTIME_SYNC_SUPERVISOR.cleanup()

app.router.lifespan_context=_application_lifespan
class ReplayIn(BaseModel):
    strategy:str="confidence_aware_v2"
    mode:str="demo"
    environment_id:str="all"
class HistoricalReplayIn(BaseModel):
    environment_id:str="china_uat"
    occurred_from:str|None=None
    occurred_to:str|None=None
    model:str|None=None
    channel:str|None=None
    request_type:str|None=None
    baseline_strategy:str="actual_observed"
    candidate_strategy:str="latency_first"
    exact_only:bool=False
    limit:int=Field(default=500,ge=1,le=1000)
class ConfigIn(BaseModel):
    weights:dict[str,float]=Field(default_factory=dict)
    fallback:str|None=None
    timeout_ms:int|None=None
    strategy:str="confidence_aware_v2"
    baseline_strategy:str="confidence_aware_v2"
    mode:str="demo"
    environment_id:str="all"
class BugIn(BaseModel): request_id:str=""; decision_id:str=""; model:str=""; channel:str=""; request_type:str=""; http_status:int|None=None; message:str=""; context:str=""
class ExportIn(BaseModel): name:str; mode:str="demo"; environment_id:str="all"
class MetricsRefreshIn(BaseModel):
    explicit_confirmation:bool=False
    rebuild:bool=False
class CircuitEventIn(BaseModel):
    action:str=Field(pattern="^(success|failure)$")
    event_id:str|None=Field(default=None,max_length=128)
    probe_lease_id:str|None=Field(default=None,max_length=128)
class TrafficProposalIn(BaseModel):
    environment_id:str=Field(min_length=1,max_length=128)
    channel_id:str=Field(min_length=1,max_length=128)
    model_id:str=Field(min_length=1,max_length=256)
    change:dict[str,Any]
    rollout_percent:float=Field(gt=0)
    ttl_seconds:int=Field(gt=0)
    idempotency_key:str=Field(min_length=1,max_length=128)
class TrafficApprovalIn(BaseModel):
    approval_reference:str=Field(min_length=1,max_length=256)
    expected_revision:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=128)
class TrafficActivationIn(BaseModel):
    expected_revision:int=Field(ge=0)
    gate_results:dict[str,bool]
    idempotency_key:str=Field(min_length=1,max_length=128)
class TrafficTerminationIn(BaseModel):
    expected_revision:int=Field(ge=0)
    reason:str=Field(min_length=1,max_length=256)
    emergency:bool=False
class TrafficControlProposalIn(BaseModel):
    environment_mode:str=Field(pattern="^(sandbox|china_uat|production)$")
    source_model_id:str|None=Field(default=None,max_length=256)
    source_channel_id:str|None=Field(default=None,max_length=128)
    target_model_id:str=Field(min_length=1,max_length=256)
    target_channel_id:str=Field(min_length=1,max_length=128)
    source_policy_version:str|None=Field(default=None,max_length=256)
    target_policy_version:str=Field(min_length=1,max_length=256)
    rollout_percent:float=Field(gt=0,le=100)
    reason:str=Field(min_length=1,max_length=1000)
    approval_reference:str|None=Field(default=None,max_length=256)
    observation_seconds:int=Field(gt=0,le=86400)
    minimum_sample_count:int=Field(gt=0,le=1000000)
    stop_conditions:dict[str,float|int|bool]
    rollback_condition:str=Field(min_length=1,max_length=1000)
class TrafficControlApprovalIn(BaseModel):
    approval_reference:str|None=Field(default=None,max_length=256)
class TrafficControlRejectIn(BaseModel):
    reason:str=Field(min_length=1,max_length=1000)
class TrafficControlActivateIn(BaseModel):
    request_ids:list[str]=Field(default_factory=list,max_length=100)
    decision_ids:list[str]=Field(default_factory=list,max_length=100)
class TrafficControlAdjustIn(BaseModel):
    rollout_percent:float=Field(gt=0,le=100)
class TrafficControlRollbackIn(BaseModel):
    reason:str=Field(min_length=1,max_length=1000)
class MultimodalValidationIn(BaseModel):
    environment_id:str=Field(min_length=1,max_length=128)
    model_id:str=Field(min_length=1,max_length=256)
    channel_id:str=Field(min_length=1,max_length=128)
    subject_version:str=Field(min_length=1,max_length=128)
    request_type:str=Field(pattern="^(image|audio|video)$")
    mime_type:str=Field(min_length=1,max_length=128)
    input_bytes:int=Field(gt=0)
class ProbeTaskIn(BaseModel):
    name:str=Field(min_length=1,max_length=128)
    environment_id:str=Field(min_length=1,max_length=128)
    channel_id:str=Field(min_length=1,max_length=128)
    model_id:str=Field(min_length=1,max_length=256)
    request_template:str=Field(pattern="^(mock_success|mock_failure)$")
    frequency_seconds:int=Field(gt=0)
    max_requests:int=Field(gt=0)
    max_concurrency:int=Field(gt=0)
    deadline:datetime
    stop_condition:str=Field(pattern="^(request_limit|first_failure)$")
class ContinuousProbeCreateIn(BaseModel):
    name:str=Field(min_length=1,max_length=128)
    environment_id:str=Field(default="china_uat",pattern="^china_uat$")
    base_url:str=Field(default="https://api-uat.weimeta.cn",max_length=512)
    endpoint:str=Field(default="/v1/chat/completions",min_length=1,max_length=512)
    method:str=Field(default="POST",pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    models:list[str]=Field(min_length=1,max_length=50)
    stream:bool=False
    prompt:str=Field(default="请只回答：OK",min_length=1,max_length=2000)
    duration_seconds:int=Field(default=300,ge=60,le=3600)
    interval_seconds:float=Field(default=5.0,ge=0.5,le=300)
    max_concurrency:int=Field(default=1,ge=1,le=10)
    timeout_seconds:int=Field(default=30,ge=1,le=120)
    max_tokens:int=Field(default=16,ge=1,le=4096)
    temperature:float=Field(default=0,ge=0,le=2)
    max_requests:int|None=Field(default=None,ge=1,le=5000)
    rotation_mode:str=Field(default="round_robin",pattern="^(round_robin|sequential)$")
    stop_thresholds:dict[str,float|int|bool]=Field(default_factory=dict)
    fault_injection_enabled:bool=False
EXPORT_REPORT_NAMES={"渠道健康报告":"markdown","影子调度报告":"markdown","错误报告":"markdown","证据清单":"markdown"}

def records_for(mode:str,environment_id:str="all")->list[dict[str,Any]]:
    if mode=="demo": return demo_records()
    if mode=="uat":
      rows=uat_records(STORE)
      return rows if environment_id=="all" else [row for row in rows if row.get("environment_id","china_uat")==environment_id]
    raise HTTPException(400,detail={"code":"INVALID_MODE","message":"mode must be demo or uat"})

def analytical_metadata(mode:str,environment_id:str,rows:list[dict[str,Any]],
                        limitations:list[str],source_type:str|None=None,
                        configuration_version:str|None=None,
                        currency:str|None=None)->dict[str,Any]:
    source=source_type if source_type is not None else (
      "demo_mock" if mode=="demo" else
      str(rows[0].get("source_type")) if rows and rows[0].get("source_type") else None)
    evidence_ids=sorted({
      str(value) for row in rows
      for value in (row.get("evidence_sha256"),row.get("collection_id"),row.get("batch_id"))
      if value})
    updated=max((
      str(row.get("updated_at") or row.get("timestamp") or row.get("observed_at") or "")
      for row in rows),default="") or None
    return {
      "mode":mode,"environment_id":environment_id,"source_type":source,
      "is_mock":source=="demo_mock","sample_size":len(rows),
      "evidence_id":evidence_ids[0] if len(evidence_ids)==1 else None,
      "collection_ids":evidence_ids,"updated_at":updated,
      "configuration_version":configuration_version,
      "currency":currency,"limitations":limitations,
    }

@app.get("/api/v1/search")
def unified_search(q:str=Query(...,min_length=1,max_length=200),
                   environment_id:str=Query("all"),source_type:str|None=Query(None),
                   limit:int=Query(25,ge=1,le=100),cursor:str|None=Query(None)):
    try:
      service=UnifiedSearchService(STORE.path,STORE)
      return service.search(q,environment_id,source_type,limit,cursor)
    except ValueError as exc:
      raise HTTPException(400,detail={"code":str(exc),"message":"搜索参数无效。"})
def mapping_value(value:str|None)->dict[str,str]:
    if not value:return {}
    try:
      parsed=json.loads(value)
      if not isinstance(parsed,dict):raise ValueError
      return {str(k):str(v) for k,v in parsed.items()}
    except (json.JSONDecodeError,ValueError):
      raise HTTPException(400,detail={"code":"INVALID_MAPPING","message":"mapping must be a JSON object"})
async def process_upload(file:UploadFile,mapping:str|None,source_type:str,environment_id:str="china_uat")->dict[str,Any]:
    if environment_id not in {"china_uat","overseas"}:
      raise HTTPException(400,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    if source_type not in {"measured_uat","measured_unified_uat","backend_log_export","integration_test_fixture"}:
      raise HTTPException(400,detail={"code":"INVALID_SOURCE_TYPE","message":"unsupported source_type"})
    try:
      batch=import_preview(Path(file.filename or "upload").name,await file.read(),mapping_value(mapping),source_type)
      batch["environment_id"]=environment_id
      for row in batch.get("normalized_rows",[]):row["environment_id"]=environment_id
      for row in batch.get("normalized_preview",[]):row["environment_id"]=environment_id
      return batch
    except Exception as exc:raise HTTPException(400,detail={"code":"IMPORT_INVALID","message":str(exc)})

@app.get("/api/v1/system/status")
def status(): return {"status":"ok","mode":"local","external_execution_enabled":UatSettings.load().enabled,"network_calls":current_uat_store().usage_today()[0],"version":"1.2.0","database":"sqlite"}

def _local_dependency_snapshot()->dict[str,Any]:
    dependencies=[]
    try:
      with STORE.connect() as db:
        db.execute("SELECT 1").fetchone()
      dependencies.append({"dependency":"sqlite","status":"ready",
        "required_for_local_readiness":True})
    except Exception:
      dependencies.append({"dependency":"sqlite","status":"failed",
        "required_for_local_readiness":True,"reason":"database_unavailable"})
    for name,loader in (
      ("incremental_metrics",current_incremental_metrics),
      ("scheduler_overhead",current_scheduler_overhead),
      ("price_catalog",current_price_catalog),
      ("circuit_breaker",current_circuit_breakers),
      ("capability_evidence",current_capability_evidence),
    ):
      try:
        loader()
        dependencies.append({"dependency":name,"status":"ready",
          "required_for_local_readiness":True})
      except Exception:
        dependencies.append({"dependency":name,"status":"failed",
          "required_for_local_readiness":True,"reason":"local_dependency_unavailable"})
    dependencies.append({"dependency":"external_price_source",
      "status":"not_configured" if current_price_catalog().transport is None else "configured",
      "required_for_local_readiness":False})
    dependencies.append({"dependency":"domestic_uat_session",
      "status":"operator_authentication_required",
      "required_for_local_readiness":False})
    migration_status=str(ENTERPRISE_TENANT_MIGRATION.get("status") or "unknown")
    migration_ready=migration_status in {"applied","already_applied"}
    dependencies.append({"dependency":"required_database_migrations",
      "status":"ready" if migration_ready else "blocked",
      "required_for_local_readiness":True,
      **({"reason":str(ENTERPRISE_TENANT_MIGRATION.get("error_code") or
                       "required_migration_incomplete")} if not migration_ready else {})})
    worker_heartbeat=_worker_heartbeat_snapshot()
    dependencies.append({"dependency":"local_worker",
      "status":worker_heartbeat["status"],
      "required_for_local_readiness":worker_heartbeat["required"],
      **({"reason":worker_heartbeat["reason"]}
         if worker_heartbeat.get("reason") else {})})
    ready=all(item["status"]=="ready" for item in dependencies
              if item["required_for_local_readiness"])
    return {"status":"ready" if ready else "not_ready",
      "dependencies":dependencies,"network_checked":False,
      "credentials_exposed":False}

def _worker_heartbeat_snapshot()->dict[str,Any]:
    required=os.getenv("ROUTING_CONSOLE_REQUIRE_WORKER","false").lower()=="true"
    state_dir=Path(os.getenv("ROUTING_CONSOLE_STATE_DIR") or ROOT/"runtime"/"local-console")
    heartbeat=state_dir/"run"/"worker-heartbeat.json"
    if not heartbeat.exists():
      return {"status":"not_running" if required else "not_required",
        "required":required,"reason":"worker_heartbeat_missing" if required else None}
    try:
      payload=json.loads(heartbeat.read_text(encoding="utf-8"))
      updated=datetime.fromisoformat(str(payload["updated_at"]).replace("Z","+00:00"))
      age=max(0.0,(datetime.now(timezone.utc)-updated.astimezone(timezone.utc)).total_seconds())
      if age>20:
        return {"status":"stale","required":required,
          "reason":"worker_heartbeat_stale","age_seconds":round(age,3)}
      return {"status":"ready","required":required,"age_seconds":round(age,3)}
    except Exception:
      return {"status":"invalid","required":required,
        "reason":"worker_heartbeat_invalid"}

@app.get("/api/v1/system/readiness")
def readiness():
    result=_local_dependency_snapshot()
    return {**result,"readiness_scope":"local_stage_preconditions"}

@app.get("/health",include_in_schema=False)
def root_health(request:Request):
    """Unauthenticated liveness probe for the local process supervisor."""
    if "text/html" in request.headers.get("accept","").casefold():
      return frontend_application("health")
    return {"status":"ok","service":"routing-quality-console","version":"1.2.0"}

@app.get("/ready",include_in_schema=False)
def root_readiness():
    """Readiness probe including migrations, database and worker heartbeat."""
    result=_local_dependency_snapshot()
    migration_status=str(ENTERPRISE_TENANT_MIGRATION.get("status") or "unknown")
    result["migration_status"]="ready" if migration_status in {"applied","already_applied"} else "blocked"
    result["migration_details"]={"required_migration":migration_status,
      "blocked_migration":None if result["migration_status"]=="ready" else
        ENTERPRISE_TENANT_MIGRATION.get("error_code"),
      "remediation":None if result["migration_status"]=="ready" else
        "检查企业租户迁移清单中的未分类表或约束失败。"}
    result["warnings"]=[item for item in result["dependencies"]
      if not item["required_for_local_readiness"] and item["status"] not in {"ready","configured"}]
    result["readiness_scope"]="local_console"
    return JSONResponse(result,status_code=200 if result["status"]=="ready" else 503)

@app.get("/api/v1/system/dependencies")
def dependencies(): return _local_dependency_snapshot()

@app.get("/api/v1/metrics/snapshots")
def metric_snapshots(
    request:Request,
    window:str|None=Query(default=None),
    environment_id:str|None=Query(default=None),
    model:str|None=Query(default=None),
    channel:str|None=Query(default=None),
    limit:int=Query(default=100,ge=1,le=500),
    offset:int=Query(default=0,ge=0),
):
    """Read versioned materialized snapshots without rescanning raw evidence."""
    try:
      result=current_incremental_metrics(request).list_snapshots(
        window=window,environment_id=environment_id,model=model,channel=channel,
        limit=limit,offset=offset)
      result["refresh_runtime"]=current_metrics_adapter(request).status()
      result["source_contract"]="immutable_local_evidence_projection"
      result["network_called"]=False
      return result
    except MetricsError as exc:
      raise HTTPException(400,detail={
        "code":str(exc),"message":"增量指标查询参数无效。"}) from exc

@app.post("/api/v1/metrics/refresh")
def refresh_metric_snapshots(body:MetricsRefreshIn,request:Request):
    _secure_credential_request(request)
    if not body.explicit_confirmation:
      raise HTTPException(400,detail={
        "code":"EXPLICIT_CONFIRMATION_REQUIRED",
        "message":"刷新本地派生指标前必须明确确认。"})
    try:
      return current_metrics_adapter(request).synchronize_safely(rebuild=body.rebuild)
    except MetricsSourceError as exc:
      raise HTTPException(409,detail={
        "code":str(exc),
        "message":"本地不可变证据完整性校验失败，未刷新指标。"}) from exc
    except MetricsError as exc:
      raise HTTPException(409,detail={
        "code":str(exc),"message":"本地指标证据存在冲突，未刷新指标。"}) from exc
    except Exception as exc:
      raise HTTPException(500,detail={
        "code":"metric_refresh_failed",
        "message":"本地指标刷新失败；未执行任何外部请求。"}) from exc

@app.post("/api/v1/dynamic-metrics/ensure")
def ensure_dynamic_metrics(request:Request):
    """Idempotent read-only refresh, independent from the UAT execution gate."""
    try:
      return current_dynamic_metrics(request).ensure(current_metrics_adapter(request))
    except Exception as exc:
      raise HTTPException(500,detail={"code":"dynamic_metrics_refresh_failed",
        "message":"动态指标聚合失败，原始调用日志未受影响。"}) from exc

@app.get("/api/v1/dynamic-metrics/jobs/{job_id}")
def dynamic_metric_job(job_id:str,request:Request):
    result=current_dynamic_metrics(request).job(job_id)
    if result is None:
      raise HTTPException(404,detail={"code":"dynamic_metric_job_not_found",
        "message":"未找到该聚合任务。"})
    return result

@app.get("/api/v1/dynamic-metrics/overview")
def dynamic_metrics_overview(
    request:Request,
    environment_id:str=Query(default="china_uat"),
    window:str=Query(default="24h"),
    model_id:str|None=Query(default=None),
    traffic_class:str=Query(default="business"),
    source_type:str|None=Query(default=None),
):
    try:
      return current_dynamic_metrics(request).overview(
        environment_id=environment_id,window=window,model_id=model_id,
        traffic_class=traffic_class,source_type=source_type)
    except ValueError as exc:
      raise HTTPException(400,detail={"code":str(exc),"message":"动态指标查询参数无效。"}) from exc

@app.get("/api/v1/statistical-confidence/snapshots")
def statistical_confidence_snapshots(
    request:Request,
    metric_snapshot_id:str|None=Query(default=None),
    confidence_version:str=Query(default="weighted_wilson_v1"),
    limit:int=Query(default=100,ge=1,le=500),
    offset:int=Query(default=0,ge=0),
):
    """Read versioned confidence results without external access."""
    try:
      return current_statistical_confidence(request).list_snapshots(
        metric_snapshot_id=metric_snapshot_id,
        confidence_version=confidence_version,
        limit=limit,
        offset=offset)
    except ValueError as exc:
      raise HTTPException(400,detail={
        "code":str(exc),
        "message":"Statistical confidence query parameters are invalid."}) from exc

@app.get("/api/v1/scheduler-attribution/chains")
def scheduler_attribution_chains(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_scheduler_attribution().list_chains(limit=limit)

@app.get("/api/v1/scheduler-attribution/chains/{decision_id}")
def scheduler_attribution_chain(decision_id:str,request:Request):
    _secure_read_only_request(request)
    result=current_scheduler_attribution().chain(decision_id)
    if result["scheduler_decision"] is None and not result["execution_events"]:
      raise HTTPException(404,detail={
        "code":"scheduler_attribution_not_found",
        "message":"未找到该 Scheduler 决策的归因证据。"})
    return result

@app.get("/api/v1/scheduler/decisions/{decision_id}/reconstruction")
def scheduler_decision_reconstruction(decision_id:str,request:Request):
    result=current_decision_reconstruction().reconstruct(decision_id)
    if result.get("reason")=="decision_not_found":
      raise HTTPException(404,detail={
        "error_code":"decision_not_found",
        "message":"未找到该决策的可重建审计记录。"})
    return result

@app.get("/api/v1/scheduler/overhead")
def scheduler_overhead(
    request:Request,window_minutes:int=Query(60,ge=1,le=10080)):
    return current_scheduler_overhead().status(window_minutes=window_minutes)

@app.get("/api/v1/observability/scheduler-performance")
def scheduler_performance(
    request:Request,environment_id:str=Query("china_uat",min_length=1,max_length=64),
    time_range:str=Query("24h",pattern="^(5m|1h|24h|7d|custom)$"),
    start:str|None=None,end:str|None=None,strategy_id:str|None=None,
    model_id:str|None=None,traffic_class:str=Query("business",pattern="^(business|probe)$"),
    stream:bool|None=None):
    minutes={"5m":5,"1h":60,"24h":1440,"7d":10080,"custom":1440}[time_range]
    if time_range=="custom" and (not start or not end):
      raise HTTPException(400,detail={"code":"custom_time_range_required",
        "message":"自定义时间范围必须同时提供开始和结束时间。"})
    return current_scheduler_overhead().performance(environment_id=environment_id,
      window_minutes=minutes,strategy_id=strategy_id,model_id=model_id,
      traffic_class=traffic_class,stream=stream,start=start,end=end)

@app.get("/api/v1/prices/status")
def price_catalog_status(
    request:Request,source_id:str=Query("approved_price_api",min_length=1,max_length=128)):
    schedule=current_price_sync_supervisor(request).status(source_id)
    catalog=current_price_catalog(request)
    model_catalog_loader=getattr(catalog,"model_catalog_status",None)
    return {
      **catalog.catalog_status(source_id),
      "model_catalog":(model_catalog_loader() if callable(model_catalog_loader) else {
        "status":"not_configured","current_version":None,"model_count":0,
        "confirmed_count":0,"pending_count":0}),
      "policy_version":catalog.policy["policy_version"],
      "automatic_sync_state":schedule["state"],
      "schedule":schedule,
      "source_adapter_configured":catalog.transport is not None,
      "scheduler_consumption":"fresh_exact_scope_only",
    }

@app.get("/api/v1/prices/audit")
def price_catalog_audit(request:Request,limit:int=Query(100,ge=1,le=500)):
    return {"status":"ready","items":current_price_catalog(request).audit(limit),
      "network_called":False}

@app.get("/api/v1/circuit-breakers/status")
def circuit_breaker_status(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_circuit_breakers().list_states(limit=limit)

@app.get("/api/v1/acceptance-runs/active")
def acceptance_run_active(environment_id:str="china_uat"):
    return current_acceptance_runs().active_run(environment_id) or JSONResponse(
      status_code=404,content={"code":"acceptance_run_not_found"})

@app.get("/api/v1/acceptance-runs/{acceptance_run_id}")
def acceptance_run_detail(acceptance_run_id:str):
    item=current_acceptance_runs().get_run(acceptance_run_id)
    if not item:raise HTTPException(404,detail={"code":"acceptance_run_not_found"})
    return {**item,"evidence_counts":current_acceptance_runs().evidence_counts(acceptance_run_id)}

@app.get("/api/v1/acceptance-runs/{acceptance_run_id}/live-summary")
def acceptance_run_live_summary(acceptance_run_id:str):
    try:return current_acceptance_runs().live_summary(acceptance_run_id)
    except AcceptanceRunError as exc:
      raise HTTPException(404,detail={"code":str(exc)}) from exc

@app.get("/api/v1/uat-fault-injection/rules")
def uat_fault_rules(acceptance_run_id:str|None=None):
    return {"items":current_uat_fault_injection().list(acceptance_run_id=acceptance_run_id)}

@app.post("/api/v1/uat-fault-injection/rules")
def uat_fault_create(body:dict[str,Any]):
    try:return current_uat_fault_injection().create(body)
    except UatFaultInjectionError as exc:
      raise HTTPException(400,detail={"code":str(exc)}) from exc

@app.post("/api/v1/uat-fault-injection/rules/{fault_id}/enable")
def uat_fault_enable(fault_id:str):
    try:return current_uat_fault_injection().enable(fault_id)
    except UatFaultInjectionError as exc:
      raise HTTPException(400,detail={"code":str(exc)}) from exc

@app.post("/api/v1/uat-fault-injection/rules/{fault_id}/disable")
def uat_fault_disable(fault_id:str):
    try:return current_uat_fault_injection().disable(fault_id)
    except UatFaultInjectionError as exc:
      raise HTTPException(400,detail={"code":str(exc)}) from exc

@app.get("/api/v1/uat-fault-injection/audit")
def uat_fault_audit(acceptance_run_id:str|None=None):
    return {"items":current_uat_fault_injection().list_audit(acceptance_run_id=acceptance_run_id)}

@app.get("/api/v1/circuit-breakers/{circuit_id}/transitions")
def circuit_breaker_transitions(circuit_id:str,request:Request):
    _secure_read_only_request(request)
    try:
      state=current_circuit_breakers().get_state(circuit_id)
      return {"status":"ready","state":state,
        "items":current_circuit_breakers().list_transition_audits(circuit_id),
        "network_called":False}
    except CircuitBreakerError as exc:
      raise HTTPException(400,detail={
        "code":str(exc),"message":"熔断器标识或持久化状态无效。"}) from exc

@app.post("/api/v1/circuit-breakers/{circuit_id}/events")
def circuit_breaker_event(circuit_id:str,body:CircuitEventIn,request:Request):
    """Record a controlled local outcome; this endpoint performs no provider call."""
    _secure_credential_request(request)
    event_id=body.event_id or f"UI-CB-{uuid.uuid4().hex}"
    try:
      service=current_circuit_breakers()
      result=(service.record_success(circuit_id,event_id,body.probe_lease_id)
              if body.action=="success" else
              service.record_failure(circuit_id,event_id,body.probe_lease_id))
      return {"status":"accepted","result":result,"network_called":False}
    except CircuitBreakerError as exc:
      raise HTTPException(409,detail={
        "code":str(exc),"message":"熔断状态事件未被接受。",
        "network_called":False}) from exc

@app.get("/api/v1/exploration-governance/status")
def exploration_governance_status(request:Request):
    _secure_read_only_request(request)
    return current_exploration_governance().status()

@app.get("/api/v1/exploration-governance/audit")
def exploration_governance_audit(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_exploration_governance().monitoring_audit(limit)

@app.get("/api/v1/capability-evidence")
def capability_evidence(
    request:Request,subject_id:str|None=Query(default=None),
    limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    service=current_capability_evidence()
    return service.monitoring_status(subject_id=subject_id,limit=limit)

@app.get("/api/v1/capability-evidence/audit")
def capability_evidence_audit(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_capability_evidence().monitoring_audit(limit)

@app.get("/api/v1/traffic-change-governance/status")
def traffic_change_governance_status(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_traffic_change_governance().monitoring_status(limit)

@app.get("/api/v1/traffic-change-governance/audit")
def traffic_change_governance_audit(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_traffic_change_governance().monitoring_audit(limit)

def _safe_workflow_actor(request:Request)->str:
    try:return request_principal(request).principal_id
    except Exception:return "local-console-operator"

@app.post("/api/v1/traffic-change-governance/proposals")
def traffic_change_proposal(body:TrafficProposalIn,request:Request):
    _secure_credential_request(request)
    try:
      return current_traffic_change_governance().propose(
        environment_id=body.environment_id,channel_id=body.channel_id,
        model_id=body.model_id,change=body.change,
        proposer_id=_safe_workflow_actor(request),actor_role="proposer",
        rollout_percent=body.rollout_percent,ttl_seconds=body.ttl_seconds,
        idempotency_key=body.idempotency_key)
    except TrafficChangeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"流量变更提议未通过本地治理策略。","network_called":False}) from exc

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/approve")
def traffic_change_approve(proposal_id:str,body:TrafficApprovalIn,request:Request):
    _secure_credential_request(request)
    approval_digest=hashlib.sha256(body.approval_reference.encode("utf-8")).hexdigest()[:24]
    try:
      return current_traffic_change_governance().approve(
        proposal_id,approver_id=f"approval:{approval_digest}",actor_role="approver",
        expected_revision=body.expected_revision,idempotency_key=body.idempotency_key)
    except TrafficChangeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"流量变更审批未被接受。","network_called":False}) from exc

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/activate")
def traffic_change_activate(proposal_id:str,body:TrafficActivationIn,request:Request):
    _secure_credential_request(request)
    try:
      return current_traffic_change_governance().activate(
        proposal_id,operator_id=_safe_workflow_actor(request),actor_role="operator",
        expected_revision=body.expected_revision,gate_results=body.gate_results,
        idempotency_key=body.idempotency_key)
    except TrafficChangeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"沙箱流量变更未激活；请检查门禁、审批和 Kill Switch。",
        "network_called":False}) from exc

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/terminate")
def traffic_change_terminate(proposal_id:str,body:TrafficTerminationIn,request:Request):
    _secure_credential_request(request)
    try:
      return current_traffic_change_governance().terminate(
        proposal_id,actor_id=_safe_workflow_actor(request),
        actor_role="emergency" if body.emergency else "operator",
        expected_revision=body.expected_revision,reason=body.reason,
        emergency=body.emergency)
    except TrafficChangeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"流量变更停止或回滚未被接受。","network_called":False}) from exc

def _traffic_control_result(operation):
    try:
      return operation()
    except TrafficChangeGovernanceError as exc:
      messages={
        "production_adapter_not_configured":"生产执行适配器尚未配置。",
        "invalid_control_proposal":"变更信息不完整，请检查目标、观察窗口和停止条件。",
        "impact_evidence_insufficient":"目标模型与渠道证据不足，国内 UAT 灰度暂不能启动。",
        "invalid_control_state_transition":"当前状态不允许执行此操作。",
        "approval_reference_required":"生产变更必须提供有效审批引用。",
        "proposal_not_found":"未找到该流量变更。",
      }
      raise HTTPException(409,detail={"code":str(exc),
        "message":messages.get(str(exc),"流量变更操作未通过治理校验。")}) from exc

@app.get("/api/v1/traffic-change-governance/current")
def traffic_control_current(request:Request):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_current(
      principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/channels")
def traffic_control_channels(request:Request):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_channels(
      principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/impact")
def traffic_control_impact(request:Request,target_model_id:str=Query(min_length=1),
                           target_channel_id:str=Query(min_length=1)):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_impact(
      target_model_id=target_model_id,target_channel_id=target_channel_id,
      principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/proposals")
def traffic_control_proposals(request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_list(
      limit,principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/control-proposals")
def create_traffic_control_proposal(body:TrafficControlProposalIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().create_control_proposal(
      body.model_dump(),actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/proposals/{proposal_id}")
def traffic_control_proposal_detail(proposal_id:str,request:Request):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_get(
      proposal_id,principal=request_principal(request)))

@app.delete("/api/v1/traffic-change-governance/proposals/{proposal_id}")
def delete_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_delete(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/validate")
def validate_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_validate(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/submit")
def submit_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_submit(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/control-approve")
def approve_traffic_control_proposal(proposal_id:str,body:TrafficControlApprovalIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_approve(
      proposal_id,actor_id=_safe_workflow_actor(request),approval_reference=body.approval_reference,
      principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/reject")
def reject_traffic_control_proposal(proposal_id:str,body:TrafficControlRejectIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_reject(
      proposal_id,actor_id=_safe_workflow_actor(request),reason=body.reason,
      principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/control-activate")
def activate_traffic_control_proposal(proposal_id:str,body:TrafficControlActivateIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_activate(
      proposal_id,actor_id=_safe_workflow_actor(request),request_ids=body.request_ids,
      decision_ids=body.decision_ids,principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/pause")
def pause_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_pause(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/resume")
def resume_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_resume(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/adjust")
def adjust_traffic_control_proposal(proposal_id:str,body:TrafficControlAdjustIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_adjust(
      proposal_id,actor_id=_safe_workflow_actor(request),rollout_percent=body.rollout_percent,
      principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/rollback")
def rollback_traffic_control_proposal(proposal_id:str,body:TrafficControlRollbackIn,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_rollback(
      proposal_id,actor_id=_safe_workflow_actor(request),reason=body.reason,
      principal=request_principal(request)))

@app.post("/api/v1/traffic-change-governance/proposals/{proposal_id}/complete")
def complete_traffic_control_proposal(proposal_id:str,request:Request):
    _secure_credential_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_complete(
      proposal_id,actor_id=_safe_workflow_actor(request),principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/proposals/{proposal_id}/metrics")
def traffic_control_metrics(proposal_id:str,request:Request):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_metrics(
      proposal_id,principal=request_principal(request)))

@app.get("/api/v1/traffic-change-governance/proposals/{proposal_id}/audit")
def traffic_control_audit(proposal_id:str,request:Request):
    _secure_read_only_request(request)
    return _traffic_control_result(lambda:current_traffic_change_governance().control_audit(
      proposal_id,principal=request_principal(request)))

@app.post("/api/v1/multimodal/validate")
def validate_multimodal(body:MultimodalValidationIn,request:Request):
    _secure_credential_request(request)
    try:
      return current_capability_evidence().validate_multimodal_request(
        environment_id=body.environment_id,model_id=body.model_id,
        channel_id=body.channel_id,subject_version=body.subject_version,
        request_type=body.request_type,mime_type=body.mime_type,
        input_bytes=body.input_bytes)
    except CapabilityEvidenceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"多模态请求未通过能力证据校验。","network_called":False}) from exc

@app.get("/api/v1/probe-governance/status")
def probe_governance_status(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_probe_governance().monitoring_status(limit)

@app.get("/api/v1/probe-governance/tasks")
def probe_tasks(request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_probe_governance().list_tasks(
      limit,principal=request_principal(request))

@app.post("/api/v1/probe-governance/tasks")
def create_probe_task(body:ProbeTaskIn,request:Request):
    _secure_credential_request(request)
    try:
      return current_probe_governance().create_task(
        **body.model_dump(),principal=request_principal(request))
    except ProbeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"探测任务超出本地安全边界或参数无效。",
        "network_called":False}) from exc

@app.post("/api/v1/probe-governance/tasks/{task_id}/start")
def start_probe_task(task_id:str,request:Request):
    _secure_credential_request(request)
    try:
      return current_probe_governance().start_task(
        task_id,principal=request_principal(request))
    except ProbeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"探测任务未能启动。","network_called":False}) from exc

@app.post("/api/v1/probe-governance/tasks/{task_id}/stop")
def stop_probe_task(task_id:str,request:Request):
    _secure_credential_request(request)
    try:
      return current_probe_governance().stop_task(
        task_id,principal=request_principal(request))
    except ProbeGovernanceError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"探测任务未能停止。","network_called":False}) from exc

@app.get("/api/v1/probe-governance/audit")
def probe_governance_audit(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_probe_governance().monitoring_audit(limit)

@app.get("/api/v1/strategy-effects/latest")
def latest_strategy_effect(request:Request):
    _secure_read_only_request(request)
    item=current_live_acceptance(request).latest_strategy_run()
    return {"status":"ready" if item else "no_real_data","item":item}

@app.get("/api/v1/strategy-effects/runs/{run_id}")
def strategy_effect_run(run_id:str,request:Request):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).strategy_run(run_id)
    except KeyError as exc:raise HTTPException(404,detail={"code":str(exc),
      "message":"未找到该策略效果验收运行。"}) from exc

@app.get("/api/v1/continuous-probes/latest")
def latest_continuous_probe(request:Request):
    _secure_read_only_request(request)
    item=current_live_acceptance(request).latest_probe_run()
    return {"status":"ready" if item else "no_real_data","item":item}

@app.get("/api/v1/continuous-probes/runs/{run_id}")
def continuous_probe_run(run_id:str,request:Request):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).probe_run(run_id)
    except KeyError as exc:raise HTTPException(404,detail={"code":str(exc),
      "message":"未找到该持续探测运行。"}) from exc

@app.post("/api/v1/continuous-probes/runs/{run_id}/stop")
def stop_continuous_probe(run_id:str,request:Request):
    _secure_credential_request(request)
    service=current_live_acceptance(request)
    try:run=service.probe_run(run_id)
    except KeyError as exc:raise HTTPException(404,detail={"code":str(exc),
      "message":"未找到该持续探测运行。"}) from exc
    if run["status"] not in {"RUNNING","THROTTLED","PAUSED","COOLDOWN","HALF_OPEN"}:
      raise HTTPException(409,detail={"code":"probe_run_not_stoppable",
        "message":"该探测任务已经结束。"})
    service.update_probe(run_id,stop_requested=1,status="STOP_REQUESTED")
    service.probe_event(run_id,"manual_stop_requested","local_control_event",
      {"operator_id":request_principal(request).principal_id})
    return service.probe_run(run_id)

def _probe_executor_key(request:Request,run_id:str)->tuple[str,str,str]:
    principal=request_principal(request)
    return principal.tenant_id,principal.workspace_id,run_id

def _probe_executor(request:Request,run_id:str)->ContinuousProbeExecutionService:
    key=_probe_executor_key(request,run_id)
    with CONTINUOUS_PROBE_EXECUTOR_LOCK:
      executor=CONTINUOUS_PROBE_EXECUTORS.get(key)
      if executor is None:
        principal=request_principal(request)
        executor=ContinuousProbeExecutionService(
          STORE.path,TenantScope(principal.tenant_id,principal.workspace_id),
          credential_provider=lambda: "",transport=WORKBENCH_TRANSPORT)
        CONTINUOUS_PROBE_EXECUTORS[key]=executor
      return executor

def _probe_http_error(exc:Exception)->HTTPException:
    code=getattr(exc,"code",str(exc))
    if code=="probe_run_not_found":status=404
    elif code in {"uat_secure_credential_unavailable","china_uat_environment_disabled"}:status=409
    elif "invalid" in code or "out_of_range" in code or code.endswith("_required"):status=422
    elif code in {"probe_worker_start_timeout","probe_worker_lease_lost"}:status=503
    else:status=409
    messages={
      "uat_secure_credential_unavailable":"当前国内 UAT 安全会话中没有可用 API Key。",
      "china_uat_environment_disabled":"国内 UAT 当前未开启，不能启动新的真实探测。",
      "probe_worker_not_available_after_restart":"服务重启后不会自动恢复付费探测；请复制配置创建新任务。",
      "probe_pause_invalid_state":"当前状态不能暂停。",
      "probe_resume_invalid_state":"当前状态不能继续。",
      "probe_run_not_startable":"探测任务已被其他执行器接管或状态不允许启动。",
    }
    return HTTPException(status,detail={"code":code,
      "message":messages.get(code,"探测任务操作未完成，请检查当前状态和配置。")})

@app.get("/api/v1/probes")
def list_continuous_probes(request:Request,q:str|None=Query(None,max_length=128),
    status:str|None=Query(None,max_length=32),started_from:str|None=None,
    started_to:str|None=None,limit:int=Query(50,ge=1,le=200),
    offset:int=Query(0,ge=0)):
    _secure_read_only_request(request)
    return current_live_acceptance(request).list_probe_runs(
      query=q,status=status,started_from=started_from,started_to=started_to,
      limit=limit,offset=offset)

@app.post("/api/v1/probes")
def create_continuous_probe(body:ContinuousProbeCreateIn,request:Request):
    _secure_credential_request(request)
    principal=request_principal(request)
    if body.environment_id!="china_uat" or body.base_url.rstrip("/")!="https://api-uat.weimeta.cn":
      raise HTTPException(422,detail={"code":"probe_environment_not_allowed",
        "message":"持续探测只允许访问国内 UAT。"})
    settings,_,_,_=_resolved(request,"china_uat")
    if not settings.api_key:
      raise HTTPException(409,detail={"code":"uat_secure_credential_unavailable",
        "message":"当前国内 UAT 安全会话中没有可用 API Key。"})
    catalog,_=MODEL_CATALOG.fetch(
      settings.api_key,MODELS_TRANSPORT,min(settings.timeout_seconds,10),False,
      environment_id="china_uat",environment_name="国内 UAT",
      models_url="https://api-uat.weimeta.cn/v1/models")
    available={str(item.get("id") or item.get("model_id") or "")
      for item in list(catalog.get("models") or []) if isinstance(item,dict)}
    missing=[model for model in body.models if model not in available]
    if catalog.get("status")!="ready" or missing:
      raise HTTPException(422,detail={"code":"probe_models_not_in_live_catalog",
        "message":"所选模型不在当前真实 UAT 模型目录中。","models":missing})
    configuration=body.model_dump(exclude={"name","base_url","environment_id"})
    configuration.update({"environment_id":"china_uat",
      "request_interval_seconds":body.interval_seconds,
      "fault_injection_enabled":bool(body.fault_injection_enabled)})
    current_call_logs(request)  # Ensure the unified probe ledger is migrated.
    with sqlite3.connect(STORE.path) as db:
      watermark=int(db.execute(
        "SELECT COALESCE(MAX(cursor_id),0) FROM standardized_call_logs").fetchone()[0])
    service=current_live_acceptance(request)
    run=service.create_probe_run({"task_name":body.name,
      "environment_id":"china_uat","git_commit":os.getenv("GIT_COMMIT","unknown"),
      "database_watermark":watermark,"configuration":configuration,
      "audit_id":"AUD-PROBE-"+uuid.uuid4().hex.upper(),
      "created_by":principal.principal_id})
    run_id=str(run["probe_run_id"])
    executor=ContinuousProbeExecutionService(
      STORE.path,TenantScope(principal.tenant_id,principal.workspace_id),
      credential_provider=lambda:settings.api_key,transport=WORKBENCH_TRANSPORT)
    with CONTINUOUS_PROBE_EXECUTOR_LOCK:
      CONTINUOUS_PROBE_EXECUTORS[_probe_executor_key(request,run_id)]=executor
    try:
      executor.start(run_id,configuration,actor_id=principal.principal_id)
      # The worker already owns its in-memory copy; drop the closure retaining
      # the credential immediately after ownership is acknowledged.
      executor._credential_provider=lambda:""
    except ContinuousProbeExecutionError as exc:
      try:
        if service.probe_run(run_id,include_events=False)["status"]!="FAILED":
          service.transition_probe_run(run_id,"fail",reason=exc.code,
            operator_id=principal.principal_id)
      except (RuntimeError,KeyError):
        pass
      raise _probe_http_error(exc) from exc
    return service.probe_run(run_id)

@app.get("/api/v1/probes/{run_id}")
def get_continuous_probe(run_id:str,request:Request):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).probe_run(run_id,include_events=False)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc

@app.post("/api/v1/probes/{run_id}/pause")
def pause_continuous_probe(run_id:str,request:Request):
    _secure_credential_request(request)
    try:return _probe_executor(request,run_id).pause(
      run_id,actor_id=request_principal(request).principal_id)
    except ContinuousProbeExecutionError as exc:raise _probe_http_error(exc) from exc

@app.post("/api/v1/probes/{run_id}/resume")
def resume_continuous_probe(run_id:str,request:Request):
    _secure_credential_request(request)
    try:return _probe_executor(request,run_id).resume(
      run_id,actor_id=request_principal(request).principal_id)
    except ContinuousProbeExecutionError as exc:raise _probe_http_error(exc) from exc

@app.post("/api/v1/probes/{run_id}/stop")
def stop_managed_continuous_probe(run_id:str,request:Request):
    _secure_credential_request(request)
    try:return _probe_executor(request,run_id).stop(
      run_id,actor_id=request_principal(request).principal_id,wait_seconds=.5)
    except ContinuousProbeExecutionError as exc:raise _probe_http_error(exc) from exc

@app.post("/api/v1/probes/{run_id}/clone")
def clone_continuous_probe(run_id:str,request:Request):
    _secure_credential_request(request)
    try:return current_live_acceptance(request).clone_probe_run(run_id)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc

@app.get("/api/v1/probes/{run_id}/metrics")
def continuous_probe_metrics(run_id:str,request:Request):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).probe_metrics(run_id)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc

@app.get("/api/v1/probes/{run_id}/events")
def continuous_probe_events(run_id:str,request:Request,
    after_event_id:int|None=Query(None,ge=0),limit:int=Query(200,ge=1,le=500)):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).probe_events(
      run_id,after_event_id=after_event_id,limit=limit)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc

@app.get("/api/v1/probes/{run_id}/requests")
def continuous_probe_requests(run_id:str,request:Request,
    limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    _secure_read_only_request(request)
    try:return current_live_acceptance(request).probe_requests(
      run_id,limit=limit,offset=offset)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc

@app.get("/api/v1/probes/{run_id}/stream")
async def continuous_probe_stream(run_id:str,request:Request,
    after_event_id:int=Query(0,ge=0)):
    _secure_read_only_request(request)
    service=current_live_acceptance(request)
    try:service.probe_run(run_id,include_events=False)
    except KeyError as exc:raise HTTPException(404,detail={
      "code":"probe_run_not_found","message":"未找到该探测任务。"}) from exc
    async def frames():
      cursor=after_event_id
      while not await request.is_disconnected():
        page=await asyncio.to_thread(
          service.probe_events,run_id,after_event_id=cursor,limit=100)
        for item in page["items"]:
          cursor=int(item["event_id"])
          yield f"id: {cursor}\nevent: probe\ndata: {json.dumps(item,ensure_ascii=False)}\n\n"
        run=await asyncio.to_thread(service.probe_run,run_id,False)
        if run["status"] in {"COMPLETED","STOPPED","AUTO_STOPPED","FAILED"} and not page["items"]:
          break
        yield ": keepalive\n\n"
        await asyncio.sleep(2)
    return StreamingResponse(frames(),media_type="text/event-stream",
      headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

@app.get("/api/v1/high-cost-test-governance/status")
def high_cost_test_governance_status(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_high_cost_test_governance().monitoring_status(limit)

@app.get("/api/v1/high-cost-test-governance/audit")
def high_cost_test_governance_audit(
    request:Request,limit:int=Query(default=100,ge=1,le=500)):
    _secure_read_only_request(request)
    return current_high_cost_test_governance().monitoring_audit(limit)

@app.get("/api/v1/safety-governance/timeline")
def safety_governance_timeline(
    request:Request,limit:int=Query(default=200,ge=1,le=500)):
    """Sanitized local state-change series; never manufactures demo points."""
    _secure_read_only_request(request)
    sources=(
      ("ADV-017",current_traffic_change_governance().monitoring_audit(limit)["items"]),
      ("ADV-018",current_probe_governance().monitoring_audit(limit)["items"]),
      ("ADV-019",current_high_cost_test_governance().monitoring_audit(limit)["items"]),
      ("capability",current_capability_evidence().monitoring_audit(limit)["items"]),
      ("exploration",current_exploration_governance().monitoring_audit(limit)["items"]),
    )
    series=[{"domain":domain,"event_type":row["event_type"],
             "timestamp":row["created_at"]}
            for domain,rows in sources for row in rows]
    series.sort(key=lambda row:(row["timestamp"],row["domain"],row["event_type"]),reverse=True)
    series=series[:limit]
    latest=series[0]["timestamp"] if series else None
    if latest:
      try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(
          latest.replace("Z","+00:00"))).total_seconds()
        freshness="fresh" if age<=86400 else "stale"
      except ValueError:
        freshness="unknown"
    else:
      freshness="unknown"
    return {"status":"ready" if series else "empty","series":series,
      "latest_event_at":latest,"freshness_status":freshness,
      "stale_after_seconds":86400,
      "scheduler_overhead":current_scheduler_overhead().status(window_minutes=60),
      "permission_boundary":"read_only_monitoring; mutations require identity provider and separate approval workflow",
      "generated_demo_points":False,"network_called":False}

def build_uat_shadow(request:dict[str,Any])->dict[str,Any]:
    catalog_path=ROOT/"data"/"channel_catalog_real_v1.csv"
    raw=catalog_path.read_bytes()
    with catalog_path.open(encoding="utf-8-sig",newline="") as handle:
      catalog_rows=list(csv.DictReader(handle))
      candidates=adapt_shadow_catalog(catalog_rows)
    decision=strategy_decision(
      request_id=request["request_id"],requested_model=request["requested_model"],
      stream_required=bool(request.get("stream")),input_tokens=max(1,sum(len(str(x.get("content",""))) for x in request.get("messages",[]))//4),
      output_tokens=int(request["max_tokens"]),currency="CNY",decision_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
      candidates=candidates,strategy_name="confidence_aware_v2")
    return {
      "catalog_version":catalog_rows[0].get("catalog_version") if catalog_rows else None,
      "catalog_sha256":hashlib.sha256(raw).hexdigest(),
      "recommended_candidate":decision["selected_candidate"],
      "complete_ranking":decision["ranked_candidates"],
      "exclusion_reasons":decision["excluded_candidates"],
      "fallback_order":[x["candidate_id"] for x in decision["ranked_candidates"][1:]],
      "selection_reason":decision["selection_reason"],
      "execution_attempted":False,
      "limitation":"本地影子推荐不等于平台实际渠道。"
    }

@app.get("/api/v1/uat/status")
def get_uat_status(request:Request):
    settings,source,_,meta=_resolved(request)
    result=uat_status(settings,current_uat_store(),source)
    result["execution_control"]=current_uat_execution_control(request).status()
    if meta: result.update({k:meta[k] for k in ("expires_at","minutes_remaining","key_fingerprint")})
    result["stream_execution_ready"]=False
    return result
@app.get("/api/v1/environments")
def list_environments():
    registry=load_platform_environments()
    return {"config_version":registry.config_version,"environments":[{
      **asdict(item),"models_url":item.models_url(),"chat_completions_url":item.chat_completions_url(),
      "configuration_complete":item.configuration_complete,
      "logs_page_url":(
        (current_runtime_settings().active(item.environment_id,"logs_page_url") or {}).get("setting_value")
        or item.logs_page_url),
      "log_page_status":(
        (current_runtime_settings().active(item.environment_id,"logs_page_url") or {}).get("validation_status")
        or item.log_page_status),
      "models_endpoint_status":(
        "read_only_validated" if item.environment_id=="overseas" and
        current_uat_store().latest_successful_endpoint_validation("overseas","model_catalog_read")
        else item.models_endpoint_status)}
      for item in registry.environments.values()]}

@app.get("/api/v1/environments/overseas/log-page/status")
def overseas_log_page_status():
    return current_runtime_settings().status("overseas")

@app.get("/api/v1/environments/china_uat/log-page/status")
def domestic_uat_log_page_status():
    status=current_runtime_settings().status("china_uat")
    return {
      **status,
      "reviewed_url":"https://uat.weimeta.cn/console/billing/logs",
      "reviewed_origin":"https://uat.weimeta.cn",
      "reviewed_path":"/console/billing/logs",
      "configuration_reason":(
        None if status["logs_page_url"]
        else "经过评审的国内 UAT 日志页尚未由本机操作员明确确认。"),
    }

@app.post("/api/v1/environments/china_uat/log-page/preview")
async def domestic_uat_log_page_preview(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    try:
      return current_runtime_settings().preview_environment_log_page(
        "china_uat",body.get("url"))
    except ValueError as exc:
      raise HTTPException(400,detail={
        "code":str(exc),
        "message":"只允许确认经过评审的国内 UAT 只读账单日志页。"})

@app.post("/api/v1/environments/china_uat/log-page/confirm")
async def domestic_uat_log_page_confirm(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    try:
      return current_runtime_settings().confirm_environment_log_page(
        "china_uat",str(body.get("validation_id") or ""),
        str(body.get("value_sha256") or ""),
        body.get("explicit_confirmation") is True,"local_operator")
    except ValueError as exc:
      raise HTTPException(409,detail={
        "code":str(exc),"message":"国内 UAT 日志页确认失败。"})

@app.post("/api/v1/environments/overseas/log-page/preview")
async def overseas_log_page_preview(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    try:
      return current_runtime_settings().preview_log_page(body.get("url"))
    except ValueError as exc:
      code=canonical_error_code(str(exc))
      raise HTTPException(400,detail={"code":code,"message":"海外日志页地址未通过安全校验。"})

@app.post("/api/v1/environments/overseas/log-page/confirm")
async def overseas_log_page_confirm(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    try:
      return current_runtime_settings().confirm_log_page(
        str(body.get("validation_id") or ""),str(body.get("value_sha256") or ""),
        body.get("explicit_confirmation") is True,"local_operator")
    except ValueError as exc:
      code=canonical_error_code(str(exc))
      raise HTTPException(409,detail={"code":code,"message":"海外日志页确认失败。"})

@app.post("/api/v1/environments/overseas/log-page/disable")
async def overseas_log_page_disable(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    if body.get("explicit_confirmation") is not True:
      raise HTTPException(400,detail={"code":"explicit_confirmation_required","message":"需要明确确认停用。"})
    return current_runtime_settings().disable_log_page("local_operator")
@app.get("/api/v1/environments/{environment_id}/status")
def environment_status(environment_id:str,request:Request):
    try: settings,source,_,meta=_resolved(request,environment_id)
    except Exception: raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    result=uat_status(settings,current_uat_store(),source)
    if environment_id=="china_uat":result["execution_control"]=current_uat_execution_control(request).status()
    if meta: result.update({k:meta[k] for k in ("expires_at","minutes_remaining","key_fingerprint")})
    result["stream_execution_ready"]=False
    return result
@app.get("/api/v1/environments/{environment_id}/execution-state")
def get_environment_execution_state(environment_id:str,request:Request):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported",
        "message":"Environment is not supported."})
    if environment_id!="china_uat":
      raise HTTPException(404,detail={"code":"environment_execution_toggle_not_supported",
        "message":"The direct environment switch is not supported for this environment."})
    settings=UatSettings.load(environment_id)
    saved=current_runtime_settings().execution_state(
      environment_id,principal=request_principal(request))
    return saved or {
      "environment_id":environment_id,
      "enabled":settings.enabled,
      "state_source":"environment_configuration",
      "updated_at":None,"updated_by":None,
      "setting_version":"environment_execution_state_v1",
    }
@app.put("/api/v1/environments/{environment_id}/execution-state")
async def update_environment_execution_state(environment_id:str,request:Request):
    _secure_credential_request(request)
    principal=request_principal(request)
    try:
      ENTERPRISE_AUTHORIZATION.authorize(principal,"uat.execution.manage")
      body=await request.json()
      if set(body)!={"enabled"} or type(body["enabled"]) is not bool:
        raise ValueError("environment_execution_enabled_boolean_required")
      return current_runtime_settings().set_execution_enabled(
        environment_id,body["enabled"],principal.principal_id,principal=principal)
    except PermissionError as exc:
      raise HTTPException(403,detail={"code":"permission_denied",
        "message":"当前用户没有修改 UAT 环境状态的权限。"}) from exc
    except ValueError as exc:
      code=str(exc)
      status_code=404 if code in {"environment_not_supported","environment_execution_toggle_not_supported"} else 400
      raise HTTPException(status_code,detail={"code":code,
        "message":"UAT 环境状态未能保存。"}) from exc
@app.get("/api/v1/environments/{environment_id}/models")
def environment_models(environment_id:str,request:Request,refresh:bool=Query(False)):
    registry=load_platform_environments()
    environment=registry.get(environment_id)
    if not environment:
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    settings,source,sid,_=_resolved(request,environment_id)
    if environment_id=="overseas" and not current_uat_store().latest_successful_endpoint_validation(
      "overseas","model_catalog_read"):
      return {"status":"error","catalog_status":"blocked","source":"remote_model_endpoint",
        "fetched_at":None,"environment_id":"overseas","environment_name":environment.display_name,
        "model_count":0,"models":[],"cache":"miss",
        "error":{"code":"overseas_model_validation_pending",
          "message":"请先在环境接入页人工确认并验证一次海外模型目录。"}}
    result,network_called=MODEL_CATALOG.fetch(settings.api_key,MODELS_TRANSPORT,min(settings.timeout_seconds,10),
      refresh,environment_id,environment.display_name,environment.models_url())
    if network_called:
      _audit_credential("environment_model_catalog_read",sid,result["status"]=="ready",source,
        (result.get("error") or {}).get("code"),urlparse(environment.models_url() or "").hostname,environment_id)
    return result
@app.post("/api/v1/environments/overseas/discovery/validate")
async def validate_overseas_discovery(request:Request):
    _secure_credential_request(request)
    body=await request.json()
    confirmation=body.get("confirmation") or {}
    if not confirmation.get("confirmed") or confirmation.get("text")!="I confirm one read-only overseas model-catalog validation request.":
      raise HTTPException(400,detail={"code":"CONFIRMATION_REQUIRED","message":"Explicit read-only validation confirmation is required."})
    environment=load_platform_environments().require("overseas")
    settings,source,sid,_=_resolved(request,"overseas")
    if not settings.api_key:
      raise HTTPException(409,detail={"code":"environment_key_not_configured","message":"Configure the overseas API key first."})
    captured={}
    def single_attempt_transport(url,key,timeout):
      response=MODELS_TRANSPORT(url,key,timeout)
      captured.update(response)
      return response
    started=time.perf_counter()
    result,network_called=MODEL_CATALOG.fetch(settings.api_key,single_attempt_transport,min(settings.timeout_seconds,10),
      True,"overseas",environment.display_name,environment.models_url())
    body_bytes=captured.get("body",b"")
    if not isinstance(body_bytes,bytes): body_bytes=str(body_bytes).encode()
    headers={str(k).lower():str(v) for k,v in captured.get("headers",{}).items()}
    evidence={
      "validation_id":f"OVM-{uuid.uuid4().hex[:16].upper()}",
      "environment_id":"overseas","validation_type":"model_catalog_read",
      "requested_url":environment.models_url(),"http_status":captured.get("status"),
      "content_type":headers.get("content-type"),
      "elapsed_ms":captured.get("elapsed_ms",round((time.perf_counter()-started)*1000,2)),
      "response_body_sha256":hashlib.sha256(body_bytes).hexdigest() if captured else None,
      "model_count":result.get("model_count",0),
      "error_category":(result.get("error") or {}).get("code"),
      "validation_timestamp":datetime.now(timezone.utc).isoformat(),
      "success":result.get("status")=="ready",
    }
    current_uat_store().save_endpoint_validation(evidence)
    if network_called:
      _audit_credential("overseas_discovery_validation",sid,result["status"]=="ready",source,
        (result.get("error") or {}).get("code"),urlparse(environment.models_url() or "").hostname,"overseas")
    return {"configuration_persisted":False,"models_endpoint_status":(
      "read_only_validated" if evidence["success"] else "validation_failed"),
      "completion_endpoint_status":"not_validated","real_execution_enabled":False,
      "validation":result,"evidence":evidence}
@app.get("/api/v1/uat/models")
def get_uat_models(request:Request,refresh:bool=Query(False)):
    settings,source,sid,_=_resolved(request)
    result,network_called=MODEL_CATALOG.fetch(
      settings.api_key,MODELS_TRANSPORT,min(settings.timeout_seconds,10),refresh)
    if network_called:
      _audit_credential("uat_model_catalog_read",sid,result["status"]=="ready",source,
        (result.get("error") or {}).get("code"),"api-uat.weimeta.cn")
    return result

_CAPABILITY_STATUS_ORDER={"pending":0,"unsupported":1,"observed":2,"confirmed":3}
_PUBLIC_CAPABILITY_KEYS={
  "text":("text_input","text_output"),
  "image_understanding":("image_understanding",),
  "image_generation":("image_generation",),
  "audio_input":("audio_input",),
  "audio_output":("audio_output",),
  "video_input":("video_input",),
  "video_generation":("video_generation",),
  "streaming":("streaming",),
  "tools":("tool_calling",),
}

def _public_capability_model(raw:dict[str,Any])->dict[str,Any]:
    """Shape the persisted projection for business UI without exposing raw rows."""
    raw_capabilities=raw.get("capabilities") or {}
    call_count=int(raw.get("call_count") or 0)
    success_count=int(raw.get("success_count") or 0)
    failure_count=int(raw.get("failure_count") or 0)
    capabilities={}
    limitations=[]
    for public_key,internal_keys in _PUBLIC_CAPABILITY_KEYS.items():
      cells=[raw_capabilities.get(key) or {} for key in internal_keys]
      chosen=max((str(cell.get("status") or "pending") for cell in cells),
        key=lambda value:_CAPABILITY_STATUS_ORDER.get(value,0),default="pending")
      sources=sorted({str(source) for cell in cells for source in (cell.get("sources") or [])})
      reasons=sorted({str(reason) for cell in cells for reason in (cell.get("reasons") or [])})
      conflict=any(bool(cell.get("conflict")) for cell in cells)
      if conflict:
        limitations.append(f"{public_key}：目录与真实调用证据存在冲突")
      relevant_calls=(call_count if public_key in {"text","streaming"} else
        (1 if sources else 0))
      if "channel_unavailable" in reasons:
        limitations.append(f"{public_key}：本次上游渠道不可用，能力仍待确认")
      relevant_success=(success_count if public_key in {"text","streaming"} else
        (1 if chosen in {"confirmed","observed"} else 0))
      relevant_failure=(failure_count if public_key in {"text","streaming"} else 0)
      capabilities[public_key]={
        "status":chosen,"evidence_count":int(raw.get("evidence_count") or 0),
        "sample_count":relevant_calls,"success_count":relevant_success if relevant_calls else None,
        "failure_count":relevant_failure if relevant_calls else None,"sources":sources,
        "last_success_at":raw.get("latest_call_at") if relevant_calls and relevant_success else None,
        "last_failure_at":raw.get("latest_call_at") if relevant_calls and relevant_failure else None,
        "confidence":None,"expires_at":None,"reasons":reasons,
      }
    overall=str(raw.get("overall_status") or "pending")
    proper_limitations=[]
    for public_key,internal_keys in _PUBLIC_CAPABILITY_KEYS.items():
      cells=[raw_capabilities.get(key) or {} for key in internal_keys]
      if any(bool(cell.get("conflict")) for cell in cells):
        proper_limitations.append(f"{public_key}：目录与真实调用证据存在冲突")
      if any("channel_unavailable" in (cell.get("reasons") or []) for cell in cells):
        proper_limitations.append(f"{public_key}：本次上游渠道不可用，能力仍待确认")
    if call_count==0 and overall=="pending":limitations.append("样本不足，能力待确认")
    limitations=proper_limitations
    if call_count==0 and overall=="pending":
      limitations.append("样本不足，能力待确认")
    actual_cost=None
    try:
      actual_cost=float(raw["cost_amount"]) if raw.get("cost_amount") is not None else None
    except (TypeError,ValueError):
      pass
    return {
      "model_id":raw.get("model_id"),"display_name":raw.get("display_name") or raw.get("model_id"),
      "provider":raw.get("provider"),"endpoints":raw.get("endpoints") or [],
      "tags":raw.get("tags") or [],"context_limit":raw.get("context_limit"),
      "pricing_type":raw.get("pricing_type"),"catalog_updated_at":raw.get("catalog_updated_at"),
      "capabilities":capabilities,
      "call_summary":{"call_count":call_count,"success_count":success_count,
        "success_rate":(success_count/call_count if call_count else None),
        "stream_count":int(raw.get("stream_count") or 0),
        "non_stream_count":int(raw.get("non_stream_count") or 0),
        "p50_ms":raw.get("p50_latency_ms"),"p95_ms":raw.get("p95_latency_ms"),
        "input_tokens":int(raw.get("input_tokens") or 0),
        "output_tokens":int(raw.get("output_tokens") or 0),"actual_cost":actual_cost,
        "last_request_id":raw.get("latest_request_id"),"last_called_at":raw.get("latest_call_at")},
      "evidence_summary":{"status":overall,"evidence_count":int(raw.get("evidence_count") or 0),
        "sources":raw.get("evidence_sources") or [],"last_verified_at":raw.get("latest_call_at")},
      "limitations":limitations,
      "technical_metadata":{"aggregation_job_id":raw.get("aggregation_job_id"),
        "updated_at":raw.get("updated_at"),"currency":raw.get("currency")},
    }

def _public_capability_overview(raw:dict[str,Any])->dict[str,Any]:
    models=[_public_capability_model(item) for item in (raw.get("models") or [])]
    coverage=[]
    for public_key in _PUBLIC_CAPABILITY_KEYS:
      coverage.append({"capability":public_key,**{
        status:sum(1 for model in models if model["capabilities"][public_key]["status"]==status)
        for status in ("confirmed","observed","pending","unsupported")}})
    source_counts=Counter(
      source for model in models for source in (model["evidence_summary"]["sources"] or ["none"]))
    return {**raw,"models":models,"capability_coverage":coverage,
      "evidence_sources":[{"source":key,"count":value} for key,value in sorted(source_counts.items())]}

def _public_model_mappings(raw:dict[str,Any])->dict[str,Any]:
    distribution=Counter()
    items=[]
    for item in raw.get("items") or []:
      count=int(item.get("call_count") or 0)
      requested=item.get("requested_model"); billed=item.get("billed_model"); actual=item.get("actual_model")
      if actual is None:distribution["missing_actual"]+=count
      elif billed is not None and billed!=actual:distribution["billed_actual_mismatch"]+=count
      elif billed is not None and requested!=billed:distribution["requested_billed_mismatch"]+=count
      else:distribution["consistent"]+=count
      level={"exact_mapping":"exact"}.get(str(item.get("evidence_level")),item.get("evidence_level") or "pending")
      items.append({"requested_model":requested,"billed_model":billed,"actual_model":actual,
        "call_count":count,"consistency_rate":(
          int(item.get("consistent_count") or 0)/count if count else None),
        "mismatch_count":int(item.get("mismatch_count") or 0),
        "missing_actual_count":int(item.get("missing_actual_count") or 0),
        "last_observed":item.get("latest_observed_at"),"evidence_level":level,
        "source":item.get("source_type") or "unknown"})
    return {"status":"ready","items":items,
      "consistency_distribution":[{"status":key,"count":value} for key,value in distribution.items()],
      "technical_metadata":{"environment_id":raw.get("environment_id"),"mapping_count":len(items),
        "exact_count":raw.get("exact_count"),"historical_count":raw.get("historical_count"),
        "pending_count":raw.get("pending_count")}}

@app.post("/api/v1/model-capabilities/ensure")
async def ensure_model_capabilities(request:Request):
    body=await request.json()
    environment_id=str(body.get("environment_id") or "china_uat")
    if environment_id not in {"china_uat","uat","国内UAT"}:
      raise HTTPException(400,detail={"code":"model_capability_environment_unsupported",
        "message":"模型能力聚合仅使用国内 UAT 真实数据。"})
    settings,source,sid,_=_resolved(request,"china_uat")
    catalog=None; catalog_error=None
    if settings.api_key:
      result,network_called=MODEL_CATALOG.fetch(
        settings.api_key,MODELS_TRANSPORT,min(settings.timeout_seconds,10),False)
      if network_called:
        _audit_credential("model_capability_catalog_read",sid,result.get("status")=="ready",source,
          (result.get("error") or {}).get("code"),"api-uat.weimeta.cn","china_uat")
      if result.get("status")=="ready":catalog=result
      else:catalog_error=(result.get("error") or {}).get("code") or "model_catalog_unavailable"
    else:
      catalog_error="uat_credential_not_available"
    try:
      projection=current_model_capability_aggregation(request).ensure(catalog,"china_uat")
    except ValueError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"真实模型能力证据聚合失败，原始日志未受影响。"}) from exc
    return {**projection,"already_running":False,"watermark":projection.get("source_watermark"),
      "catalog_error":catalog_error}

@app.get("/api/v1/model-capabilities/aggregation-status")
def model_capability_aggregation_status(request:Request,environment_id:str=Query("china_uat")):
    return current_model_capability_aggregation(request).status(environment_id)

@app.get("/api/v1/model-capabilities/overview")
def model_capabilities_overview(request:Request,environment_id:str=Query("china_uat")):
    return _public_capability_overview(
      current_model_capability_aggregation(request).overview(environment_id))

@app.get("/api/v1/model-capabilities/models")
def model_capability_models(request:Request,environment_id:str=Query("china_uat")):
    raw=current_model_capability_aggregation(request).models(environment_id,page_size=500)
    return {**raw,"items":[_public_capability_model(item) for item in raw.get("items") or []]}

@app.get("/api/v1/model-capabilities/models/{model_id}")
def model_capability_detail(model_id:str,request:Request,environment_id:str=Query("china_uat")):
    raw=current_model_capability_aggregation(request).model_detail(model_id,environment_id)
    if raw.get("status")=="not_found":
      raise HTTPException(404,detail={"code":"model_capability_not_found",
        "message":"未找到该模型的真实能力聚合结果。"})
    return _public_capability_model(raw)

@app.get("/api/v1/model-mappings")
def model_mappings(request:Request,environment_id:str=Query("china_uat")):
    return _public_model_mappings(
      current_model_capability_aggregation(request).mappings(environment_id))
@app.post("/api/v1/uat/credentials/session")
async def set_session_credential(request:Request,response:Response):
    if not SESSION_ENABLED: raise HTTPException(404,detail={"code":"FEATURE_DISABLED","message":"Session credentials are disabled."})
    _secure_credential_request(request)
    body=await request.json()
    confirmation=body.get("confirmation") or {}
    if not confirmation.get("confirmed") or confirmation.get("text")!="I authorize encrypted persistence of this UAT key for this Windows user until replacement or logout.":
      raise HTTPException(400,detail={"code":"CONFIRMATION_REQUIRED","message":"Explicit encrypted-persistence confirmation is required."})
    raw=body.get("api_key")
    if not isinstance(raw,str): raise HTTPException(400,detail={"code":"INVALID_CREDENTIAL","message":"Credential is required."})
    if "\n" in raw or "\r" in raw: raise HTTPException(400,detail={"code":"INVALID_CREDENTIAL","message":"Credential cannot contain line breaks."})
    key=raw.strip()
    if not key or len(key)>4096 or any(ord(char)<32 or ord(char)>126 for char in key):
      raise HTTPException(400,detail={"code":"INVALID_CREDENTIAL","message":"Credential must be printable ASCII with a valid length."})
    principal=request_principal(request)
    if PERSISTENT_CREDENTIALS is None:
      raise HTTPException(503,detail={"code":"CREDENTIAL_VAULT_UNAVAILABLE",
        "message":"Windows encrypted credential storage is unavailable."})
    try: meta=await run_in_threadpool(
      PERSISTENT_CREDENTIALS.save,_credential_scope(request),key)
    except CredentialVaultError as exc:
      raise HTTPException(503,detail={"code":"CREDENTIAL_PERSISTENCE_FAILED",
        "message":"The credential could not be encrypted and saved."}) from exc
    old=request.cookies.get(COOKIE_NAME)
    if old: SESSION_CREDENTIALS.clear(old)
    response.delete_cookie(COOKIE_NAME,path="/",secure=COOKIE_SECURE,samesite="strict")
    _audit_credential("credential_encrypted_saved",principal.session_id,True,"windows_encrypted_vault")
    settings=replace(UatSettings.load(),api_key=key)
    ready=uat_status(settings,current_uat_store(),"windows_encrypted_vault")
    return {"configured":True,"credential_source":"windows_encrypted_vault","created_at":meta["created_at"],
            "updated_at":meta["updated_at"],"expires_at":None,"minutes_remaining":None,
            "key_fingerprint":meta["key_fingerprint"],"encryption":meta["encryption"],
            "key_protection":meta["key_protection"],
            "execution_ready":ready["execution_ready"],"blocking_reasons":ready["blocking_reasons"]}
@app.get("/api/v1/uat/credentials/status")
def credential_status(request:Request):
    settings,source,_,meta=_resolved(request)
    if source in {"session","windows_encrypted_vault"}:
      fields=("created_at","updated_at","expires_at","minutes_remaining","key_fingerprint","encryption","key_protection")
      return {"configured":True,"credential_source":source,**{k:meta.get(k) for k in fields}}
    return {"configured":bool(settings.api_key),"credential_source":source,"expires_at":None,"minutes_remaining":None,"key_fingerprint":None}
@app.get("/api/v1/environments/{environment_id}/credentials/status")
def environment_credential_status(environment_id:str,request:Request):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    settings,source,_,meta=_resolved(request,environment_id)
    if source in {"session","windows_encrypted_vault"}:
      fields=("created_at","updated_at","expires_at","minutes_remaining","key_fingerprint","encryption","key_protection")
      return {"environment_id":environment_id,"configured":True,"credential_source":source,
        **{k:meta.get(k) for k in fields}}
    return {"environment_id":environment_id,"configured":bool(settings.api_key),"credential_source":source,
      "expires_at":None,"minutes_remaining":None,"key_fingerprint":None}
@app.post("/api/v1/environments/{environment_id}/credentials/session")
async def set_environment_credential(environment_id:str,request:Request,response:Response):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    _secure_credential_request(request)
    body=await request.json();confirmation=body.get("confirmation") or {}
    if not confirmation.get("confirmed") or confirmation.get("text")!="I authorize encrypted persistence of this API key for the selected environment until replacement or logout.":
      raise HTTPException(400,detail={"code":"CONFIRMATION_REQUIRED","message":"Explicit environment-scoped encrypted-persistence confirmation is required."})
    raw=body.get("api_key")
    if not isinstance(raw,str) or not raw.strip() or len(raw.strip())>4096 or "\n" in raw or "\r" in raw or any(ord(char)<32 or ord(char)>126 for char in raw.strip()):
      raise HTTPException(400,detail={"code":"INVALID_CREDENTIAL","message":"Credential is invalid."})
    principal=request_principal(request)
    if PERSISTENT_CREDENTIALS is None:
      raise HTTPException(503,detail={"code":"CREDENTIAL_VAULT_UNAVAILABLE",
        "message":"Windows encrypted credential storage is unavailable."})
    try:meta=await run_in_threadpool(
      PERSISTENT_CREDENTIALS.save,_credential_scope(request,environment_id),raw.strip())
    except CredentialVaultError as exc:
      raise HTTPException(503,detail={"code":"CREDENTIAL_PERSISTENCE_FAILED",
        "message":"The credential could not be encrypted and saved."}) from exc
    existing_sid=request.cookies.get(COOKIE_NAME)
    if existing_sid: SESSION_CREDENTIALS.clear(existing_sid,environment_id)
    response.delete_cookie(COOKIE_NAME,path="/",secure=COOKIE_SECURE,samesite="strict")
    _audit_credential("environment_credential_encrypted_saved",principal.session_id,True,
      "windows_encrypted_vault",environment_id=environment_id)
    fields=("created_at","updated_at","expires_at","minutes_remaining","key_fingerprint","encryption","key_protection")
    return {"environment_id":environment_id,"configured":True,
      "credential_source":"windows_encrypted_vault",**{k:meta.get(k) for k in fields}}
@app.delete("/api/v1/environments/{environment_id}/credentials/session")
def clear_environment_credential(environment_id:str,request:Request,response:Response):
    _secure_credential_request(request)
    principal=request_principal(request);sid=request.cookies.get(COOKIE_NAME)
    legacy=SESSION_CREDENTIALS.clear(sid,environment_id)
    try: persisted=PERSISTENT_CREDENTIALS.delete(_credential_scope(request,environment_id)) if PERSISTENT_CREDENTIALS else False
    except CredentialVaultError as exc:
      raise HTTPException(503,detail={"code":"CREDENTIAL_DELETE_FAILED",
        "message":"The encrypted credential could not be cleared."}) from exc
    cleared=legacy or persisted
    _audit_credential("environment_credential_cleared",principal.session_id,cleared,
      "windows_encrypted_vault",environment_id=environment_id)
    return {"environment_id":environment_id,"configured":False,"cleared":cleared}

def _workbench_settings(environment_id:str,request:Request)->UatSettings:
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    settings=UatSettings.load(environment_id)
    saved=current_runtime_settings().execution_enabled(
      environment_id,principal=request_principal(request))
    if saved is not None: settings=replace(settings,enabled=saved)
    # The workbench never inherits a hidden environment or encrypted-vault key.
    return replace(settings,api_key="")

def _temporary_workbench_credential(request:Request,environment_id:str,
                                    auth_session_id:str|None=None):
    sid=request.cookies.get(COOKIE_NAME)
    key,meta=SESSION_CREDENTIALS.resolve(sid,environment_id=environment_id)
    expected=session_hash(sid or "") if sid else None
    if auth_session_id is not None and auth_session_id!=expected:
      raise HTTPException(403,detail={"code":"TEMPORARY_CREDENTIAL_SESSION_MISMATCH",
        "message":"临时凭据会话与当前浏览器会话不匹配。"})
    return sid,key,meta,expected

@app.get("/api/v1/environments/{environment_id}/credentials/temporary/status")
def temporary_credential_status(environment_id:str,request:Request):
    _secure_read_only_request(request)
    _workbench_settings(environment_id,request)
    sid,key,meta,auth_session_id=_temporary_workbench_credential(request,environment_id)
    fingerprint=(meta or {}).get("key_fingerprint")
    verified=bool(key and fingerprint and WORKBENCH_CONNECTIONS.verified(
      sid,environment_id,str(fingerprint),str(auth_session_id or "")))
    return {"environment_id":environment_id,"configured":bool(key),
      "credential_source":"temporary_session" if key else "none",
      "auth_session_id":auth_session_id if key else None,
      "connection_status":"success" if verified else "not_tested",
      **({k:meta.get(k) for k in ("created_at","expires_at","minutes_remaining","key_fingerprint")}
         if meta else {"created_at":None,"expires_at":None,"minutes_remaining":None,"key_fingerprint":None})}

@app.post("/api/v1/environments/{environment_id}/credentials/temporary")
async def set_temporary_credential(environment_id:str,request:Request,response:Response):
    if not SESSION_ENABLED:
      raise HTTPException(404,detail={"code":"FEATURE_DISABLED","message":"Temporary credentials are disabled."})
    _secure_credential_request(request)
    _workbench_settings(environment_id,request)
    body=await request.json();raw=body.get("api_key")
    if not isinstance(raw,str) or not raw.strip() or len(raw.strip())>4096 or \
       "\n" in raw or "\r" in raw or any(ord(char)<32 or ord(char)>126 for char in raw.strip()):
      raise HTTPException(400,detail={"code":"INVALID_CREDENTIAL",
        "message":"API Key 必须是非空的可打印 ASCII，且不得包含换行。"})
    existing=request.cookies.get(COOKIE_NAME)
    if existing:
      WORKBENCH_CONNECTIONS.clear(existing,environment_id)
    try:sid,meta=SESSION_CREDENTIALS.create(
      raw.strip(),environment_id=environment_id,session_id=existing)
    except OverflowError as exc:
      raise HTTPException(429,detail={"code":"TEMPORARY_CREDENTIAL_SESSION_LIMIT",
        "message":"临时凭据会话数量已达上限。"}) from exc
    response.set_cookie(COOKIE_NAME,sid,max_age=SESSION_CREDENTIALS.ttl_minutes*60,
      httponly=True,secure=COOKIE_SECURE,samesite="strict",path="/")
    _audit_credential("temporary_credential_saved",sid,True,"temporary_session",
      environment_id=environment_id)
    return {**meta,"environment_id":environment_id,"configured":True,
      "credential_source":"temporary_session","auth_session_id":session_hash(sid),
      "connection_status":"not_tested"}

@app.post("/api/v1/environments/{environment_id}/credentials/temporary/from-persistent")
def restore_temporary_credential_from_persistent(
    environment_id:str,request:Request,response:Response):
    """Explicitly bind an encrypted-vault credential to this browser session.

    The credential is decrypted only in the backend process and is never returned
    to the browser. The new temporary session still requires a fresh `/v1/models`
    connection test before any UAT request can execute.
    """
    if not SESSION_ENABLED:
      raise HTTPException(404,detail={"code":"FEATURE_DISABLED",
        "message":"Temporary credentials are disabled."})
    _secure_credential_request(request)
    _workbench_settings(environment_id,request)
    persisted=_persistent_credential(request,environment_id)
    if not persisted:
      raise HTTPException(409,detail={"code":"PERSISTENT_CREDENTIAL_REQUIRED",
        "message":"当前 Windows 用户没有可复用的后端加密凭据。"})
    key,_vault_meta=persisted
    existing=request.cookies.get(COOKIE_NAME)
    if existing:
      WORKBENCH_CONNECTIONS.clear(existing,environment_id)
    try:
      sid,meta=SESSION_CREDENTIALS.create(
        key,environment_id=environment_id,session_id=existing)
    except OverflowError as exc:
      raise HTTPException(429,detail={"code":"TEMPORARY_CREDENTIAL_SESSION_LIMIT",
        "message":"临时凭据会话数量已达上限。"}) from exc
    response.set_cookie(COOKIE_NAME,sid,max_age=SESSION_CREDENTIALS.ttl_minutes*60,
      httponly=True,secure=COOKIE_SECURE,samesite="strict",path="/")
    _audit_credential("temporary_credential_restored_from_vault",sid,True,
      "windows_encrypted_vault",environment_id=environment_id)
    return {**meta,"environment_id":environment_id,"configured":True,
      "credential_source":"temporary_session","auth_session_id":session_hash(sid),
      "connection_status":"not_tested"}

@app.delete("/api/v1/environments/{environment_id}/credentials/temporary")
def clear_temporary_credential(environment_id:str,request:Request,response:Response):
    _secure_credential_request(request)
    _workbench_settings(environment_id,request)
    sid=request.cookies.get(COOKIE_NAME)
    WORKBENCH_CONNECTIONS.clear(sid,environment_id)
    cleared=SESSION_CREDENTIALS.clear(sid,environment_id)
    response.delete_cookie(COOKIE_NAME,path="/",secure=COOKIE_SECURE,samesite="strict")
    _audit_credential("temporary_credential_cleared",sid,cleared,"temporary_session",
      environment_id=environment_id)
    return {"environment_id":environment_id,"configured":False,"cleared":cleared,
      "credential_source":"none","auth_session_id":None,"connection_status":"not_tested"}

@app.post("/api/v1/environments/{environment_id}/credentials/temporary/test")
async def test_temporary_credential(environment_id:str,request:Request):
    _secure_credential_request(request)
    settings=_workbench_settings(environment_id,request)
    body=await request.json()
    sid,key,meta,auth_session_id=_temporary_workbench_credential(
      request,environment_id,str(body.get("auth_session_id") or ""))
    if not key or not meta:
      raise HTTPException(409,detail={"code":"TEMPORARY_API_KEY_REQUIRED",
        "message":"请先提交当前浏览器会话使用的 API Key。"})
    auth=body.get("auth") if isinstance(body.get("auth"),dict) else {
      "method":"bearer","header_name":"Authorization","prefix":"Bearer"}
    try:
      spec=normalize_request({"method":"GET","path":"/v1/models","auth":auth,
        "body":{"type":"none"},"timeout_seconds":min(settings.timeout_seconds,10)},settings)
      result=WORKBENCH_TRANSPORT(spec,key)
    except WorkbenchError as exc:
      raise HTTPException(exc.status_code,detail={"code":exc.code,
        "message":"连接测试配置未通过安全校验。"}) from exc
    except (TimeoutError,OSError,urllib.error.URLError):
      _audit_credential("temporary_credential_connection_tested",sid,False,
        "temporary_session","timeout",urlparse(settings.base_url).hostname,environment_id)
      return {"environment_id":environment_id,"connection_status":"timeout",
        "http_status":None,"tested_at":datetime.now(timezone.utc).isoformat(),
        "auth_session_id":auth_session_id,"message":"UAT 连接测试超时。"}
    code=int(result.get("status") or 0)
    success=code==200
    catalog_result={"model_count":0,"models":[]}
    if success:
      catalog_result,_=MODEL_CATALOG.fetch(key,lambda *_args:result,
        min(settings.timeout_seconds,10),refresh=True,
        environment_id=environment_id,environment_name=(
          "国内 UAT" if environment_id=="china_uat" else environment_id),
        models_url=settings.base_url.rstrip("/")+"/v1/models")
      success=catalog_result.get("catalog_status")=="ready"
    if success:
      WORKBENCH_CONNECTIONS.record(sid,ConnectionProof(
        environment_id,str(meta["key_fingerprint"]),str(auth_session_id),
        datetime.now(timezone.utc).isoformat(),str(meta["expires_at"])))
    else:WORKBENCH_CONNECTIONS.clear(sid,environment_id)
    _audit_credential("temporary_credential_connection_tested",sid,success,
      "temporary_session",None if success else f"http_{code}",urlparse(settings.base_url).hostname,environment_id)
    return {"environment_id":environment_id,
      "connection_status":"success" if success else "authentication_failed" if code in {401,403} else "endpoint_unavailable",
      "http_status":code,"tested_at":datetime.now(timezone.utc).isoformat(),
      "auth_session_id":auth_session_id,
      "model_count":int(catalog_result.get("model_count") or 0),
      "models":catalog_result.get("models") if success else [],
      "message":"UAT API Key 连接测试成功。" if success else "UAT API Key 连接测试未通过。"}

@app.post("/api/v1/environments/{environment_id}/request-workbench/models")
async def request_workbench_models(environment_id:str,request:Request):
    """Return only the catalog cached by this session's explicit key test."""
    _secure_credential_request(request)
    _workbench_settings(environment_id,request)
    body=await request.json()
    sid,key,meta,auth_session_id=_temporary_workbench_credential(
      request,environment_id,str(body.get("auth_session_id") or ""))
    fingerprint=str((meta or {}).get("key_fingerprint") or "")
    if not key or not WORKBENCH_CONNECTIONS.verified(
        sid,environment_id,fingerprint,str(auth_session_id or "")):
      raise HTTPException(409,detail={"code":"TEMPORARY_API_KEY_TEST_REQUIRED",
        "message":"请先使用当前临时 API Key 完成连接测试。"})
    result=MODEL_CATALOG.cached(key,environment_id)
    if not result:
      WORKBENCH_CONNECTIONS.clear(sid,environment_id)
      raise HTTPException(409,detail={"code":"TEMPORARY_MODEL_CATALOG_EXPIRED",
        "message":"临时模型目录已过期，请重新执行连接测试。"})
    return {**result,"credential_source":"temporary_session"}

@app.post("/api/v1/environments/{environment_id}/request-workbench/validate")
def validate_request_workbench(environment_id:str,body:dict[str,Any],request:Request):
    settings=_workbench_settings(environment_id,request)
    auth_session_id=str(body.get("auth_session_id") or "")
    sid,key,meta,current_auth_session_id=_temporary_workbench_credential(
      request,environment_id,None)
    if key and auth_session_id and auth_session_id!=current_auth_session_id:
      raise HTTPException(403,detail={"code":"TEMPORARY_CREDENTIAL_SESSION_MISMATCH",
        "message":"临时凭据会话与当前浏览器会话不匹配。"})
    fingerprint=str((meta or {}).get("key_fingerprint") or "")
    verified=WORKBENCH_CONNECTIONS.verified(
      sid,environment_id,fingerprint,str(current_auth_session_id or ""))
    capabilities=current_uat_output_capabilities(request).catalog(
      str(body.get("channel_id") or "unified-routing"))
    return validate_workbench(body,settings,credential_configured=bool(key),
      connection_verified=verified,capabilities=capabilities,for_execution=False)

def _automatic_live_model_decision(environment_id:str,catalog:dict[str,Any],
                                   call_logs:CallLogService)->tuple[str,dict[str,Any]]:
    """Choose from the real catalog using attributable recent UAT evidence.

    Pending capability evidence is intentionally not a blocker for text.  A
    model with a successful recent live call wins over an unobserved catalog
    entry; deterministic model-id ordering is only the final tie breaker.
    """
    stage_durations:dict[str,float]={};stage_started=time.perf_counter()
    catalog_rows=[item for item in catalog.get("models",[]) if str(item.get("id") or "").strip()]
    stage_durations["candidate_discovery"]=(time.perf_counter()-stage_started)*1000
    stage_started=time.perf_counter()
    eligible=[item for item in catalog_rows if item.get("execution_allowed") is not False]
    stage_durations["capability_filter"]=(time.perf_counter()-stage_started)*1000
    if not eligible:
      raise HTTPException(409,detail={"code":"NO_EXECUTABLE_MODEL",
        "message":"当前 API Key 的真实模型目录中没有可执行模型。"})
    stage_started=time.perf_counter()
    recent=call_logs.list_records(limit=1000,environment_id=environment_id,
      source_type="realtime_execution").get("items",[])
    stage_durations["metrics_read"]=(time.perf_counter()-stage_started)*1000
    evidence:dict[str,dict[str,Any]]={}
    for row in recent:
      if row.get("endpoint_type")!="uat_http_post":continue
      # requested_model is the authoritative catalog identity; actual_model may
      # be a provider alias (for example MiniMax/MiniMax-M3).
      model_id=str(row.get("requested_model") or row.get("actual_model") or "").strip()
      if not model_id or model_id=="non_model_uat_http":continue
      stats=evidence.setdefault(model_id,{"success":0,"failure":0,"latencies":[]})
      if row.get("request_status")=="SUCCESS":stats["success"]+=1
      elif row.get("request_status") in {"FAILED","TIMEOUT","INTERRUPTED","CANCELLED"}:stats["failure"]+=1
      if row.get("total_latency_ms") is not None:stats["latencies"].append(float(row["total_latency_ms"]))
    stage_started=time.perf_counter();scored=[]
    for item in eligible:
      model_id=str(item["id"]).strip();stats=evidence.get(model_id,{"success":0,"failure":0,"latencies":[]})
      explicit_channels=[]
      if isinstance(item.get("channel_ids"),list):
        explicit_channels.extend(str(value).strip() for value in item["channel_ids"] if str(value).strip())
      if isinstance(item.get("channels"),list):
        explicit_channels.extend(str(value.get("channel_id") or "").strip()
          for value in item["channels"] if isinstance(value,dict) and str(value.get("channel_id") or "").strip())
      if str(item.get("channel_id") or "").strip():explicit_channels.append(str(item["channel_id"]).strip())
      explicit_channels=sorted(set(explicit_channels))
      avg_latency=(sum(stats["latencies"])/len(stats["latencies"])) if stats["latencies"] else None
      score=(stats["success"]*10000)-(stats["failure"]*5000)-(avg_latency or 100000)/1000
      scored.append({"model_id":model_id,"score":round(score,6),
        "successful_live_samples":stats["success"],"failed_live_samples":stats["failure"],
        "average_latency_ms":round(avg_latency,3) if avg_latency is not None else None,
        "channel_id":explicit_channels[0] if explicit_channels else None,
        "channel_source":"catalog_explicit_mapping" if explicit_channels else "unknown",
        "capability_status":"pending_allowed_for_text",
        "reason":"recent_live_success_evidence" if stats["success"] else "real_catalog_pending_validation"})
    stage_durations["scoring"]=(time.perf_counter()-stage_started)*1000
    stage_started=time.perf_counter()
    scored.sort(key=lambda row:(-row["score"],row["model_id"]))
    stage_durations["deterministic_sort"]=(time.perf_counter()-stage_started)*1000
    stage_started=time.perf_counter()
    excluded=[{"model_id":str(item.get("id") or ""),"reason":"catalog_execution_disabled"}
      for item in catalog_rows if item.get("execution_allowed") is False]
    stage_durations["constraint_filter"]=(time.perf_counter()-stage_started)*1000
    return scored[0]["model_id"],{"policy":"latency_first","selected_model":scored[0]["model_id"],
      "selected_channel":scored[0].get("channel_id"),
      "selection_reason":scored[0]["reason"],"confidence":"observed" if scored[0]["successful_live_samples"] else "low",
      "candidates":scored[:20],"excluded":excluded[:20],"catalog_candidate_count":len(eligible),
      "performance_stages_ms":stage_durations}

def _automatic_route_catalog(settings:UatSettings,api_key:str)->dict[str,Any]:
    catalog=MODEL_CATALOG.cached(api_key,settings.environment_id)
    if catalog:return catalog
    spec=normalize_request({"method":"GET","path":"/v1/models",
      "auth":{"method":"bearer","header_name":"Authorization","prefix":"Bearer"},
      "body":{"type":"none"},"timeout_seconds":min(settings.timeout_seconds,10)},settings)
    catalog,_=MODEL_CATALOG.fetch(api_key,
      lambda *_args:WORKBENCH_TRANSPORT(spec,api_key),
      min(settings.timeout_seconds,10),refresh=True,
      environment_id=settings.environment_id,
      environment_name="国内 UAT" if settings.environment_id=="china_uat" else settings.environment_id,
      models_url=settings.base_url.rstrip("/")+"/v1/models")
    if catalog.get("catalog_status")!="ready":
      error=catalog.get("error") if isinstance(catalog.get("error"),dict) else {}
      raise HTTPException(502,detail={"code":str(error.get("code") or "UAT_MODEL_CATALOG_UNAVAILABLE"),
        "message":str(error.get("message") or "真实 UAT 模型目录暂时不可用。")})
    return catalog

def _active_acceptance_run_id(environment_id:str)->str|None:
    run=current_acceptance_runs().active_run(environment_id)
    return str(run["acceptance_run_id"]) if run else None

def _fault_aware_transport(environment_id:str,execution_body:dict[str,Any],
                           live_transport:Callable[[dict[str,Any],str],dict[str,Any]]):
    """Wrap the real UAT boundary with explicitly scoped, auditable test faults."""
    run_id=str(execution_body.get("acceptance_run_id") or "").strip() or _active_acceptance_run_id(environment_id)
    if run_id:execution_body["acceptance_run_id"]=run_id
    model=str(execution_body.get("model") or "non_model_uat_http")
    service=current_uat_fault_injection()

    def synthetic(outcome:dict[str,Any])->dict[str,Any]:
      kind=str(outcome["error_type"]);delay=int(outcome.get("delay_ms") or 0)
      if delay:time.sleep(delay/1000)
      if kind=="connection_timeout":raise TimeoutError("uat_fault_injected_connection_timeout")
      if kind=="connection_reset":raise ConnectionResetError("uat_fault_injected_connection_reset")
      if kind=="delayed_response":return {}
      if kind=="empty_response":
        return {"status":200,"headers":{"content-type":"application/json"},"body":b"",
          "elapsed_ms":delay,"_fault_metadata":outcome}
      status=int(kind) if kind.isdigit() else 502
      payload={"error":{"code":f"uat_injected_{kind}",
        "message":"UAT controlled fault injection"}}
      return {"status":status,"headers":{"content-type":"application/json"},
        "body":json.dumps(payload).encode(),"elapsed_ms":delay,"_fault_metadata":outcome}

    def transport(spec:dict[str,Any],key:str)->dict[str,Any]:
      pre=service.pre_request_outcome(environment_id=environment_id,model=model,
        acceptance_run_id=run_id)
      if pre.get("is_fault_injected"):
        if pre.get("error_type")!="delayed_response":return synthetic(pre)
        delay=int(pre.get("delay_ms") or 0)
        if delay:time.sleep(delay/1000)
      observed=live_transport(spec,key)
      layer=(service.stream_outcome if spec.get("stream") else service.post_response_outcome)
      post=layer(environment_id=environment_id,model=model,acceptance_run_id=run_id)
      if not post.get("is_fault_injected"):
        if pre.get("is_fault_injected"):observed["_fault_metadata"]=pre
        return observed
      kind=str(post.get("error_type"))
      if kind in {"usage_missing","usage_mismatch"}:
        try:
          payload=json.loads((observed.get("body") or b"{}").decode("utf-8","replace"))
          if kind=="usage_missing":payload.pop("usage",None)
          else:payload["usage"]={"prompt_tokens":1,"completion_tokens":1,"total_tokens":999999}
          observed["body"]=json.dumps(payload).encode()
        except Exception:
          pass
        observed["_fault_metadata"]=post;return observed
      if kind=="sse_malformed":
        return {"status":200,"headers":{"content-type":"text/event-stream"},
          "body":b"data: {malformed\n\n","elapsed_ms":observed.get("elapsed_ms"),
          "_fault_metadata":post}
      if kind=="stream_interrupted":
        raise WorkbenchError("uat_fault_injected_stream_interrupted",502)
      return synthetic(post)
    return transport

@app.post("/api/v1/environments/{environment_id}/request-workbench/execute")
def execute_request_workbench(environment_id:str,body:dict[str,Any],request:Request):
    settings=_workbench_settings(environment_id,request)
    auth_session_id=str(body.get("auth_session_id") or "")
    sid,key,meta,current_auth_session_id=_temporary_workbench_credential(
      request,environment_id,auth_session_id)
    fingerprint=str((meta or {}).get("key_fingerprint") or "")
    verified=WORKBENCH_CONNECTIONS.verified(
      sid,environment_id,fingerprint,str(current_auth_session_id or ""))
    execution_body=dict(body)
    active_versions=current_config_review(request).versions()
    active_configuration=next((item for item in active_versions if item.get("is_active")),None)
    if active_configuration:
      execution_body["_configuration_version"]=active_configuration["configuration_version"]
    acceptance_run_id=str(execution_body.get("acceptance_run_id") or "").strip() or _active_acceptance_run_id(environment_id)
    if acceptance_run_id:execution_body["acceptance_run_id"]=acceptance_run_id
    selection_mode=str(execution_body.get("model_selection_mode") or "specified").casefold()
    routing_decision=None
    if (selection_mode=="automatic" and not str(execution_body.get("model") or "").strip()
        and str(execution_body.get("path") or "").strip()=="/v1/chat/completions"):
      catalog=_automatic_route_catalog(settings,key or "")
      selected_model,routing_decision=_automatic_live_model_decision(
        environment_id,catalog,current_call_logs(request))
      execution_body["model"]=selected_model
      if routing_decision.get("selected_channel"):
        execution_body["channel_id"]=routing_decision["selected_channel"]
        execution_body["_channel_source"]="scheduler_decision"
      body_spec=dict(execution_body.get("body") or {})
      if body_spec.get("type")=="json":
        raw_value=body_spec.get("value")
        try: payload=json.loads(raw_value) if isinstance(raw_value,str) else dict(raw_value or {})
        except (json.JSONDecodeError,TypeError,ValueError) as exc:
          raise HTTPException(400,detail={"code":"UAT_JSON_BODY_INVALID",
            "message":"请求正文不是有效 JSON。"}) from exc
        payload["model"]=selected_model
        execution_body["body"]={**body_spec,"value":payload}
    execution_body["_tenant_id"]=str(getattr(request_principal(request),"tenant_id",None) or "tenant_local_dev_v1")
    if routing_decision is not None:
      execution_body["_routing_decision"]=routing_decision
      execution_body["_scheduler_stage_durations"]=routing_decision.get("performance_stages_ms") or {}
    capabilities=current_uat_output_capabilities(request).catalog(
      str(execution_body.get("channel_id") or "unified-routing"))
    request_type=str(execution_body.get("request_type") or "text").strip().casefold()
    if request_type != "text":
      requirement={"image":"image_in","audio":"audio","video":"video_in"}.get(request_type)
      model_id=str(execution_body.get("model") or "").strip()
      channel_id=str(execution_body.get("channel_id") or "").strip()
      state="unknown"
      if requirement and model_id and channel_id:
        resolved=current_capability_evidence().resolve(
          environment_id=environment_id,
          subject_id=f"model:{model_id}|channel:{channel_id}",subject_version="1",
          requirement=requirement,decision_source="live")
        state=str(resolved.get("state") or "unknown")
      if state=="unsupported":
        raise HTTPException(409,detail={"code":"CAPABILITY_UNSUPPORTED",
          "message":"已有明确证据证明当前模型与渠道不支持此请求类型。"})
      confirmed=bool((execution_body.get("execution_limits") or {}).get(
        "confirm_unverified_capability"))
      if state!="supported" and not confirmed:
        raise HTTPException(409,detail={"code":"UNVERIFIED_CAPABILITY_CONFIRMATION_REQUIRED",
          "message":"能力信息尚未审核；确认风险后可通过真实 UAT 调用验证。"})
    try:
      result=execute_workbench(execution_body,settings,key or "",connection_verified=verified,
        call_logs=current_call_logs(request),capabilities=capabilities,
        transport=_fault_aware_transport(environment_id,execution_body,WORKBENCH_TRANSPORT),
        performance_recorder=current_scheduler_overhead().recorder)
    except WorkbenchError as exc:
      raise HTTPException(exc.status_code,detail={"code":exc.code,
        "message":"UAT 请求未通过安全校验或传输失败。"}) from exc
    if routing_decision is not None:result["routing_decision"]=routing_decision
    current_call_logs(request).save_routing_decision(
      result=result,routing_decision=routing_decision)
    if acceptance_run_id:
      current_acceptance_runs().link_evidence(acceptance_run_id,
        request_id=result.get("request_id"),decision_id=result.get("decision_id"),
        fault_id=result.get("fault_id"),audit_id=result.get("fault_audit_id"))
    if result.get("execution_status")=="success" and execution_body.get("model"):
      requirement={"text":"text","image":"image_in","audio":"audio",
        "video":"video_in"}.get(request_type,"text")
      observed_model=str(result.get("actual_model") or execution_body.get("model"))
      observed_channel=str(result.get("channel_id") or execution_body.get("channel_id") or "").strip()
      subject_id=(f"model:{observed_model}|channel:{observed_channel}"
        if observed_channel else f"model:{observed_model}")
      current_capability_evidence().record_evidence({
        "evidence_id":f"CAP-LIVE-{uuid.uuid4().hex.upper()}",
        "evidence_type":"measured","observed_at":str(result["created_at"]),
        "environment_id":environment_id,"subject_id":subject_id,
        "subject_version":"1","source":"live","requirement":requirement,
        "state":"supported","details":{"request_id":result["request_id"],
          "http_status":result.get("http_status"),"method":result.get("method"),
          "channel_observed":bool(observed_channel)}})
      result["capability_evidence"]={"state":"supported","subject_id":subject_id,
        "requirement":requirement,"source":"live"}
    if str(execution_body.get("method") or "").upper() in {"PUT","PATCH","DELETE"}:
      ENTERPRISE_HTTP.audit_action(request_principal(request),"uat.execute",
        "completed",f"workbench_write:{str(execution_body.get('method')).upper()}:{result['request_id']}")
    return result

@app.post("/api/v1/environments/{environment_id}/request-workbench/execute-stream")
def execute_request_workbench_stream(environment_id:str,body:dict[str,Any],request:Request):
    """Relay safe text deltas while preserving the normal execution/logging path."""
    settings=_workbench_settings(environment_id,request)
    auth_session_id=str(body.get("auth_session_id") or "")
    sid,key,meta,current_auth_session_id=_temporary_workbench_credential(
      request,environment_id,auth_session_id)
    execution_body=dict(body)
    acceptance_run_id=str(execution_body.get("acceptance_run_id") or "").strip() or _active_acceptance_run_id(environment_id)
    if acceptance_run_id:execution_body["acceptance_run_id"]=acceptance_run_id
    if execution_body.get("stream") is not True:
      raise HTTPException(400,detail={"code":"STREAM_MODE_REQUIRED",
        "message":"流式执行接口要求 stream=true。"})
    selection_mode=str(execution_body.get("model_selection_mode") or "specified").casefold()
    routing_decision=None
    if (selection_mode=="automatic" and not str(execution_body.get("model") or "").strip()
        and str(execution_body.get("path") or "").strip()=="/v1/chat/completions"):
      catalog=_automatic_route_catalog(settings,key or "")
      selected_model,routing_decision=_automatic_live_model_decision(
        environment_id,catalog,current_call_logs(request))
      execution_body["model"]=selected_model
      body_spec=dict(execution_body.get("body") or {})
      raw=body_spec.get("value")
      payload=json.loads(raw) if isinstance(raw,str) else dict(raw or {})
      payload["model"]=selected_model;payload["stream"]=True
      execution_body["body"]={**body_spec,"value":payload}
    stream_principal=request_principal(request)
    execution_body["_tenant_id"]=str(getattr(stream_principal,"tenant_id",None) or "tenant_local_dev_v1")
    if routing_decision is not None:
      execution_body["_routing_decision"]=routing_decision
      execution_body["_scheduler_stage_durations"]=routing_decision.get("performance_stages_ms") or {}
    fingerprint=str((meta or {}).get("key_fingerprint") or "")
    verified=WORKBENCH_CONNECTIONS.verified(
      sid,environment_id,fingerprint,str(current_auth_session_id or ""))
    capabilities=current_uat_output_capabilities(request).catalog(
      str(execution_body.get("channel_id") or "unified-routing"))
    events:queue.Queue[dict[str,Any]]=queue.Queue(maxsize=128)
    cancelled=threading.Event();completed=threading.Event();buffer=""
    principal=request_principal(request)
    skill_runtime=current_formal_agent_skill_runtime(request)
    skill_arguments={
      "environment_id":environment_id,
      "method":str(execution_body.get("method") or "POST").upper(),
      "endpoint":settings.base_url.rstrip("/")+"/"+str(
        execution_body.get("path") or "").lstrip("/"),
      "headers":list(execution_body.get("headers") or []),
      "body":execution_body.get("body"),
      "model_id":str(execution_body.get("model") or ""),
    }
    try:
      skill_context=skill_runtime.begin_managed_invocation(
        skill_id="execute-uat-request",arguments=skill_arguments,
        actor_id=str(getattr(principal,"principal_id",None) or "local-console-operator"),
        principal=principal)
    except Exception as exc:
      raise HTTPException(403,detail={"code":"permission_denied",
        "message":"当前身份无权通过 Agent Skill 执行真实 UAT 请求。"}) from exc

    def emit_chunks(chunk:bytes)->None:
      nonlocal buffer
      if cancelled.is_set():
        raise WorkbenchError("uat_stream_cancelled",499)
      buffer+=chunk.decode("utf-8","replace")
      lines=buffer.split("\n");buffer=lines.pop()
      for raw_line in lines:
        line=raw_line.strip()
        if not line.startswith("data:"):
          continue
        value=line[5:].strip()
        if not value or value=="[DONE]":
          continue
        try:payload=json.loads(value)
        except json.JSONDecodeError:continue
        choices=payload.get("choices") if isinstance(payload,dict) else None
        first=choices[0] if isinstance(choices,list) and choices else {}
        delta=first.get("delta") if isinstance(first,dict) else {}
        content=(delta.get("content") or delta.get("reasoning_content")) if isinstance(delta,dict) else None
        if isinstance(content,str) and content:
          events.put({"type":"delta","content":content})

    def worker()->None:
      try:
        events.put({"type":"stage","stage":"calling","message":"正在调用国内 UAT"})
        result=execute_workbench(execution_body,settings,key or "",connection_verified=verified,
          call_logs=current_call_logs(request),capabilities=capabilities,
          transport=_fault_aware_transport(environment_id,execution_body,
            lambda spec,credential:WORKBENCH_STREAM_TRANSPORT(spec,credential,emit_chunks)),
          performance_recorder=current_scheduler_overhead().recorder)
        if routing_decision is not None:result["routing_decision"]=routing_decision
        current_call_logs(request).save_routing_decision(
          result=result,routing_decision=routing_decision)
        if acceptance_run_id:
          current_acceptance_runs().link_evidence(acceptance_run_id,
            request_id=result.get("request_id"),decision_id=result.get("decision_id"),
            fault_id=result.get("fault_id"),audit_id=result.get("fault_audit_id"))
        if result.get("execution_status")=="success" and execution_body.get("model"):
          observed_model=str(result.get("actual_model") or execution_body.get("model"))
          observed_channel=str(result.get("channel_id") or "").strip()
          subject_id=(f"model:{observed_model}|channel:{observed_channel}"
            if observed_channel else f"model:{observed_model}")
          current_capability_evidence().record_evidence({
            "evidence_id":f"CAP-LIVE-{uuid.uuid4().hex.upper()}","evidence_type":"measured",
            "observed_at":str(result["created_at"]),"environment_id":environment_id,
            "subject_id":subject_id,"subject_version":"1","source":"live",
            "requirement":"text","state":"supported","details":{
              "request_id":result["request_id"],"http_status":result.get("http_status"),
              "method":result.get("method"),"channel_observed":bool(observed_channel)}})
          result["capability_evidence"]={"state":"supported","subject_id":subject_id,
            "requirement":"text","source":"live"}
        ENTERPRISE_HTTP.audit_action(principal,"uat.execute","completed",
          f"workbench_stream:{result['request_id']}")
        managed=skill_runtime.finish_managed_invocation(
          skill_context,{**result,"network_called":True,"write_performed":False},
          status="success" if result.get("execution_status")=="success" else "failed")
        result["invocation_id"]=managed["invocation_id"]
        result["audit_id"]=managed["audit_id"]
        result["skill_trace"]={
          "skill_id":managed["skill_id"],"skill_version":managed["skill_version"],
          "invocation_id":managed["invocation_id"],"audit_id":managed["audit_id"],
          "permission_decision":managed["permission_decision"]}
        events.put({"type":"result","result":result})
      except WorkbenchError as exc:
        skill_runtime.finish_managed_invocation(skill_context,{
          "status":"failed","network_called":True,"write_performed":False,
          "error_code":exc.code,"http_status":exc.status_code},status="failed")
        events.put({"type":"error","code":exc.code,"status":exc.status_code,
          "message":"流式 UAT 请求未通过安全校验或传输失败。"})
      except Exception:
        try:
          skill_runtime.finish_managed_invocation(skill_context,{
            "status":"failed","network_called":False,"write_performed":False,
            "error_code":"uat_stream_failed"},status="failed")
        except Exception:
          pass
        events.put({"type":"error","code":"uat_stream_failed","status":502,
          "message":"流式 UAT 请求未完成。"})
      finally:completed.set()

    threading.Thread(target=worker,name="uat-workbench-stream",daemon=True).start()
    async def generate():
      try:
        while not (completed.is_set() and events.empty()):
          try:event=await asyncio.to_thread(events.get,True,.25)
          except queue.Empty:continue
          yield json.dumps(event,ensure_ascii=False,separators=(",",":"))+"\n"
      finally:cancelled.set()
    return StreamingResponse(generate(),media_type="application/x-ndjson",
      headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})
@app.delete("/api/v1/uat/credentials/session")
def clear_session_credential(request:Request,response:Response):
    _secure_credential_request(request)
    principal=request_principal(request);sid=request.cookies.get(COOKIE_NAME)
    legacy=SESSION_CREDENTIALS.clear(sid)
    try: persisted=PERSISTENT_CREDENTIALS.delete(_credential_scope(request)) if PERSISTENT_CREDENTIALS else False
    except CredentialVaultError as exc:
      raise HTTPException(503,detail={"code":"CREDENTIAL_DELETE_FAILED",
        "message":"The encrypted credential could not be cleared."}) from exc
    cleared=legacy or persisted
    response.delete_cookie(COOKIE_NAME,path="/",secure=COOKIE_SECURE,samesite="strict")
    _audit_credential("credential_encrypted_cleared",principal.session_id,cleared,"windows_encrypted_vault")
    return {"configured":False,"credential_source":"environment" if UatSettings.load().api_key else "none","cleared":cleared}
@app.post("/api/v1/uat/credentials/test")
def test_session_credential(request:Request):
    _secure_credential_request(request)
    settings,source,sid,_=_resolved(request)
    if not settings.api_key:
      _audit_credential("credential_resolution_failed",sid,False,source,"credential_not_configured")
      raise HTTPException(409,detail={"code":"CREDENTIAL_NOT_CONFIGURED","message":"No UAT credential is configured."})
    target="api-uat.weimeta.cn"
    if settings.base_url!="https://api-uat.weimeta.cn" or target not in settings.allowed_hosts:
      return {"connection_status":"test_not_supported","http_status":None,"tested_at":datetime.now(timezone.utc).isoformat(),"target_host":target,"credential_source":source,"model_count":0,"message":"Connection test target is not supported."}
    try:
      result=MODELS_TRANSPORT("https://api-uat.weimeta.cn/v1/models",settings.api_key,min(settings.timeout_seconds,10))
      code=result.get("status")
      if code==200:
        try:
          parsed=json.loads(result.get("body",b"{}")[:1_000_000]); count=len(parsed.get("data",[])) if isinstance(parsed,dict) and isinstance(parsed.get("data",[]),list) else 0
          state,message="success","UAT credential was accepted."
        except (ValueError,UnicodeDecodeError): state,message,count="invalid_response","Provider returned an invalid response.",0
      elif code==401: state,message,count="authentication_failed","UAT credential was rejected.",0
      elif code==403: state,message,count="forbidden","UAT credential is not permitted for this endpoint.",0
      else: state,message,count="endpoint_unavailable","UAT model endpoint is unavailable.",0
    except (TimeoutError,urllib.error.URLError):
      code,state,message,count=None,"timeout","Connection test timed out.",0
    _audit_credential("credential_connection_tested",sid,state=="success",source,state,target)
    return {"connection_status":state,"http_status":code,"tested_at":datetime.now(timezone.utc).isoformat(),
            "target_host":target,"credential_source":source,"model_count":count,"message":message}
@app.get("/api/v1/uat/editor-options")
def get_uat_editor_options(request:Request,channel_id:str=Query(default="unified-routing",min_length=1,max_length=128)):
    with (ROOT/"data"/"request_profiles_v2.csv").open(encoding="utf-8-sig",newline="") as handle:
      profiles=[]
      for row in csv.DictReader(handle):
       if row["request_profile_id"] not in {"P01","P02","P03","P04"}: continue
       blocked=row["request_profile_id"]=="P04"
       profiles.append({"id":row["request_profile_id"],"name":row["profile_name"],"prompt":row["prompt_template"],
         "stream":row["stream"]=="TRUE","default_max_tokens":int(row["requested_max_tokens"]),
         "purpose":row["purpose"],"expected_input_scale":row["expected_input_scale"],
         "expected_output_scale":row["expected_output_scale"],"status":"blocked_capability" if blocked else "available",
         "blocking_reason":"stream_execution_not_ready" if blocked else None})
    cfg=json.loads((ROOT/"config"/"uat_execution_policy_v1.json").read_text(encoding="utf-8"))
    allowed=cfg.get("fallback_allowed_models",[])
    return {"profiles":profiles,"models":{
      "access_mode":cfg["model_access_mode"],"fallback_allowed":allowed,
      "explanation":"可执行模型来自当前 UAT API Key 最近成功读取的 /v1/models 目录。"},
      "stream_execution_ready":False,"streaming_explanation":"真实流式执行尚未通过安全验收，当前不可启用。",
      "policy_version":cfg["policy_version"],"maximum_max_tokens":cfg["maximum_max_tokens"],
      "token_presets":[2048,4096,8192,12288,16384],
      "model_output_capabilities":list(uat_model_capabilities(request,channel_id).values()),
      "selected_channel":channel_id,"six_model_ids":list(UAT_SIX_MODEL_IDS)}

@app.get("/api/v1/uat/output-capabilities")
def get_uat_output_capabilities(request:Request,
    channel_id:str=Query(default="unified-routing",min_length=1,max_length=128)):
    service=current_uat_output_capabilities(request)
    return {"status":"ready","channel_id":channel_id,
      "models":list(service.catalog(channel_id).values()),
      "audit_log":service.audit(50)}

@app.post("/api/v1/uat/output-capabilities/review")
def review_uat_output_capability(body:dict[str,Any],request:Request):
    principal=request_principal(request)
    try:
      item=current_uat_output_capabilities(request).review(
        body,reviewed_by=principal.principal_id)
      return {"status":"reviewed","capability":item}
    except UatOutputCapabilityError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"模型或渠道能力证据未通过审查。"}) from exc

@app.get("/api/v1/uat/execution-control")
def get_uat_execution_control(request:Request):
    return current_uat_execution_control(request).status()

@app.post("/api/v1/uat/execution-control")
def create_uat_execution_control(body:dict[str,Any],request:Request):
    try:return current_uat_execution_control(request).create(body)
    except UatExecutionControlError as exc:
      raise HTTPException(409,detail={"code":str(exc),"message":"受控真实执行任务未能创建。"}) from exc

@app.post("/api/v1/uat/execution-control/{task_id}/approve")
def approve_uat_execution_control(task_id:str,body:dict[str,Any],request:Request):
    try:return current_uat_execution_control(request).approve(
      task_id,approval_reference=str(body.get("approval_reference") or ""),
      approval_expires_at=str(body.get("approval_expires_at") or ""))
    except UatExecutionControlError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"受控真实执行任务未能批准。"}) from exc

@app.post("/api/v1/uat/execution-control/{task_id}/activate")
def activate_uat_execution_control(task_id:str,request:Request):
    try:return current_uat_execution_control(request).activate(task_id)
    except UatExecutionControlError as exc:
      raise HTTPException(409,detail={"code":str(exc),
        "message":"受控真实执行任务未能激活。"}) from exc

@app.post("/api/v1/uat/execution-control/stop")
def stop_uat_execution_control(body:dict[str,Any],request:Request):
    try:return current_uat_execution_control(request).stop(
      str(body.get("reason") or ""),kill_switch=body.get("kill_switch") is True)
    except UatExecutionControlError as exc:
      raise HTTPException(409,detail={"code":str(exc),"message":"受控真实执行任务未能停止。"}) from exc
@app.post("/api/v1/uat/validate-request")
def validate_uat_request(body:dict[str,Any],request:Request):
    settings,_,_,_=_resolved(request)
    channel=str((body.get("request") or {}).get("channel_id") or "unified-routing")
    return validate_uat(body,settings,current_uat_store(),
      executable_model_catalog(settings,request,channel),_uat_runtime_constraints(request))
@app.post("/api/v1/environments/{environment_id}/validate-request")
def validate_environment_request(environment_id:str,body:dict[str,Any],request:Request):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    settings,_,_,_=_resolved(request,environment_id)
    ids=MODEL_CATALOG.model_ids(settings.api_key,environment_id)
    channel=str((body.get("request") or {}).get("channel_id") or "unified-routing")
    catalog=None if ids is None else {x:uat_model_capabilities(request,channel).get(x,{
      "confirmed_max_output_tokens":None,"confirmed_channel_max_output_tokens":None,
      "evidence_source":"pending_confirmation"}) for x in ids}
    return validate_uat(body,settings,current_uat_store(),catalog,_uat_runtime_constraints(request))
@app.post("/api/v1/uat/estimate-cost")
def estimate_uat_cost(body:dict[str,Any]):
    request=body.get("request",body)
    return estimate_cost(request.get("messages",[]),int(request.get("max_tokens") or 0))
@app.post("/api/v1/uat/execute")
def execute_uat_request(body:dict[str,Any],request:Request):
    settings,source,sid,_=_resolved(request)
    valid=(lambda: _persistent_credential(request) is not None) if source=="windows_encrypted_vault" else \
      ((lambda: SESSION_CREDENTIALS.metadata(sid) is not None) if source=="session" else None)
    channel=str((body.get("request") or {}).get("channel_id") or "unified-routing")
    catalog=executable_model_catalog(settings,request,channel)
    constraints=_uat_runtime_constraints(request)
    check=validate_uat(body,settings,current_uat_store(),catalog,constraints)
    if not check["valid"]:
      raise HTTPException(403,detail={"code":check["blocking_reasons"][0],"message":"真实 UAT 执行被安全策略阻止。","details":{"validation":check}})
    control=current_uat_execution_control(request)
    try:reservation=control.reserve(
      str(body["request"]["requested_model"]),channel,
      Decimal(str(check["cost_estimate"]["estimated_cost_cny"])))
    except UatExecutionControlError as exc:
      raise HTTPException(403,detail={"code":str(exc),"message":"受控真实执行额度不可用。"}) from exc
    try:
      result=execute_uat(body,settings,current_uat_store(),build_uat_shadow,UAT_TRANSPORT,
        source,valid,catalog,constraints,current_call_logs(request))
    finally:
      control.complete(reservation["reservation_id"],"completed")
    if not result["validation"]["valid"]:
      raise HTTPException(403,detail={"code":result["validation"]["blocking_reasons"][0],"message":"真实 UAT 执行被安全策略阻止。","details":result})
    return result
@app.post("/api/v1/environments/{environment_id}/execute")
def execute_environment_request(environment_id:str,body:dict[str,Any],request:Request):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    settings,source,sid,_=_resolved(request,environment_id)
    valid=(lambda: _persistent_credential(request,environment_id) is not None) if source=="windows_encrypted_vault" else \
      ((lambda: SESSION_CREDENTIALS.metadata(sid,environment_id=environment_id) is not None) if source=="session" else None)
    ids=MODEL_CATALOG.model_ids(settings.api_key,environment_id)
    channel=str((body.get("request") or {}).get("channel_id") or "unified-routing")
    capabilities=uat_model_capabilities(request,channel)
    catalog=None if ids is None else {x:capabilities.get(x,{
      "confirmed_max_output_tokens":None,"confirmed_channel_max_output_tokens":None,
      "evidence_source":"pending_confirmation"}) for x in ids}
    reservation=None
    if environment_id=="china_uat":
      constraints=_uat_runtime_constraints(request)
      check=validate_uat(body,settings,current_uat_store(),catalog,constraints)
      if not check["valid"]:
        raise HTTPException(403,detail={"code":check["blocking_reasons"][0],
          "message":"Real API execution was blocked by environment-aware safety policy.",
          "details":{"validation":check}})
      try:
        reservation=current_uat_execution_control(request).reserve(
          str(body["request"]["requested_model"]),channel,
          Decimal(str(check["cost_estimate"]["estimated_cost_cny"])))
      except UatExecutionControlError as exc:
        raise HTTPException(403,detail={"code":str(exc),
          "message":"Controlled UAT execution capacity is unavailable."}) from exc
    try:
      result=execute_uat(body,settings,current_uat_store(),build_uat_shadow,UAT_TRANSPORT,
        source,valid,catalog,constraints if environment_id=="china_uat" else None,
        current_call_logs(request))
    finally:
      if reservation is not None:
        current_uat_execution_control(request).complete(
          reservation["reservation_id"],"completed")
    if not result["validation"]["valid"]:
      raise HTTPException(403,detail={"code":result["validation"]["blocking_reasons"][0],
        "message":"Real API execution was blocked by environment-aware safety policy.","details":result})
    return result
@app.get("/api/v1/uat/executions")
def list_uat_executions(): return {"items":current_uat_store().list()}
@app.get("/api/v1/call-logs")
def list_call_logs(request:Request,after_cursor:int=Query(0,ge=0),
                   limit:int=Query(100,ge=1,le=1000),
                   environment_id:str|None=Query(None),
                   source_type:str|None=Query(None)):
    return current_call_logs(request).list_records(
      after_cursor=after_cursor,limit=limit,environment_id=environment_id,
      source_type=source_type)
@app.get("/api/v1/call-logs/analytics")
def call_log_analytics(request:Request,environment_id:str=Query("china_uat"),
                       model:str|None=Query(None),source_type:str|None=Query(None),
                       channel_id:str|None=Query(None),provider:str|None=Query(None),
                       request_status:str|None=Query(None),stream:bool|None=Query(None),
                       occurred_from:str|None=Query(None),occurred_to:str|None=Query(None),
                       traffic_class:str=Query("business",pattern="^(business|probe|all)$")):
    return current_call_logs(request).analytics(
        environment_id=environment_id,model=model,source_type=source_type,
        channel_id=channel_id,provider=provider,request_status=request_status,
        stream=stream,occurred_from=occurred_from,occurred_to=occurred_to,
        traffic_class=traffic_class)
@app.get("/api/v1/observability/overview")
def observability_overview(
    request:Request,environment_id:str=Query("china_uat"),
    time_range:str=Query("24h",pattern="^(5m|1h|24h|7d|custom)$"),
    start:str|None=Query(None),end:str|None=Query(None),
    traffic_class:str=Query("business",pattern="^(business|probe|all)$"),
    model_id:str|None=Query(None),stream:bool|None=Query(None),
    source_type:str=Query("all",pattern="^(historical|realtime|all)$")):
    try:
        return ObservabilityOverviewService(current_call_logs(request)).overview(
          environment_id=environment_id,time_range=time_range,start=start,end=end,
          traffic_class=traffic_class,model_id=model_id,stream=stream,
          source_type=source_type,circuits=current_circuit_breakers().list_states(limit=20))
    except ValueError as exc:
        raise HTTPException(400,detail={"code":str(exc)}) from exc
@app.get("/api/v1/call-logs/import-status")
def call_log_import_status(request:Request):
    report=current_call_logs(request).latest_import_report()
    return {"status":"ready" if report else "not_initialized","report":report}
@app.get("/api/v1/routing-decisions")
def list_routing_decisions(request:Request,request_id:str|None=Query(None),
                           limit:int=Query(100,ge=1,le=500)):
    return current_call_logs(request).list_routing_decisions(
      request_id=request_id,limit=limit)
@app.get("/api/v1/routing-decisions/{decision_id}")
def get_routing_decision(decision_id:str,request:Request):
    item=current_call_logs(request).routing_decision(decision_id)
    if not item:
      raise HTTPException(404,detail={"code":"ROUTING_DECISION_NOT_FOUND",
        "message":"未找到对应的真实调度决策。"})
    return item
@app.get("/api/v1/environments/{environment_id}/executions")
def list_environment_executions(environment_id:str):
    if not load_platform_environments().get(environment_id):
      raise HTTPException(404,detail={"code":"environment_not_supported","message":"Environment is not supported."})
    return {"environment_id":environment_id,"items":current_uat_store().list(environment_id)}
@app.get("/api/v1/uat/executions/{execution_id}")
def get_uat_execution(execution_id:str):
    item=current_uat_store().get(execution_id)
    if not item: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"execution not found"})
    return item
@app.get("/api/v1/uat/progress")
def uat_progress():
    store=current_uat_store();items=store.list()
    return {"execution_count":len(items),"requests_used_today":store.usage_today()[0],"genuine_imported_record_count":sum(1 for r in STORE.records() if r.get("source_type") in {"measured_uat","measured_unified_uat","backend_log_export"})}
@app.post("/api/v1/uat/executions/{execution_id}/attach-backend-evidence")
def attach_backend_evidence(execution_id:str,body:dict[str,Any]):
    store=current_uat_store();item=store.get(execution_id)
    if not item: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"execution not found"})
    logs=[r for r in STORE.records() if r.get("source_type")=="backend_log_export"]
    result=correlate(item,logs,str(body.get("platform_log_id") or "") or None)
    store.save_correlation(result)
    return result
@app.post("/api/v1/uat/executions/{execution_id}/confirm-correlation")
def confirm_uat_correlation(execution_id:str,body:dict[str,Any]):
    store=current_uat_store();item=store.get(execution_id)
    if not item or not item.get("correlation"): raise HTTPException(409,detail={"code":"CORRELATION_NOT_READY","message":"correlation must be attached first"})
    result={**item["correlation"],"correlation_status":"manually_confirmed","human_confirmed":True,
      "confirmed_by":str(body.get("confirmed_by") or "operator"),"confirmed_at":datetime.now(timezone.utc).isoformat()}
    store.save_correlation(result)
    return result
@app.get("/api/v1/overview")
def overview(request:Request,mode:str=Query("uat"),environment_id:str=Query("china_uat"),
             time_range:str=Query("all",pattern="^(24h|7d|30d|all)$")):
    if mode=="uat":
      selected_environment=environment_id if environment_id in {"china_uat","overseas"} else "china_uat"
      range_days={"24h":1,"7d":7,"30d":30}
      occurred_from=(datetime.now(timezone.utc)-timedelta(
        days=range_days[time_range])).isoformat() if time_range in range_days else None
      snapshot=current_call_logs(request).overview_snapshot(
        selected_environment,occurred_from=occurred_from)
      principal=request_principal(request)
      saved=current_runtime_settings().execution_state(
        selected_environment,principal=principal) if selected_environment=="china_uat" else None
      configured_enabled=UatSettings.load(selected_environment).enabled
      environment_enabled=(saved or {}).get("enabled",configured_enabled)
      try:
        settings,credential_source,_,credential_meta=_resolved(
          request,selected_environment)
        credential_meta=credential_meta or {}
        credential_configured=bool(settings.api_key)
      except Exception:
        credential_source="none";credential_meta={};credential_configured=False
      connection_status="not_tested"
      if selected_environment=="china_uat":
        try:
          sid,key,temp_meta,auth_session_id=_temporary_workbench_credential(
            request,selected_environment)
          fingerprint=(temp_meta or {}).get("key_fingerprint")
          if key and fingerprint and WORKBENCH_CONNECTIONS.verified(
              sid,selected_environment,str(fingerprint),str(auth_session_id or "")):
            connection_status="success"
          elif key:
            connection_status="not_tested"
        except Exception:
          pass
      blockers=[]
      if not environment_enabled: blockers.append({
        "code":"real_execution_disabled","message":"UAT 环境当前已关闭，请打开 UAT 环境后再执行。",
        "action_path":"/routing/execute"})
      if not credential_configured: blockers.append({
        "code":"credential_missing","message":"尚未提供可用于真实执行的 API Key。",
        "action_path":"/routing/execute?tab=auth"})
      elif connection_status!="success": blockers.append({
        "code":"connection_not_verified","message":"API Key 尚未完成当前会话的连接测试。",
        "action_path":"/routing/execute?tab=auth"})
      metrics=snapshot["metrics"]
      return {**snapshot,
        "mode":"uat","time_range":time_range,"records":metrics["request_count"],
        "sample_size":metrics["request_count"],
        "channel_summary":snapshot["channels"],"channels":len(snapshot["channels"]),
        "errors":metrics["failure_count"],
        "healthy":sum(item["success_rate"]>=.95 for item in snapshot["channels"]),
        "routing_distribution":{
          item["channel_id"]:item["request_count"] for item in snapshot["channels"]},
        "budget":{"today_uat_spend":metrics["today_cost"],"remaining_budget":None},
        "last_updated":snapshot["data_as_of"],"updated_at":snapshot["data_as_of"],
        "warnings":[item["code"] for item in blockers],"blockers":blockers,
        "runtime":{
          "environment_enabled":environment_enabled,
          "environment_state_source":(saved or {}).get("state_source","environment_configuration"),
          "credential_configured":credential_configured,
          "credential_source":credential_source,
          "credential_fingerprint":credential_meta.get("key_fingerprint"),
          "connection_status":connection_status,
          "log_sync_status":"synchronized" if snapshot["last_synced_at"] else "never_synchronized",
          "freshness_status":"available" if snapshot["last_synced_at"] else
            ("historical_only" if snapshot["data_as_of"] else "no_data"),
        },
        "evidence_id":hashlib.sha256(json.dumps({
          "environment_id":selected_environment,"data_as_of":snapshot["data_as_of"],
          "records":metrics["request_count"]},sort_keys=True).encode()).hexdigest(),
        "collection_ids":[],"configuration_version":"overview_real_evidence_v1",
        "currency":metrics["currency"],
        "limitations":[snapshot["channel_evidence_note"]],
      }
    rows=records_for(mode,environment_id)
    cards=health(rows) if mode=="demo" else uat_health(rows)
    explicit_environment=environment_id if environment_id in {"demo","china_uat","overseas"} else None
    meta=analytical_metadata(mode,environment_id,rows,
      ["汇总仅代表当前筛选范围内已载入的结构化证据。"],
      configuration_version="routing-console-analytics-v1")
    return {**meta,"data_mode":"mock","data_as_of":meta["updated_at"],
      "last_synced_at":meta["updated_at"],
      "records":len(rows),"channels":len(cards),"healthy":sum(x.get("health_state")=="healthy" for x in cards),
      "errors":sum(x.get("result")=="failure" for x in rows),
      "budget":budget(rows,environment_id=explicit_environment),
      "routing_distribution":dict(Counter(x.get("channel_id","") for x in rows if x.get("channel_id"))),
      "last_updated":meta["updated_at"],
      "warnings":[] if rows else ["尚未导入UAT证据，请先前往数据导入页面。"]}
@app.post("/api/v1/imports/preview")
async def preview(file:UploadFile=File(...),mapping:str|None=Form(None),source_type:str=Form("measured_uat"),environment_id:str=Form("china_uat")):
    return await process_upload(file,mapping,source_type,environment_id)
@app.post("/api/v1/imports/validate")
async def validate_import(file:UploadFile=File(...),mapping:str|None=Form(None),source_type:str=Form("measured_uat"),environment_id:str=Form("china_uat")):
    return await process_upload(file,mapping,source_type,environment_id)
@app.post("/api/v1/imports/confirm")
async def confirm(file:UploadFile=File(...),mapping:str|None=Form(None),source_type:str=Form("measured_uat"),environment_id:str=Form("china_uat")):
    batch=await process_upload(file,mapping,source_type,environment_id)
    try:STORE.save_batch(batch)
    except Exception as exc:
      raise HTTPException(409,detail={"code":"BATCH_EXISTS","message":str(exc)})
    current_call_logs().import_normalized_rows(
      batch.get("normalized_rows",[]),environment_id=environment_id,
      source_type=source_type,import_batch_id=batch["batch_id"])
    return batch
@app.get("/api/v1/imports")
def imports(): return {"items":STORE.batches()}
@app.get("/api/v1/imports/{batch_id}/quality-report",response_class=PlainTextResponse)
def quality_report(batch_id:str):
    batch=next((item for item in STORE.batches() if item["batch_id"]==batch_id),None)
    if not batch:raise HTTPException(404,detail={"code":"NOT_FOUND","message":"import batch not found"})
    lines=[f"# Data Quality Report — {batch_id}","",f"- Source: {batch['source_type']}",f"- SHA-256: {batch['source_sha256']}",
      f"- Rows: {batch['row_count']}",f"- Valid: {batch['valid_count']}",f"- Warnings: {batch['warning_count']}",
      f"- Rejected: {batch['rejected_count']}",f"- Needs confirmation: {batch['needs_confirmation_count']}","",
      "## Issues",*(f"- Row {issue['row']}: {issue['code']}" for issue in batch["issues"])]
    return "\n".join(lines)
@app.get("/api/v1/health/channels")
def channels(mode:str=Query("demo"),environment_id:str=Query("all")):
    rows=records_for(mode,environment_id)
    meta=analytical_metadata(mode,environment_id,rows,
      ["健康度只基于当前样本；小样本不会被描述为稳定健康。"],
      configuration_version="health-window-v1")
    return {**meta,"last_updated":meta["updated_at"],
      "items":health(rows) if mode=="demo" else uat_health(rows)}
@app.get("/api/v1/health/channels/{channel_id}")
def channel(channel_id:str,mode:str=Query("demo")):
    items=channels(mode)["items"]; item=next((x for x in items if x["channel_id"]==channel_id),None)
    if not item:raise HTTPException(404,detail={"code":"NOT_FOUND","message":"渠道不存在"})
    return item
@app.get("/api/v1/shadow/summary")
def shadow_summary(mode:str=Query("demo")):
    if mode=="uat":
      store=current_uat_store();executions=[store.get(x["execution_id"]) for x in store.list()]
      correlated=[x for x in executions if x and x.get("correlation")]
      return {**analytical_metadata("uat","all",[],
          ["缺少关联时，实际渠道只能等待已导入的后端日志；未执行渠道没有反事实结果。"],
          source_type="measured_unified_uat" if executions else None,
          configuration_version="decision-policy-v1.0.0"),
        "agreement_rate":None,"routing_concentration_hhi":None,"counterfactual_available":False,
        "sample_size":len(executions),"source_type":"measured_unified_uat" if executions else None,
        "items":[{"execution_id":x["execution_id"],"local_scheduler_recommendation":(x.get("shadow_decision") or {}).get("recommended_candidate"),
          "platform_actual_channel":x["correlation"].get("platform_actual_channel"),"correlation_status":x["correlation"].get("correlation_status"),
          "recommendation_matches_actual":(x.get("shadow_decision") or {}).get("recommended_candidate")==x["correlation"].get("platform_actual_channel"),
          "decision_limitations":["不一致不自动代表错误；平台私有规则未知。"]} for x in correlated],
        "limitation":"缺少关联时，实际渠道只能等待已导入的后端日志；未执行渠道没有反事实结果。"}
    rows=records_for("demo")
    return {**analytical_metadata("demo","all",rows,
      ["未执行渠道的反事实性能不可用。"],configuration_version="decision-policy-v1.0.0"),
      "agreement_rate":.6,"routing_concentration_hhi":.2,"counterfactual_available":False,
      "sample_size":5,"source_type":"demo_mock","limitation":"未执行渠道的反事实性能不可用。"}
@app.get("/api/v1/shadow/decisions")
def shadow_decisions(mode:str=Query("demo")):
    if mode=="uat":return {"items":[],"blocker":"missing_pre_execution_shadow_decision_linkage"}
    return {"items":[{"shadow_decision_id":"SD-001","request_id":"D002","shadow_recommended_channel":"19","platform_actual_channel":"27","recommendation_matches_actual":False,"execution_attempted_by_scheduler":False,"counterfactual_outcome_available":False}]}
@app.get("/api/v1/historical-replay/source")
def historical_replay_source(request:Request,environment_id:str="china_uat",
    occurred_from:str|None=None,occurred_to:str|None=None,model:str|None=None,
    channel:str|None=None,request_type:str|None=None,exact_only:bool=False):
    return current_historical_replay(request).source_summary({
      "environment_id":environment_id,"occurred_from":occurred_from,
      "occurred_to":occurred_to,"model":model,"channel":channel,
      "request_type":request_type,"exact_only":exact_only})
@app.post("/api/v1/historical-replay/runs")
def run_historical_replay(body:HistoricalReplayIn,request:Request):
    try:
      return current_historical_replay(request).run(
        baseline_strategy=body.baseline_strategy,
        candidate_strategy=body.candidate_strategy,
        filters={key:value for key,value in body.model_dump().items()
          if key not in {"baseline_strategy","candidate_strategy","limit"}},
        limit=body.limit)
    except HistoricalReplayError as exc:
      raise HTTPException(400,detail={"code":str(exc),
        "message":"历史回放请求未通过确定性校验。"}) from exc
@app.get("/api/v1/historical-replay/runs/{replay_id}")
def get_historical_replay(replay_id:str,request:Request):
    try:return current_historical_replay(request).get(replay_id)
    except HistoricalReplayError as exc:
      raise HTTPException(404,detail={"code":str(exc),"message":"历史回放不存在。"}) from exc
@app.get("/api/v1/historical-replay/runs/{replay_id}/export.csv")
def export_historical_replay(replay_id:str,request:Request):
    try:content=current_historical_replay(request).export_csv(replay_id)
    except HistoricalReplayError as exc:
      raise HTTPException(404,detail={"code":str(exc),"message":"历史回放不存在。"}) from exc
    return Response(content=content,media_type="text/csv; charset=utf-8",
      headers={"Content-Disposition":f'attachment; filename="{replay_id}.csv"'})
@app.post("/api/v1/replay/run")
def run_replay(body:ReplayIn):
    rows=records_for(body.mode,body.environment_id)
    result=replay(body.strategy,rows)
    return {**result,**analytical_metadata(
      body.mode,body.environment_id,rows,
      ["离线重放不会调用模型 API。","缺少候选快照或实际字段时，相应比较值保持 null。"],
      source_type="offline_replay",configuration_version="decision-policy-v1.0.0"),
      "is_mock":body.mode=="demo",
      "input_sample_size":len(rows),
      "eligible_sample_size":sum(bool(row.get("channel_id")) for row in rows),
      "limitations":["离线重放不会调用模型 API。",
        "缺少候选快照或实际字段时，相应比较值保持 null。"],
      "catalog_version":None,"catalog_sha256":None,
      "policy_version":"decision-policy-v1.0.0",
      "created_at":datetime.now(timezone.utc).isoformat()}
@app.get("/api/v1/replay/{replay_id}")
def get_replay(replay_id:str,mode:str=Query("demo"),environment_id:str=Query("all")):
    return {**replay("confidence_aware_v2",records_for(mode,environment_id)),
      "replay_id":replay_id,"environment_id":environment_id,"source_type":"offline_replay"}
@app.get("/api/v1/errors")
def errors(request:Request,mode:str=Query("demo"),environment_id:str=Query("all")):
    """Return failures from the authoritative standardized call ledger.

    The operations UI must not fall back to the legacy demo collection when it
    is in UAT mode.  Only redacted identifiers and normalized error metadata are
    projected; request/response bodies and credentials never enter this view.
    """
    if mode == "demo":
        rows=records_for(mode,environment_id)
        return {**analytical_metadata(mode,environment_id,rows,
          ["演示模式仅用于开发回归，不属于真实 UAT 验收。"],
          configuration_version="error-taxonomy-v1"),
          "items":[{**classify(r.get("http_status"),"imported failure"),
            "error_id":"ERR-"+r.get("request_id","unknown"),
            "request_id":r.get("request_id","") or None,
            "channel_id":r.get("channel_id","") or None}
            for r in rows if r.get("result")=="failure"]}
    ledger=current_call_logs(request).list_records(
        limit=1000,environment_id=None if environment_id=="all" else environment_id)
    failed=[row for row in ledger["items"] if row.get("request_status") in {
        "FAILED","TIMEOUT","INTERRUPTED"}]
    actions={
      "timeout":"检查上游响应时间与超时设置后再决定是否有限重试。",
      "rate_limit":"等待限流窗口恢复后再执行有限重试。",
      "authentication":"检查当前 UAT API Key，禁止自动重试。",
      "cancelled":"用户已停止请求；保留已接收内容，不自动重试。",
      "protocol_error":"检查上游响应协议和流式兼容性。",
      "uat_transport_failed":"检查国内 UAT 网络或上游服务状态。",
    }
    items=[]
    for row in failed:
        category=str(row.get("error_category") or row.get("error_code") or "unknown_failure")
        status=row.get("http_status")
        layer=("historical_evidence" if row.get("is_historical") else
               "upstream" if status is not None else "transport")
        items.append({
          "error_id":f"ERR-{row['record_id']}","record_id":row["record_id"],
          "request_id":row.get("request_id"),"decision_id":row.get("decision_id"),
          "channel_id":row.get("channel_id"),"model":row.get("requested_model"),
          "error_category":category,"error_layer":layer,
          "request_status":row.get("request_status"),"http_status":status,
          "retryable":bool(row.get("retryable")),"fallback_allowed":False,
          "recommended_action":actions.get(category,"查看脱敏错误和关联执行记录后处理。"),
          "occurred_at":row.get("occurred_at"),"source_type":row.get("source_type"),
        })
    return {
      "mode":"uat","environment_id":environment_id,"source_type":"standardized_call_logs",
      "is_mock":False,"sample_size":len(items),"updated_at":ledger.get("updated_at"),
      "configuration_version":"error-taxonomy-v2","items":items,
      "limitations":["只展示标准化脱敏错误字段；不包含请求正文、响应正文或凭据。"],
    }
@app.get("/api/v1/errors/{error_id}")
def error(error_id:str): return {**classify(429,"rate limited"),"error_id":error_id}
@app.post("/api/v1/bugs/generate")
def bugs(body:BugIn): return bug_report(body.model_dump())
@app.get("/api/v1/budgets/summary")
def budgets(request:Request,mode:str=Query("demo"),environment_id:str=Query("all")):
    if mode=="uat" and environment_id!="overseas":
      target_environment="china_uat" if environment_id=="all" else environment_id
      unified=current_call_logs(request).cost_records(environment_id=target_environment)
      rows=[{
        "record_id":row.get("record_id"),"request_id":row.get("request_id"),
        "timestamp":row.get("occurred_at"),"environment_id":target_environment,
        "channel_id":row.get("channel_id"),"channel_name":row.get("channel_name"),
        "requested_model":row.get("requested_model"),"actual_model":row.get("actual_model"),
        "result":"success" if str(row.get("request_status") or "").upper()=="SUCCESS" else "failure",
        "http_status":row.get("http_status"),"latency_ms":row.get("total_latency_ms"),
        "input_tokens":row.get("input_tokens"),"cached_input_tokens":row.get("cached_input_tokens"),
        "output_tokens":row.get("output_tokens"),
        "total_tokens":sum(int(row.get(field) or 0) for field in
          ("input_tokens","cached_input_tokens","output_tokens")),
        "cost_cny":row.get("cost_amount") if row.get("currency") in (None,"CNY") else None,
        "source_type":row.get("source_type"),"price_version":row.get("price_version"),
        "cost_source":row.get("cost_source"),
      } for row in unified]
    else:
      rows=records_for(mode,environment_id)
    explicit_environment=environment_id if environment_id in {"demo","china_uat","overseas"} else None
    result={**budget(rows,environment_id=explicit_environment),"mode":mode,"environment_id":environment_id,
      "source_type":"demo_mock" if mode=="demo" else (rows[0].get("source_type") if rows else None),
      "sample_size":len(rows)}
    result["currency"]=result.get("summary",{}).get("currency")
    if environment_id=="overseas":
      result["currency"]=None
      result["summary"]["currency"]=None
      result["limitations"]=["费用币种待确认；未进行跨币种汇总或换算。"]
    meta=analytical_metadata(
      mode,environment_id,rows,result.get("limitations",[]),
      configuration_version=result.get("policy_version"),currency=result["currency"])
    result.update({key:meta[key] for key in (
      "is_mock","evidence_id","collection_ids","updated_at","configuration_version")})
    return result
@app.get("/api/v1/mappings")
def mapping(mode:str=Query("demo"),environment_id:str=Query("all")):
    rows=records_for(mode,environment_id)
    return {**analytical_metadata(mode,environment_id,rows,
      ["实际模型缺失时保持待确认，不从请求模型推断。"],
      configuration_version="model-mapping-v1"),
      "items":mappings([r for r in rows if r.get("channel_id") and r.get("requested_model")])}
@app.get("/api/v1/compatibility")
def compat(mode:str=Query("demo"),environment_id:str=Query("all")):
    rows=records_for(mode,environment_id)
    meta=analytical_metadata(mode,environment_id,rows,
      ["Only explicit compatibility evidence can produce passed or failed."],
      configuration_version="compatibility-evidence-v1")
    if mode=="uat" and not rows:return {**meta,"execution_mode":"definition_only","items":[]}
    return {**meta,"execution_mode":"evidence_only",
      "items":compatibility(rows)}
@app.get("/api/v1/config-review")
def review_current_configuration(request:Request,environment_id:str=Query("china_uat"),
                                 baseline_version:str|None=Query(None),
                                 comparison_version:str|None=Query(None)):
    if environment_id != "china_uat":
      raise HTTPException(400,detail={"code":"CONFIG_REVIEW_ENVIRONMENT_UNSUPPORTED",
        "message":"配置评审仅使用国内 UAT 真实数据。"})
    return current_config_review(request).review(environment_id,baseline_version,comparison_version)

@app.get("/api/v1/config-review/versions")
def config_review_versions(request:Request):
    return {"status":"ready","items":current_config_review(request).versions()}

@app.post("/api/v1/config-review/versions")
def create_config_review_version(body:dict[str,Any],request:Request):
    _secure_credential_request(request)
    principal=request_principal(request)
    try:
      return current_config_review(request).create_version(
        base_version=str(body.get("base_version") or ""),
        patch=body.get("patch") if isinstance(body.get("patch"),dict) else {},
        created_by=principal.principal_id,
        change_reason=str(body.get("change_reason") or ""))
    except ValueError as exc:
      raise HTTPException(409,detail={"code":str(exc),"message":"配置版本创建失败。"}) from exc

@app.post("/api/v1/config-review/versions/{target_version}/rollback")
def rollback_config_review_version(target_version:str,body:dict[str,Any],request:Request):
    _secure_credential_request(request)
    principal=request_principal(request)
    try:
      return current_config_review(request).rollback(
        target_version=target_version,active_version=str(body.get("active_version") or ""),
        created_by=principal.principal_id,change_reason=str(body.get("change_reason") or ""))
    except ValueError as exc:
      raise HTTPException(409,detail={"code":str(exc),"message":"配置回滚失败。"}) from exc

@app.post("/api/v1/config-review")
def review_current_configuration_compat(body:ConfigIn,request:Request):
    """Compatibility route; client-provided weights never replace persisted policy."""
    environment_id=body.environment_id if body.environment_id=="china_uat" else "china_uat"
    return current_config_review(request).review(environment_id)
@app.get("/api/v1/exports")
def exports(mode:str=Query("demo"),environment_id:str=Query("all")):
    rows=records_for(mode,environment_id)
    return {**analytical_metadata(mode,environment_id,rows,
      ["报告只导出当前筛选范围内的脱敏结构化证据。"],
      configuration_version="report-catalog-v1"),
      "items":[{"name":name,"format":fmt} for name,fmt in EXPORT_REPORT_NAMES.items()]}
@app.post("/api/v1/exports/generate")
def generate_export(body:ExportIn,request:Request):
    if body.name not in EXPORT_REPORT_NAMES:
      raise HTTPException(404,detail={"code":"UNKNOWN_REPORT","message":f"未知报告名称：{body.name}"})
    generated_at=datetime.now(timezone.utc).isoformat()
    fmt=EXPORT_REPORT_NAMES[body.name]
    if body.name=="渠道健康报告":
      data=channels(body.mode,body.environment_id)
      lines=[f"# {body.name}","",f"生成时间：{generated_at}",f"样本：{data['sample_size']}",f"数据来源：{data.get('source_type') or '待确认'}","",
        "| 渠道 | 样本 | 成功率 | P50 延迟 | P95 延迟 | 状态 |","|---|---:|---:|---:|---:|---|"]
      lines += [f"| {item.get('channel_name') or item.get('channel_id') or '待确认'} | {item.get('sample_size',0)} | {item.get('observed_success_rate','待确认')} | {item.get('p50_latency','待确认')} | {item.get('p95_latency','待确认')} | {item.get('health_state') or '待确认'} |" for item in data["items"]]
      if not data["items"]: lines.append("| 暂无可用记录 | 0 | 待确认 | 待确认 | 待确认 | 证据不足 |")
      content="\n".join(lines)
    elif body.name=="影子调度报告":
      data=shadow_summary(body.mode)
      lines=[f"# {body.name}","",f"生成时间：{generated_at}",f"样本：{data.get('sample_size',0)}",f"数据来源：{data.get('source_type') or '待确认'}","",
        "## 观察摘要","",f"- 决策数量：{data.get('decision_count',data.get('sample_size',0))}",f"- 平台实际渠道：仅在存在后台日志证据时展示",f"- 离线推荐：不代表平台实际路由","",
        "## 限制","",*(f"- {item}" for item in data.get("limitations",["当前证据不足以确认平台实际渠道。"]))]
      content="\n".join(lines)
    elif body.name=="错误报告":
      data=errors(request,body.mode,body.environment_id)
      lines=[f"# {body.name}","",f"生成时间：{generated_at}",f"样本：{data['sample_size']}",f"数据来源：{data.get('source_type') or '未知'}",""]
      lines+=[f"- {item['error_id']}：{item['error_category']}（{item['error_layer']}）· 请求 {item['request_id']} · 渠道 {item['channel_id'] or '未知'} · 建议：{item['recommended_action']}" for item in data["items"]]
      if not data["items"]: lines.append("当前范围没有错误记录。")
      content="\n".join(lines)
    else:
      rows=records_for(body.mode,body.environment_id)
      lines=[f"# {body.name}","",f"生成时间：{generated_at}","说明：清单仅列出脱敏证据引用，不包含密钥、鉴权头或完整提示词。","",
        "| 请求 ID | 证据引用 |","|---|---|"]
      lines += [f"| {r.get('request_id') or '待确认'} | {safe_ref(r.get('request_id') or 'unknown')} |" for r in rows]
      if not rows: lines.append("| 暂无记录 | 待确认 |")
      content="\n".join(lines)
    return {"name":body.name,"format":fmt,"generated_at":generated_at,
      "sha256":hashlib.sha256(content.encode("utf-8")).hexdigest(),"content":content}

@app.post("/api/v1/security/session/bootstrap")
def enterprise_session_bootstrap(request:Request,response:Response):
    """Create only the explicit loopback development bootstrap session."""
    try:
      token,validation,csrf_token=ENTERPRISE_HTTP.bootstrap_development(request)
    except EnterpriseHTTPError as exc:
      raise HTTPException(exc.status_code,detail={"code":exc.code}) from exc
    set_session_cookie(response,token,INGRESS_PROFILE)
    principal=validation.principal
    return {
      "status":"authenticated",
      "principal":{
        "principal_id":principal.principal_id,
        "principal_type":principal.principal_type.value,
        "tenant_id":principal.tenant_id,
        "workspace_id":principal.workspace_id,
        "roles":sorted(principal.roles),
        "authn_method":principal.authn_method,
        "expires_at":principal.expires_at.isoformat(),
        "is_development_identity":principal.is_development_identity,
      },
      "csrf_token":csrf_token,
      "enterprise_idp":"pending_external_configuration",
    }

@app.get("/api/v1/security/session")
def enterprise_session_status(request:Request):
    principal=request_principal(request)
    return {"status":"authenticated","principal":{
      "principal_id":principal.principal_id,
      "principal_type":principal.principal_type.value,
      "tenant_id":principal.tenant_id,
      "workspace_id":principal.workspace_id,
      "roles":sorted(principal.roles),
      "authn_method":principal.authn_method,
      "expires_at":principal.expires_at.isoformat(),
      "is_development_identity":principal.is_development_identity,
    }}

@app.get("/api/v1/security/session/csrf")
def enterprise_session_csrf(request:Request):
    validation=request.state.enterprise_session
    return {"csrf_token":ENTERPRISE_HTTP.csrf.issue(validation)}

@app.post("/api/v1/security/session/logout")
def enterprise_session_logout(request:Request,response:Response):
    validation=request.state.enterprise_session
    if PERSISTENT_CREDENTIALS is not None:
      try:
        PERSISTENT_CREDENTIALS.delete_principal(
          validation.principal.principal_id,validation.principal.tenant_id,
          validation.principal.workspace_id,
          tuple(load_platform_environments().environments.keys()))
      except CredentialVaultError as exc:
        raise HTTPException(503,detail={"code":"CREDENTIAL_DELETE_FAILED",
          "message":"Logout was stopped because encrypted credentials could not be cleared."}) from exc
    ENTERPRISE_HTTP.sessions.revoke_session(validation.session_id,"user_logout")
    clear_session_cookie(response,INGRESS_PROFILE)
    return {"status":"revoked"}

def _frontend_distribution()->Path:
    configured=os.getenv("ROUTING_CONSOLE_FRONTEND_DIST")
    return Path(configured).resolve() if configured else (ROOT/"web"/"dist").resolve()

@app.get("/runtime-config.json",include_in_schema=False)
def frontend_runtime_config():
    """Safe same-origin browser configuration; it never contains credentials."""
    return {"api_base_url":"","api_prefix":"/api","health_url":"/health",
      "ready_url":"/ready","deployment_mode":"local_operator",
      "credentials_exposed":False}

@app.get("/{frontend_path:path}",include_in_schema=False)
def frontend_application(frontend_path:str):
    """Serve the production SPA from the API process to remove proxy drift."""
    distribution=_frontend_distribution()
    index=distribution/"index.html"
    if not index.is_file():
      return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='3'><title>本地服务正在启动</title>"
        "<main><h1>本地服务正在启动</h1><p>前端构建产物尚未就绪，页面将自动重试。</p>"
        "<p>诊断编号：frontend_build_pending</p></main></html>",status_code=503,
        headers={"Cache-Control":"no-store"})
    requested=(distribution/frontend_path).resolve()
    try:
      requested.relative_to(distribution)
    except ValueError:
      raise HTTPException(404,detail={"code":"api_route_not_found"})
    if frontend_path and requested.is_file():
      return FileResponse(requested)
    return FileResponse(index,headers={"Cache-Control":"no-cache"})

# Route metadata is part of the default-deny contract and must be installed
# after every router/handler has been registered.
annotate_route_permissions(app)
app.add_middleware(
  EnterpriseHTTPAuthorizationMiddleware,runtime=ENTERPRISE_HTTP,route_app=app)
app.add_middleware(
  EnterpriseIngressMiddleware,profile=INGRESS_PROFILE,
  action_resolver=permission_for_scope,sensitive_actions=frozenset({
    "collector.session.reauthenticate","collector.oneshot.execute",
    "evidence.import","snapshot.generate","scheduler.execute","price.sync",
    "circuit.manage","exploration.manage","kill_switch.activate",
    "kill_switch.release","tenant.manage","service_identity.manage",
    "uat.execution.manage","uat.execute","uat.capability.review"}))
app.add_middleware(PrincipalPreResolutionMiddleware,runtime=ENTERPRISE_HTTP)
