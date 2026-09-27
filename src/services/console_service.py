"""Auditable, network-free services for Routing Quality Console."""
from __future__ import annotations
import csv, hashlib, json, math, re, sqlite3, sys, uuid
from decimal import Decimal, ROUND_HALF_UP
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"src")) if str(ROOT/"src") not in sys.path else None
from error_taxonomy import classify_runtime_error, load_taxonomy  # noqa: E402
from decision_logger import DecisionLogger  # noqa: E402

SENSITIVE=re.compile(r"(?i)(authorization\s*[:=]|bearer\s+[A-Za-z0-9._-]+|sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=]\s*\S+)")
DECISION_LOG_PATH=ROOT/"output"/"scheduler_decision_logs_v1.jsonl"
BUDGET_POLICY_PATH=ROOT/"config"/"budget_policy_v1.json"

TAXONOMY=load_taxonomy()
# Backward-compatible view: category -> (error_code, error_layer, retryable, fallback_allowed, recommended_action).
# Derived from TAXONOMY so the Console and the Scheduler fallback policy (src/scheduler.py) can never drift apart.
ERRORS={name:(meta["error_code"],meta["error_layer"],meta["retryable"],meta["fallback_allowed"],meta["recommended_action"])
  for name,meta in TAXONOMY["categories"].items()}

def redact(value:Any)->str:
    text=str(value or "")
    return SENSITIVE.sub("[REDACTED]",text)
def safe_ref(name:str)->str:
    return f"evidence://{hashlib.sha256(name.encode()).hexdigest()[:16]}"
def classify(status:int|None=None,message:str="",context:str="")->dict[str,Any]:
    """Deterministically map (status, message, context) to one of the 17 canonical
    runtime error categories in data/error_taxonomy_runtime_v1.json. Rule order
    mirrors each category's documented detection_rule; see that file for the
    authoritative description of every branch below."""
    result=classify_runtime_error(status=status,message=message,context=context,taxonomy=TAXONOMY)
    return {**result,"maximum_total_attempts":2,"evidence_source":"imported_log"}

class Store:
    def __init__(self,path:Path): self.path=path; self._init()
    def connect(self): self.path.parent.mkdir(parents=True,exist_ok=True); return sqlite3.connect(self.path)
    def _init(self):
      with self.connect() as db:
       db.executescript("""CREATE TABLE IF NOT EXISTS import_batches(batch_id TEXT PRIMARY KEY,sha256 TEXT,source_type TEXT,created_at TEXT,payload TEXT);
       CREATE TABLE IF NOT EXISTS audit_log(event_id TEXT PRIMARY KEY,event_type TEXT,created_at TEXT,details TEXT);""")
    def save_batch(self,batch:dict)->None:
      with self.connect() as db:
       # Use named legacy columns so the enterprise tenant migration can append
       # tenant/workspace scope columns without breaking this local-development
       # compatibility store. SQLite supplies the migration's audited defaults;
       # request-scoped enterprise services do not use this compatibility path.
       db.execute("""INSERT INTO import_batches(
         batch_id,sha256,source_type,created_at,payload) VALUES(?,?,?,?,?)""",
         (batch["batch_id"],batch["source_sha256"],batch["source_type"],
          batch["created_at"],json.dumps(batch,ensure_ascii=False)))
       db.execute("""INSERT INTO audit_log(
         event_id,event_type,created_at,details) VALUES(?,?,?,?)""",
         (str(uuid.uuid4()),"import_confirmed",batch["created_at"],
          json.dumps({"batch_id":batch["batch_id"]})))
    def batches(self): 
      with self.connect() as db: return [json.loads(r[0]) for r in db.execute("SELECT payload FROM import_batches ORDER BY created_at DESC")]
    def records(self) -> list[dict[str, Any]]:
      records: list[dict[str, Any]] = []
      for batch in reversed(self.batches()):
       for row in batch.get("normalized_rows", batch.get("normalized_preview", [])):
        if row.get("_classification") != "rejected":
         records.append({**row, "import_batch_id": batch["batch_id"], "source_type": batch["source_type"]})
      return records

def parse_content(filename:str,content:bytes)->list[dict]:
    if len(content)>5_000_000: raise ValueError("文件超过 5 MB 限制")
    suffix=Path(filename).suffix.lower()
    try: text=content.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise ValueError("unsupported_text_encoding: expected UTF-8 or UTF-8-SIG") from exc
    if suffix==".csv": return list(csv.DictReader(text.splitlines()))
    if suffix==".json":
      value=json.loads(text); return value if isinstance(value,list) else [value]
    if suffix in {".jsonl",".ndjson"}: return [json.loads(line) for line in text.splitlines() if line.strip()]
    raise ValueError("仅支持 CSV、JSON、JSON Lines")
def validate_rows(rows:list[dict],mapping:dict[str,str]|None=None)->dict:
    mapping=mapping or {}; normalized=[]; issues=[]; seen=set()
    for index,source in enumerate(rows,1):
      row={target:source.get(origin,"") for target,origin in mapping.items()} if mapping else dict(source)
      state="valid"; row_issues=[]
      def issue(code,severity="warning"):
       nonlocal state; row_issues.append(code); state="rejected" if severity=="rejected" else ("needs_confirmation" if severity=="confirm" and state!="rejected" else ("warning" if state=="valid" else state))
      for field in ("channel_id","request_id","requested_model"):
       if not row.get(field): issue(f"missing_{field}")
      if row.get("request_id") in seen: issue("duplicate_request_id","rejected")
      seen.add(row.get("request_id"))
      try:
       if row.get("http_status") and not 100<=int(row["http_status"])<=599: issue("invalid_http_status","rejected")
      except ValueError: issue("invalid_http_status","rejected")
      for field in ("input_tokens","output_tokens","total_tokens"):
       try:
        if row.get(field) not in ("",None) and int(row[field])<0: issue(f"negative_{field}","rejected")
       except ValueError: issue(f"invalid_{field}","rejected")
      if all(str(row.get(f,"")).isdigit() for f in ("input_tokens","output_tokens","total_tokens")) and int(row["input_tokens"])+int(row["output_tokens"])!=int(row["total_tokens"]): issue("total_tokens_inconsistent")
      if row.get("requested_model") and row.get("actual_model") and row["requested_model"]!=row["actual_model"]: issue("model_mismatch")
      if row.get("cost_cny") not in ("",None) and row.get("estimated_cost_cny") not in ("",None):
       try:
        observed, estimated = float(row["cost_cny"]), float(row["estimated_cost_cny"])
        if abs(observed-estimated) > max(.001, abs(estimated)*.1): issue("cost_mismatch")
       except ValueError: issue("invalid_cost","rejected")
      if row.get("timestamp"):
       try: datetime.fromisoformat(str(row["timestamp"]).replace("Z","+00:00"))
       except ValueError: issue("invalid_timestamp","rejected")
      if row.get("latency_ms") not in ("",None):
       try:
        latency=float(row["latency_ms"])
        if latency < 0: issue("impossible_latency","rejected")
        elif latency > 600_000: issue("latency_outlier")
       except ValueError: issue("invalid_latency","rejected")
      stream=str(row.get("stream","")).upper()=="TRUE"
      if stream and not row.get("ttft_ms"): issue("missing_stream_ttft")
      if not stream and row.get("ttft_ms"): issue("unexpected_nonstream_ttft")
      raw=json.dumps(source,ensure_ascii=False)
      if "\ufffd" in raw or re.search(r"(?:Ã.|Â.|鈥[滀淇]|锛[歿坆])",raw): issue("likely_mojibake")
      if SENSITIVE.search(raw): issue("sensitive_data","rejected")
      def formula_risk(value: Any) -> bool:
       if not isinstance(value,str) or not value:return False
       if value[0] in ("=","+","@"):return True
       return value[0]=="-" and not re.fullmatch(r"-\d+(?:\.\d+)?",value)
      if any(formula_risk(v) for v in source.values()): issue("csv_formula_injection","confirm")
      clean={k:redact(v) for k,v in row.items() if k.lower() not in {"prompt","messages","authorization","api_key"}}
      clean["_row_number"]=index; clean["_classification"]=state; clean["_issues"]=row_issues; normalized.append(clean)
      issues.extend({"row":index,"code":code,"severity":state} for code in row_issues)
    counts=Counter(r["_classification"] for r in normalized)
    return {"row_count":len(rows),"valid_count":counts["valid"],"warning_count":counts["warning"],
      "rejected_count":counts["rejected"],"needs_confirmation_count":counts["needs_confirmation"],
      "issues":issues,"detected_columns":sorted({key for row in rows for key in row}),
      "normalized_preview":normalized[:100],"normalized_rows":normalized}
def import_preview(filename:str,content:bytes,mapping=None,source_type="measured_uat"):
    rows=parse_content(filename,content); report=validate_rows(rows,mapping)
    digest=hashlib.sha256(content).hexdigest()
    return {"batch_id":f"IMP-{digest[:12].upper()}","import_batch_id":f"IMP-{digest[:12].upper()}",
      "source_sha256":digest,"source_type":source_type,
      "created_at":datetime.now(timezone.utc).isoformat(),"imported_at":datetime.now(timezone.utc).isoformat(),
      "imported_row_count":report["row_count"]-report["rejected_count"],
      "audit_status":"immutable_audited",
      "provenance":{"filename":Path(filename).name,"immutable":True},**report}

def uat_records(store: Store) -> list[dict[str, Any]]:
    """Convert accepted normalized import rows to analytics records without inventing evidence."""
    output = []
    for row in store.records():
      browser_collected = row.get("source_type") == "measured_uat_browser_collector"
      def number(name: str, default: float = 0) -> float:
        try: return float(row.get(name) or default)
        except (TypeError, ValueError): return default
      status = int(number("http_status", 0))
      result = row.get("result") or ("unknown" if browser_collected else ("success" if 200 <= status < 400 else "failure" if status else "unknown"))
      output.append({
        **row,
        "channel_id": str(row.get("channel_id") or ""),
        "channel_name": row.get("channel_name") if browser_collected else str(row.get("channel_name") or row.get("channel_id") or "unknown"),
        "requested_model": str(row.get("requested_model") or ""),
        "actual_model": row.get("actual_model") if browser_collected else str(row.get("actual_model") or ""),
        "request_id": str(row.get("request_id") or ""),
        "result": result,
        "http_status": (int(row["http_status"]) if browser_collected and row.get("http_status") not in (None,"") else (None if browser_collected else status)),
        "latency_ms": number("latency_ms", number("total_latency_ms", 0)),
        "cost_cny": number("cost_cny", 0),
        "timestamp": str(row.get("timestamp") or row.get("measured_at") or row.get("actual_executed_at") or ""),
        "profile": str(row.get("request_profile_id") or row.get("profile") or ""),
      })
    return output

def uat_health(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable = [row for row in records if row.get("channel_id")]
    cards = []
    for channel_id in sorted({row["channel_id"] for row in usable}):
      rows = [row for row in usable if row["channel_id"] == channel_id]
      sample = len(rows)
      successes = sum(row.get("result") == "success" for row in rows)
      latencies = sorted(float(row.get("latency_ms") or 0) for row in rows if float(row.get("latency_ms") or 0) >= 0)
      confidence = min(1.0, sample / 20)
      last = max((row.get("timestamp") or "" for row in rows), default="")
      state = "insufficient_data" if sample < 5 else "unknown"
      cards.append({
        "channel_id": channel_id, "channel_name": rows[0].get("channel_name") or channel_id,
        "requested_model": rows[0].get("requested_model", ""),
        "observed_actual_model": rows[0].get("actual_model", ""),
        "sample_size": sample, "success_count": successes, "failure_count": sample-successes,
        "observed_success_rate": successes/sample if sample else None,
        "p50_latency": median(latencies) if latencies else None,
        "p95_latency": latencies[max(0, math.ceil(.95*len(latencies))-1)] if latencies else None,
        "maximum_latency": max(latencies) if latencies else None,
        "average_cost": sum(float(row.get("cost_cny") or 0) for row in rows)/sample,
        "confidence": confidence, "health_score": None, "health_state": state,
        "freshness_status": "unknown" if not last else "observed",
        "last_observation": last, "source_type": rows[0].get("source_type", "measured_uat"),
        "evidence_limitations": "样本不足，不显示健康结论" if sample < 5 else "仅基于导入的观测证据",
      })
    return cards

def demo_records():
    now="2026-07-27T10:00:00+08:00"
    return [
     {"request_id":"D001","channel_id":"19","channel_name":"赤陶一号","requested_model":"deepseek-v4-flash","actual_model":"deepseek-v4-flash","result":"success","http_status":200,"latency_ms":620,"ttft_ms":"","cost_cny":.004,"timestamp":now,"source_type":"demo_mock","profile":"P01"},
     {"request_id":"D002","channel_id":"27","channel_name":"琥珀二号","requested_model":"deepseek-v4-flash","actual_model":"deepseek-v4-flash","result":"failure","http_status":429,"latency_ms":1300,"cost_cny":.006,"timestamp":now,"source_type":"demo_mock","profile":"P02"},
     {"request_id":"D003","channel_id":"45","channel_name":"玫瑰三号","requested_model":"deepseek-v4-flash","actual_model":"deepseek-v3","result":"failure","http_status":500,"latency_ms":9100,"cost_cny":.09,"timestamp":"2026-07-01T10:00:00+08:00","source_type":"demo_mock","profile":"P03"},
     {"request_id":"D004","channel_id":"48","channel_name":"青绿四号","requested_model":"deepseek-v4-flash","actual_model":"","result":"failure","http_status":504,"latency_ms":12000,"stream":"TRUE","ttft_ms":"","cost_cny":.012,"timestamp":now,"source_type":"demo_mock","profile":"P04"},
     {"request_id":"D005","channel_id":"50","channel_name":"梅紫五号","requested_model":"deepseek-v4-flash","actual_model":"deepseek-v4-flash","result":"success","http_status":200,"latency_ms":880,"cost_cny":.003,"timestamp":now,"source_type":"demo_mock","profile":"P01"}]
def health(records):
    out=[]
    for channel,rows in sorted(defaultdict(list,{k:list(v) for k,v in __import__("itertools").groupby(sorted(records,key=lambda r:r["channel_id"]),lambda r:r["channel_id"])}).items()):
      lat=sorted(float(r.get("latency_ms",0)) for r in rows); n=len(rows); success=sum(r["result"]=="success" for r in rows)
      age=0 if "2026-07-27" in max(r["timestamp"] for r in rows) else 26
      confidence=min(1,n/20); score=(success/n)*.6+max(0,1-median(lat)/10000)*.25+confidence*.15
      state="stale" if age>7 else ("insufficient_data" if n<2 else ("healthy" if score>=.8 else "degraded" if score>=.5 else "unhealthy"))
      out.append({"channel_id":channel,"channel_name":rows[0]["channel_name"],"requested_model":rows[0]["requested_model"],
       "observed_actual_model":rows[0].get("actual_model",""),"sample_size":n,"success_count":success,"failure_count":n-success,
       "observed_success_rate":success/n,"p50_latency":median(lat),"p95_latency":lat[max(0,math.ceil(.95*n)-1)],"maximum_latency":max(lat),
       "average_ttft":None,"average_cost":sum(float(r.get("cost_cny",0)) for r in rows)/n,
       "count_400":sum(r.get("http_status")==400 for r in rows),"count_401":sum(r.get("http_status")==401 for r in rows),
       "count_429":sum(r.get("http_status")==429 for r in rows),"count_5xx":sum(int(r.get("http_status",0))>=500 for r in rows),
       "timeout_count":sum(r.get("http_status")==504 for r in rows),"incomplete_sse_count":sum(r.get("stream")=="TRUE" and not r.get("ttft_ms") for r in rows),
       "last_observation":max(r["timestamp"] for r in rows),"data_age_days":age,"freshness_status":"stale" if age>7 else "fresh",
       "confidence":confidence,"health_score":round(score,3),"health_state":state,"source_type":rows[0]["source_type"],
       "evidence_limitations":"小样本，仅作演示" if n<20 else ""})
    return out
def replay(strategy="fastest_first",records=None):
    """Replay observed rows deterministically; unknown outcomes stay unknown."""
    rows=list(demo_records() if records is None else records)
    grouped=defaultdict(list)
    for row in rows:
      candidate=str(row.get("channel_id") or "").strip()
      if candidate: grouped[candidate].append(row)
    candidates=[]
    for candidate,items in sorted(grouped.items()):
      latencies=[_decimal(x["latency_ms"]) for x in items if x.get("latency_ms") not in ("",None)]
      costs=[_decimal(x["cost_cny"]) for x in items if x.get("cost_cny") not in ("",None)]
      outcomes=[x["result"] for x in items if x.get("result") in {"success","failure"}]
      candidates.append({"candidate_id":candidate,"sample_size":len(items),
        "observed_success_rate":float(Decimal(sum(x=="success" for x in outcomes))/len(outcomes)) if outcomes else None,
        "observed_average_latency_ms":float(sum(latencies,Decimal("0"))/len(latencies)) if latencies else None,
        "observed_average_cost":_money(sum(costs,Decimal("0"))/len(costs),"0.000001") if costs else None,
        "source_types":sorted({str(x.get("source_type") or "unknown") for x in items}),
        "evidence_request_ids":sorted(str(x["request_id"]) for x in items if x.get("request_id"))})
    keys={
      "fastest_first":lambda x:(x["observed_average_latency_ms"] is None,x["observed_average_latency_ms"] or 0,x["candidate_id"]),
      "cheapest_first":lambda x:(x["observed_average_cost"] is None,Decimal(x["observed_average_cost"] or "0"),x["candidate_id"]),
      "reliability_first":lambda x:(x["observed_success_rate"] is None,-(x["observed_success_rate"] or 0),x["candidate_id"]),
      "fixed_channel":lambda x:(x["candidate_id"],)}
    rank_key=keys.get(strategy,lambda x:(x["observed_success_rate"] is None,-(x["observed_success_rate"] or 0),
      x["observed_average_latency_ms"] is None,x["observed_average_latency_ms"] or 0,x["candidate_id"]))
    ranked=sorted(candidates,key=rank_key)
    selected=ranked[0] if ranked else None
    original=[str(x.get("channel_id")) for x in rows if x.get("channel_id")]
    changed=sum(x!=selected["candidate_id"] for x in original) if selected else 0
    evidence_shape=[{"request_id":x.get("request_id"),"channel_id":x.get("channel_id"),
      "result":x.get("result"),"latency_ms":x.get("latency_ms"),"cost_cny":x.get("cost_cny"),
      "source_type":x.get("source_type")} for x in rows]
    digest=hashlib.sha256(json.dumps({"strategy":strategy,"rows":evidence_shape},
      sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {"replay_id":f"RPL-{digest[:12].upper()}","strategy":strategy,
      "execution_mode":"offline_estimate","evidence_mode":"observed_rows",
      "formula_version":"observed-routing-replay-v1",
      "selected_candidate":selected["candidate_id"] if selected else None,
      "changed_decision_rate":changed/len(original) if original else None,
      "cost_estimate_cny":selected["observed_average_cost"] if selected else None,
      "latency_estimate_ms":selected["observed_average_latency_ms"] if selected else None,
      "sla_risk_estimate":None,"concentration_risk":None,
      "unroutable_count":sum(not x.get("channel_id") for x in rows),
      "low_confidence_count":sum(x["sample_size"]<2 for x in candidates),
      "stale_data_count":None,"candidate_metrics":candidates,
      "provenance":{"evidence_sha256":digest,"sample_size":len(rows),
        "source_types":sorted({str(x.get("source_type") or "unknown") for x in rows})},
      "network_calls":0}
def _decimal(value) -> Decimal:
    if value in ("", None): return Decimal("0")
    try: return Decimal(str(value))
    except Exception: return Decimal("0")

def _money(value: Decimal, places="0.0000") -> str:
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")

def _load_budget_policy(path=BUDGET_POLICY_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def _record_environment(row):
    if row.get("environment_id"): return str(row["environment_id"])
    return "demo" if row.get("source_type")=="demo_mock" else "china_uat"

def budget(records,policy_path=BUDGET_POLICY_PATH,environment_id=None):
    """Presentation-ready Decimal contract, plus backward-compatible fields."""
    policy=_load_budget_policy(policy_path)
    environments=sorted({_record_environment(row) for row in records}) or [environment_id or "demo"]
    if len(environments)>1:
      return {"contract_version":"cost-budget-v3","policy_version":policy["policy_version"],
        "formula_version":policy["formula_version"],"grouped_by_environment":True,
        "currency_aggregation":"blocked_mixed_environment",
        "groups":[{"environment_id":env,**budget(
          [row for row in records if _record_environment(row)==env],policy_path)}
          for env in environments],"sample_size":len(records),"external_platform_modified":False}
    environment=environments[0]
    env_policy=policy["environments"].get(environment,{
      "currency":None,"daily_limit":None,"limit_source":"unconfigured"})
    currency=env_policy.get("currency")
    limit=Decimal(str(env_policy["daily_limit"])) if env_policy.get("daily_limit") is not None else None
    actual_rows=[row for row in records if row.get("cost_cny") not in ("",None)]
    spend=sum((_decimal(r.get("cost_cny")) for r in actual_rows),Decimal("0"))
    remaining=max(Decimal("0"),limit-spend) if limit is not None else None
    usage=(spend/limit*100) if limit else None
    warning=Decimal(str(policy["warning_usage_percentage"]))
    state="unlimited" if limit is None else "blocked" if spend>=limit else "near_limit" if usage>=warning else "budget_ok"
    source=str(records[0].get("source_type") or "unknown") if records else "unknown"
    channels,results,dates=defaultdict(list),defaultdict(list),defaultdict(list)
    for row in records:
      channels[(str(row.get("channel_id") or ""),str(row.get("channel_name") or "渠道待确认"))].append(row)
      results[str(row.get("result") or "unknown")].append(row)
      dates[str(row.get("timestamp") or "")[:10] or "unknown"].append(row)
    by_channel=[]
    for (channel_id,name),rows in channels.items():
      total=sum((_decimal(x.get("cost_cny")) for x in rows),Decimal("0"))
      by_channel.append({"channel_id":channel_id or None,"channel_name":name,"total_cost":_money(total),
        "request_count":len(rows),"average_cost":_money(total/len(rows),"0.000001")})
    by_channel.sort(key=lambda x:Decimal(x["total_cost"]),reverse=True)
    by_result=[{"result":name,"total_cost":_money(sum((_decimal(x.get("cost_cny")) for x in rows),Decimal("0"))),
      "request_count":len(rows)} for name,rows in sorted(results.items())]
    trend=[{"date":day,"actual_cost":_money(sum((_decimal(x.get("cost_cny")) for x in rows),Decimal("0"))),
      "estimated_cost":None,"budget_limit":_money(limit) if limit is not None else None,
      "request_count":len(rows),"source_type":source}
      for day,rows in sorted(dates.items())]
    requests=[]
    for r in sorted(records,key=lambda x:_decimal(x.get("cost_cny")),reverse=True):
      actual=None if r.get("cost_cny") in ("",None) else _money(_decimal(r.get("cost_cny")),"0.000001")
      estimated=None if r.get("estimated_cost_cny") in ("",None) else _money(_decimal(r.get("estimated_cost_cny")),"0.000001")
      requests.append({"request_id":r.get("request_id") or None,"timestamp":r.get("timestamp") or None,
        "channel_id":r.get("channel_id") or None,"channel_name":r.get("channel_name") or None,
        "requested_model":r.get("requested_model") or None,"actual_model":r.get("actual_model") or None,
        "profile":r.get("profile") or None,"input_tokens":r.get("input_tokens"),"output_tokens":r.get("output_tokens"),
        "total_tokens":r.get("total_tokens"),"latency_ms":r.get("latency_ms"),"http_status":r.get("http_status"),
        "result":r.get("result") or "unknown","actual_cost":actual,"estimated_cost":estimated,
        "cost_difference":_money(_decimal(actual)-_decimal(estimated),"0.000001") if actual and estimated else None,
        "source_type":r.get("source_type") or source})
    anomalies=[]
    if usage is not None and usage>=warning:
      anomalies.append({"severity":"warning" if spend<limit else "critical","type":"budget_near_limit" if spend<limit else "budget_blocked",
        "request_id":None,"evidence":f"{_money(usage,'0.1')}%","reason":"预算使用率已达到阈值。",
        "recommended_action":"核对当日请求与后端计费证据。","limitation":"80% 为现有界面预警阈值。"})
    if spend and requests:
      share=Decimal(requests[0]["actual_cost"] or "0")/spend*100
      if share>=Decimal("50"):
        anomalies.append({"severity":"high","type":"cost_concentration","request_id":requests[0]["request_id"],
          "evidence":f"{_money(share,'0.1')}%","reason":"单个请求占当前样本总费用过高。",
          "recommended_action":"核对 token、重试与计费日志。","limitation":"仅基于当前选定样本。"})
    missing=sum(r.get("cost_cny") in ("",None) for r in records)
    if missing: anomalies.append({"severity":"info","type":"missing_cost_evidence","request_id":None,"evidence":str(missing),
      "reason":"部分请求缺少费用证据。","recommended_action":"导入脱敏计费日志。","limitation":"不会推测缺失费用。"})
    return {
      "contract_version":"cost-budget-v3","policy_version":policy["policy_version"],
      "formula_version":policy["formula_version"],"environment_id":environment,
      "mode":"demo" if source=="demo_mock" else "uat","source_type":source,"sample_size":len(records),
      "provenance":{"source_type":source,"label":"演示数据" if source=="demo_mock" else "真实 UAT 证据",
        "is_genuine":source in {"measured_uat","measured_unified_uat"},"sample_size":len(records),
        "actual_cost_sample_size":len(actual_rows),
        "updated_at":max((str(r.get("timestamp") or "") for r in records),default=None)},
      "summary":{"status":state,"currency":currency,"total_spend":_money(spend),
        "budget_limit":_money(limit) if limit is not None else None,
        "remaining_budget":_money(remaining) if remaining is not None else None,
        "usage_percentage":_money(usage,"0.1") if usage is not None else None,
        "limit_source":env_policy.get("limit_source"),"request_count":len(records),"maximum_total_attempts":2},
      "trend":trend,"by_channel":by_channel,"by_result":by_result,"highest_cost_requests":requests,
      "anomalies":anomalies,"limitations":["演示数据不代表真实平台费用。"] if source=="demo_mock" else ["结论仅适用于已导入样本。"],
      "status":state,"today_uat_spend":float(spend),
      "daily_limit":float(limit) if limit is not None else None,
      "remaining_budget":float(remaining) if remaining is not None else None,
      "request_count":len(records),"maximum_total_attempts":2,"blocked_request_reasons":[],
      "external_platform_modified":False}
def mappings(records):
    groups=defaultdict(list)
    for r in records: groups[(r["channel_id"],r["requested_model"],r.get("actual_model",""))].append(r)
    return [{"channel_id":k[0],"requested_model":k[1],"actual_model":k[2],
      "mapping_status":"actual_model_missing" if not k[2] else "exact_match" if k[1]==k[2] else "unexpected_model",
      "observed_count":len(v),"mismatch_count":sum(bool(x.get("actual_model")) and x["actual_model"]!=x["requested_model"] for x in v),
      "missing_actual_model_count":sum(not x.get("actual_model") for x in v),
      "evidence_source":Counter(x.get("source_type") for x in v if x.get("source_type")).most_common(1)[0][0] if any(x.get("source_type") for x in v) else "demo_mock",
      "purity_score":sum(x.get("actual_model")==x["requested_model"] for x in v)/len(v)} for k,v in groups.items()]
_COMPATIBILITY_CAPABILITIES=(
  "non_streaming_text","streaming_sse","structured_output","function_calling_tools",
  "prompt_cache","long_context","timeout_behavior","usage_fields","finish_reason",
  "request_id","json_error_response","incomplete_sse")

def compatibility(records=None):
    """Summarize only explicit compatibility evidence; HTTP success is not proof."""
    rows=list(records or [])
    output=[]
    for name in _COMPATIBILITY_CAPABILITIES:
      evidence=[]
      for row in rows:
        results=row.get("compatibility_results")
        if isinstance(results,dict) and results.get(name) in {"passed","failed","supported","unsupported"}:
          evidence.append((row,results[name]))
      statuses={status for _,status in evidence}
      status=("failed" if statuses & {"failed","unsupported"} else
        "passed" if statuses and statuses <= {"passed","supported"} else "not_tested")
      output.append({"capability":name,"status":status,"evidence_count":len(evidence),
        "last_tested":max((str(row.get("timestamp") or "") for row,_ in evidence),default=None),
        "source_type":Counter(str(row.get("source_type") or "unknown") for row,_ in evidence).most_common(1)[0][0] if evidence else None,
        "evidence_request_ids":sorted(str(row["request_id"]) for row,_ in evidence if row.get("request_id")),
        "notes":"HTTP 200 alone does not establish compatibility."})
    return output

def config_review(candidate):
    """Review and replay a candidate configuration without applying it."""
    issues=[]
    weights=candidate.get("weights",{})
    if any(float(value)<0 for value in weights.values()):
      issues.append({"severity":"block","rule":"negative_weights"})
    if weights and abs(sum(float(value) for value in weights.values())-1)>.001:
      issues.append({"severity":"block","rule":"weights_not_sum_one"})
    if not candidate.get("fallback"):
      issues.append({"severity":"warn","rule":"missing_fallback"})
    if not candidate.get("timeout_ms"):
      issues.append({"severity":"block","rule":"timeout_missing"})
    evidence=candidate.get("evidence_records") if isinstance(candidate.get("evidence_records"),list) else []
    baseline=replay(str(candidate.get("baseline_strategy") or "health_aware_scheduler"),evidence)
    proposed=replay(str(candidate.get("strategy") or candidate.get("baseline_strategy") or "health_aware_scheduler"),evidence)
    impact={"evidence_status":"computed" if evidence else "insufficient_evidence",
      "sample_size":len(evidence),"baseline_selected_candidate":baseline["selected_candidate"],
      "proposed_selected_candidate":proposed["selected_candidate"],
      "changed_selected_candidate":baseline["selected_candidate"]!=proposed["selected_candidate"] if evidence else None,
      "estimated_cost_difference":_money(
        _decimal(proposed["cost_estimate_cny"])-_decimal(baseline["cost_estimate_cny"]),"0.000001")
        if proposed["cost_estimate_cny"] is not None and baseline["cost_estimate_cny"] is not None else None,
      "estimated_latency_difference":proposed["latency_estimate_ms"]-baseline["latency_estimate_ms"]
        if proposed["latency_estimate_ms"] is not None and baseline["latency_estimate_ms"] is not None else None,
      "formula_version":baseline["formula_version"]}
    return {"review_id":"CR-"+hashlib.sha256(
      json.dumps(candidate,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:10].upper(),
      "status":"block" if any(item["severity"]=="block" for item in issues) else "warn" if issues else "pass",
      "issues":issues,"replay_impact":impact,
      "rollback_checklist":["retain current configuration snapshot","name rollback owner","verify model and stream capability"],
      "approval_recommendation":"do not approve while blocking issues remain",
      "external_configuration_changed":False}

def fallback_evidence(decision_id:str)->dict[str,Any]:
    """Look up a Scheduler runtime decision by decision_id and return its real
    fallback_trace, if any. Never fabricates a trace: a missing or unlinked
    decision_id yields an empty trace with an honest status, not a guess."""
    if not decision_id:
        return {"fallback_trace":[],"fallback_evidence_status":"no_decision_id_provided"}
    decision=DecisionLogger(DECISION_LOG_PATH).find_by_decision_id(decision_id)
    if decision is None:
        return {"fallback_trace":[],"fallback_evidence_status":"decision_id_not_found_in_runtime_log"}
    trace=list(decision.get("fallback_trace") or [])
    status="linked_to_scheduler_decision" if trace else "decision_found_no_fallback_executed"
    return {"fallback_trace":trace,"fallback_evidence_status":status}
def bug_report(error):
    bug_id="BUG-"+hashlib.sha256(json.dumps(error,sort_keys=True).encode()).hexdigest()[:10].upper()
    c=classify(error.get("http_status"),error.get("message",""),error.get("context",""))
    evidence=fallback_evidence(error.get("decision_id",""))
    data={"bug_id":bug_id,"title":f"{c['error_category']}：请求失败","environment":"Demo","detected_time":"2026-07-27T10:00:00+08:00",
      "request_id":error.get("request_id",""),"decision_id":error.get("decision_id",""),"model":error.get("model",""),
      "channel":error.get("channel",""),"request_type":error.get("request_type",""),"http_status":error.get("http_status"),
      "sanitized_original_error":c["sanitized_message"],"structured_classification":c,"reproduction_steps":["使用相同脱敏参数","在 Demo Mode 重放"],
      "expected_behavior":"返回兼容响应","actual_behavior":"发生结构化错误","retryable":c["retryable"],"fallback_allowed":c["fallback_allowed"],
      "fallback_trace":evidence["fallback_trace"],"fallback_evidence_status":evidence["fallback_evidence_status"],
      "evidence_paths":[safe_ref(error.get("request_id","unknown"))],"impact":"单请求失败","frequency":"待确认",
      "suggested_owner":"渠道运维" if c["error_layer"]=="upstream" else "后端平台","suggested_next_action":c["recommended_action"],
      "limitations":"未执行外部请求"}
    data["markdown"]="# "+data["title"]+"\n\n"+f"- Bug ID: {bug_id}\n- Request ID: {data['request_id']}\n- 分类: {c['error_category']}\n- 建议: {c['recommended_action']}\n"
    if evidence["fallback_trace"]:
        data["markdown"]+=f"- Fallback 轨迹: {' → '.join(evidence['fallback_trace'])}\n"
    return data
