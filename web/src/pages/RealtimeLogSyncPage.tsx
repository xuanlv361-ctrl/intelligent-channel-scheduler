import {useEffect,useRef,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {api,ApiError,EnvironmentFilter,EnvironmentId} from "../services/api";
import {MetricCard,TechnicalDataDrawer} from "../components/operations/OperationsUI";
import {formatTimestamp} from "../lib/presentation";
import {
 browserTimezone,calculateQuickRange,LogSyncQuickRange,rangeAsUtc,validateLogSyncRange,
} from "../lib/logSyncRange";

const active=new Set(["browser_starting","waiting_for_manual_login","waiting_for_operator_confirmation","waiting_for_operator","authentication_in_progress","syncing"]);
const labels:Record<string,string>={
 idle:"等待启动",browser_starting:"正在打开安全浏览器",
 waiting_for_operator:"等待操作员在正式 Chrome 中完成登录",
 authentication_in_progress:"正在检查正式 Chrome 登录状态",
 waiting_for_manual_login:"请在新窗口中手动登录",
 checking_authentication_storage:"正在检查认证存储",
 waiting_for_confirmation:"等待确认加密保存",
 confirmation_requested:"正在加密保存",
 authentication_validation_failed:"登录验证未通过，请在配对窗口修正后重试",
 validating_authentication:"正在验证持久会话",
 synchronizing:"无界面同步运行中",
 stopping:"正在停止",
 waiting_for_operator_confirmation:"等待操作员确认登录完成",
 syncing:"正在同步平台日志",stopped:"已停止",completed:"已完成",
 blocked:"已阻止",failed:"同步失败",session_expired:"登录会话已失效",
};
const reconciliationLabels:Record<string,string>={
 awaiting_log:"等待平台日志",matched:"已匹配",cost_confirmed:"实际费用已确认",
 cost_mismatch:"费用差异待复核",actual_cost_unavailable:"平台费用不可用",
 ambiguous:"关联存在歧义",
};
function message(error:unknown){
 return error instanceof ApiError?(error.detail.message||error.detail.code||"同步操作失败"):"同步操作失败";
}
function money(value:number|null,currency:string|null){
 return value===null?"待平台日志确认":`${currency||"币种待确认"} ${value.toFixed(6)}`;
}

const pairingStates=new Set([
 "browser_starting","waiting_for_manual_login","waiting_for_confirmation",
 "waiting_for_operator","authentication_in_progress",
 "checking_authentication_storage","confirmation_requested",
 "authentication_validation_failed",
]);

function DomesticAccessRecoveryPanel(){
 const qc=useQueryClient();
 const [ttl,setTtl]=useState(8);
 const [showConfig,setShowConfig]=useState(false);
 const [configConfirmed,setConfigConfirmed]=useState(false);
 const [notice,setNotice]=useState("");
 const sessionStatus=useQuery({queryKey:["persistent-session-status"],
  queryFn:({signal})=>api.persistentStatus(signal),refetchInterval:2000});
 const logPage=useQuery({queryKey:["domestic-log-page-status"],
  queryFn:({signal})=>api.domesticLogPageStatus(signal)});
 const session=sessionStatus.data?.session;
 const pairing=sessionStatus.data?.pairing;
 const browser=sessionStatus.data?.browser;
 const pairingActive=Boolean(pairing&&pairingStates.has(pairing.state));
 const authenticationValid=Boolean(
  session&&session.state==="active"&&session.authentication_status==="verified"&&
  session.revocation_status==="not_revoked");
 const reauthenticationRequired=!authenticationValid;
 const diagnostic=useQuery({
  queryKey:["persistent-storage-diagnostic",pairing?.pairing_id],
  queryFn:({signal})=>api.persistentDiagnostic(pairing!.pairing_id,signal),
  enabled:Boolean(pairing?.pairing_id&&pairingActive),
  refetchInterval:pairing?.storage_diagnostic_status==="ready"?false:2000,
 });
 const refresh=()=>{
  void qc.invalidateQueries({queryKey:["persistent-session-status"]});
  void qc.invalidateQueries({queryKey:["collector-status","china_uat"]});
 };
 const reauthenticate=useMutation({
  mutationFn:()=>api.startPersistentReauthentication(ttl),
  onSuccess:()=>{setNotice("已打开项目专用的正式 Google Chrome。窗口会持续保留，请手动完成登录、CAPTCHA 或 MFA。");refresh()},
  onError:error=>setNotice(message(error)),
 });
 const openChrome=useMutation({
  mutationFn:()=>api.openOrFocusPersistentChrome(),
  onSuccess:result=>{setNotice(result.action==="focused_existing"?"已聚焦现有国内 UAT Chrome 窗口。":"已打开国内 UAT Chrome 窗口。");refresh()},
  onError:error=>setNotice(message(error)),
 });
 const confirmPair=useMutation({
  mutationFn:()=>api.checkPersistentAuthentication(pairing!.pairing_id),
  onSuccess:()=>{setNotice("认证已验证并安全保存；正式 Chrome 仍保持打开。");refresh()},
  onError:error=>setNotice(message(error)),
 });
 const closeChrome=useMutation({
  mutationFn:()=>api.closePersistentChrome(),
  onSuccess:()=>{setNotice("已按你的明确操作关闭国内 UAT Chrome。");refresh()},
  onError:error=>setNotice(message(error)),
 });
 const configure=useMutation({
  mutationFn:async()=>{
   const preview=await api.previewDomesticLogPage();
   return api.confirmDomesticLogPage(preview.validation_id,preview.value_sha256);
  },
  onSuccess:()=>{setNotice("国内 UAT 日志页已明确确认。此操作未访问 UAT，也未启动同步。");setShowConfig(false);setConfigConfirmed(false);void logPage.refetch();refresh()},
  onError:error=>setNotice(message(error)),
 });
 const canConfirmPair=Boolean(pairing&&browser?.running&&[
  "waiting_for_operator","waiting_for_confirmation","authentication_validation_failed",
 ].includes(pairing.state));
 return <section className="ops-section access-recovery" aria-labelledby="domestic-access-title">
  <header><div><h2 id="domestic-access-title">国内 UAT 访问恢复</h2><p>日志页确认、登录认证和同步执行是三个独立步骤；前两步都不会读取账单日志。</p></div></header>
  {notice&&<div className="notice" role="status">{notice}</div>}
  <div className="prerequisite-grid">
   <div className={logPage.data?.logs_page_url?"state success":"state pending-state"}>
    <b>{logPage.data?.logs_page_url?"日志页已确认":"批准日志页 = 尚未确认"}</b>
    <span>{logPage.data?.configuration_reason||"已使用版本化本机运行时设置。"}</span>
    <button type="button" className="secondary" onClick={()=>setShowConfig(value=>!value)}>
     {logPage.data?.logs_page_url?"查看/重新确认日志页":"配置日志页"}
    </button>
   </div>
   <div className={authenticationValid?"state success":"state error"}>
    <b>{authenticationValid?"国内 UAT 登录有效":"国内 UAT 登录不可用"}</b>
    <span>{session?`状态：${session.state} / ${session.authentication_status}`:"未找到有效的本机加密登录状态。"}</span>
    {reauthenticationRequired
     ?<><label>登录状态有效期（小时）<input aria-label="重新认证有效期" type="number" min="1" max="24" value={ttl} onChange={event=>setTtl(Number(event.target.value))}/></label>
      <button type="button" onClick={()=>reauthenticate.mutate()} disabled={pairingActive||reauthenticate.isPending}>重新登录国内 UAT</button></>
     :<span className="status-chip">已认证 / 可用</span>}
    <div className="action-row">
     <button type="button" className="secondary" onClick={()=>openChrome.mutate()} disabled={openChrome.isPending}>{browser?.running?"聚焦UAT登录窗口":"打开UAT登录窗口"}</button>
     {browser?.running&&<button type="button" className="secondary" onClick={()=>closeChrome.mutate()} disabled={closeChrome.isPending}>关闭UAT浏览器</button>}
    </div>
    <span>浏览器：{browser?.browser_family||"Google Chrome"} · {labels[browser?.state||""]||browser?.state||"browser_not_running"}</span>
   </div>
  </div>
  {showConfig&&<fieldset className="range-panel">
   <legend>确认经过评审的只读日志页</legend>
   <label>日志页 URL<input aria-label="国内 UAT 日志页 URL" readOnly value={logPage.data?.reviewed_url||"https://uat.weimeta.cn/console/billing/logs"}/></label>
   <dl className="environment-config-grid">
    <dt>允许来源</dt><dd><code>{logPage.data?.reviewed_origin||"https://uat.weimeta.cn"}</code></dd>
    <dt>允许路径</dt><dd><code>{logPage.data?.reviewed_path||"/console/billing/logs"}</code></dd>
    <dt>设置版本</dt><dd>{logPage.data?.setting_version||"environment_log_page_confirmation_v1"}</dd>
   </dl>
   <label className="confirmation"><input aria-label="明确确认国内 UAT 日志页" type="checkbox" checked={configConfirmed} onChange={event=>setConfigConfirmed(event.target.checked)}/>我明确确认仅使用上述国内 UAT 只读账单日志页。</label>
   <button type="button" onClick={()=>configure.mutate()} disabled={!configConfirmed||configure.isPending}>确认日志页</button>
  </fieldset>}
  {pairingActive&&<div className="login-gate" role="status">
   <b>配对正在进行</b>
   <p>只在项目专用的正式 Google Chrome 窗口中输入登录信息。该窗口不会因页面刷新、API 返回或后端重启而自动关闭；本页面不会收集密码、Cookie、Authorization、API Key、浏览器存储或 DPAPI 材料。</p>
   <p>当前阶段：{labels[pairing!.state]||pairing!.state}</p>
   {diagnostic.data?.diagnostic&&<div className="storage-diagnostic">
    <h3>认证存储诊断（仅元数据）</h3>
    <dl className="environment-config-grid">
     <dt>认证来源</dt><dd>{diagnostic.data.diagnostic.authentication_origins.join("、")||"未检测到"}</dd>
     <dt>localStorage 键名</dt><dd>{diagnostic.data.diagnostic.local_storage_keys.join("、")||"无"}</dd>
     <dt>sessionStorage 键名</dt><dd>{diagnostic.data.diagnostic.session_storage_keys.join("、")||"无"}</dd>
     <dt>IndexedDB 名称</dt><dd>{diagnostic.data.diagnostic.indexed_db_names.join("、")||"无"}</dd>
    </dl>
   </div>}
   <button type="button" onClick={()=>confirmPair.mutate()} disabled={!canConfirmPair||confirmPair.isPending}>重新检查认证状态</button>
  </div>}
  {pairing?.failure_summary&&!pairingActive&&<div className="state error" role="alert"><b>重新认证未完成</b><span>{pairing.failure_summary}</span></div>}
 </section>;
}

function PersistentModePanel({environment,dateFrom,dateTo,timezoneName,interval,maximum,rangeError}:{
 environment:EnvironmentId;dateFrom:string;dateTo:string;timezoneName:string;
 interval:number;maximum:number;rangeError:string|null;
}){
 const qc=useQueryClient();
 const [periodicPolling,setPeriodicPolling]=useState(false);
 const [maximumHttpReads,setMaximumHttpReads]=useState(20);
 const [confirmed,setConfirmed]=useState(false);
 const [revokeText,setRevokeText]=useState("");
 const [notice,setNotice]=useState("");
 const status=useQuery({queryKey:["persistent-session-status"],
  queryFn:({signal})=>api.persistentStatus(signal),refetchInterval:2000});
 const logPage=useQuery({queryKey:["domestic-log-page-status"],
  queryFn:({signal})=>api.domesticLogPageStatus(signal)});
 const session=status.data?.session;
 const pairing=status.data?.pairing;
 const utcRange=rangeError?null:rangeAsUtc(dateFrom,dateTo);
 const refresh=()=>void qc.invalidateQueries({queryKey:["persistent-session-status"]});
 const start=useMutation({mutationFn:()=>api.startPersistentLogSync({
  environment_id:"china_uat",date_from:utcRange!.dateFromUtc,date_to:utcRange!.dateToUtc,
  timezone:timezoneName,periodic_polling:periodicPolling,
  sync_interval_seconds:interval,maximum_records:maximum,
  maximum_http_reads:periodicPolling?maximumHttpReads:1,
  maximum_records_observed:maximum,maximum_records_accepted:maximum,
  maximum_elapsed_seconds:600,page_size:100,maximum_pages:periodicPolling?10:1,
  persistent_session_id:session!.persistent_session_id,explicit_confirmation:true}),
  onSuccess:()=>{setNotice("无界面后台同步已启动。");setConfirmed(false);refresh();void qc.invalidateQueries()},
  onError:error=>setNotice(message(error))});
 const revoke=useMutation({mutationFn:()=>api.revokePersistentSession(session!.persistent_session_id,revokeText),
  onSuccess:()=>{setNotice("本机加密登录状态已清除；平台服务端会话不一定已注销。");setRevokeText("");refresh()},
  onError:error=>setNotice(message(error))});
 const validate=useMutation({mutationFn:()=>api.validatePersistentSession(session!.persistent_session_id),
  onSuccess:()=>{setNotice("加密保险库完整性、来源和有效期校验通过。");refresh()},
  onError:error=>setNotice(message(error))});
 useEffect(()=>{setConfirmed(false);setNotice("");setRevokeText("")},[environment]);
 if(environment!=="china_uat")return <section className="ops-section"><div className="state error"><b>加密持久后台同步仅限国内 UAT</b><span>海外站和生产环境均不允许使用本机加密会话。</span></div></section>;
 if(status.isLoading)return <section className="ops-section"><div className="state loading">正在读取本机加密会话状态…</div></section>;
 if(!status.data?.enabled)return <section className="ops-section"><div className="state pending-state"><b>加密持久后台同步默认关闭</b><span>由本机管理员设置 UAT_PERSISTENT_SESSION_ENABLED=true 后重启后端。临时模式仍可正常使用。</span></div></section>;
 const remaining=session?Math.max(0,Math.floor((new Date(session.expires_at).getTime()-Date.now())/60000)):0;
 const latest=status.data.latest_job;
 const usageLabel=session?.usage_state==="in_use"?"会话使用中":session?"会话可用":"尚未配对";
 const schemaMappingRequired=latest?.schema_status==="schema_mapping_required";
 const pairingActive=Boolean(pairing&&pairingStates.has(pairing.state));
 const blockers=[
  ...(!logPage.data?.logs_page_url?["日志页尚未确认","允许主机未配置"]:[]),
  ...(!session?["国内 UAT 登录缺失"]:[]),
  ...(session&&(session.state!=="active"||session.authentication_status!=="verified"||session.revocation_status!=="not_revoked")?["国内 UAT 登录已过期"]:[]),
  ...(pairingActive?["配对正在进行"]:[]),
  ...(status.data.active_job?["已有活动同步任务"]:[]),
  ...(rangeError?[rangeError]:[]),
 ];
 return <section className="ops-section persistent-mode">
  <header><div><h2>加密持久后台同步</h2><p>首次人工登录后，批准来源的 Cookie 与 Web Storage 使用 AES‑256‑GCM 加密，随机密钥仅由 Windows DPAPI CurrentUser 保护；后续任务使用全新无界面 Chromium context。</p></div></header>
  {notice&&<div className="notice">{notice}</div>}
  {schemaMappingRequired&&<div className="state error" role="alert">
   <b>UAT 已返回数据，但响应已被安全拒绝</b>
   <span>本次没有导入任何记录；需要完成经过评审的字段映射，系统没有猜测任何字段。</span>
   <small>已观察 {latest?.collected_count||0} 条，接受 {latest?.inserted_count||0} 条，拒绝 {latest?.rejected_count||0} 条。最后原因：{latest?.schema_adapter?.rejection_reason||latest?.schema_status}</small>
  </div>}
  {status.data.last_lease_event?.event_type.includes("recovered")&&<div className="state pending-state">
   <b>已恢复陈旧会话租约</b><span>历史占用状态已清理，会话按当前有效租约重新计算。</span>
  </div>}
  <div className="state pending-state"><b>{usageLabel}</b><span>{status.data.active_job?`${labels[status.data.active_job.state]||status.data.active_job.state}：${status.data.active_job.sync_job_id}`:"当前没有有效同步租约"}</span></div>
  {latest&&<dl className="environment-config-grid">
   <dt>最近任务状态</dt><dd>{labels[latest.state]||latest.state}</dd>
   <dt>Schema 状态</dt><dd>{latest.schema_status}</dd>
   <dt>Schema 适配器</dt><dd>{latest.schema_adapter?.schema_adapter_id||"尚无已评审记录适配器"}</dd>
   <dt>适配器版本</dt><dd>{latest.schema_adapter?.adapter_version||"待评审"}</dd>
   <dt>记录统计</dt><dd>观察 {latest.collected_count} / 接受 {latest.inserted_count} / 拒绝 {latest.rejected_count}</dd>
   <dt>同步模式</dt><dd>{latest.periodic_polling?"有界周期同步":"一次性读取"}</dd>
   <dt>证据类型</dt><dd>{latest.source_type==="measured_uat_realtime_browser_sync"?"当前真实 UAT 日志":latest.source_type==="measured_uat_historical_reconciled"?"历史 CSV 对账证据":latest.source_type||"尚未同步"}</dd>
   <dt>读取 / 页面</dt><dd>{latest.http_read_count} / {latest.maximum_http_reads} 次；{latest.pages_read} / {latest.maximum_pages} 页</dd>
   <dt>连续空读 / 失败</dt><dd>{latest.consecutive_empty_reads} / {latest.consecutive_failures}</dd>
   <dt>最后成功读取</dt><dd>{formatTimestamp(latest.last_successful_read_at)}</dd>
   <dt>Watermark</dt><dd><code>{latest.watermark_utc||"尚无已接受事件"}</code></dd>
   <dt>指标快照</dt><dd><code>{latest.latest_metric_snapshot_id||"尚未生成"}</code></dd>
   <dt>置信度快照</dt><dd><code>{latest.latest_confidence_snapshot_id||"尚未生成"}</code></dd>
   <dt>证据清单</dt><dd><code>{latest.provenance_manifest_sha256||"尚未绑定"}</code></dd>
  </dl>}
  <div className="ops-metrics"><MetricCard label="会话状态" value={session?.state||"未配对"} context={session?.authentication_status||"等待首次登录"} source="AES‑GCM / DPAPI"/><MetricCard label="保险库状态" value={session?"已加密保存":"尚未创建"} context={session?.storage_types?.join(" + ")||"等待认证存储诊断"} source="本机保险库"/><MetricCard label="剩余有效期" value={session?`${remaining} 分钟`:"—"} context={session?formatTimestamp(session.expires_at):"默认 8 小时"} source="本机时钟"/><MetricCard label="配对状态" value={labels[pairing?.state||""]||pairing?.state||"未开始"} context={pairing?.browser_state||"无活动配对浏览器"} source="可见 Chromium"/><MetricCard label="同步记录" value={String(status.data.active_job?.inserted_count||0)} context={status.data.active_job?.sync_job_id||"无活动任务"} source="SQLite"/><MetricCard label="精确关联" value={String(status.data.active_job?.correlated_count||0)} context="仅 Request ID / response ID 精确匹配" source="日志关联"/><MetricCard label="实际费用已补全" value={String(status.data.active_job?.actual_cost_enriched_count||0)} context="仅统计含平台实际费用的精确关联" source="平台日志"/></div>
  <dl className="environment-config-grid"><dt>创建时间</dt><dd>{formatTimestamp(session?.created_at)}</dd><dt>到期时间</dt><dd>{formatTimestamp(session?.expires_at)}</dd><dt>最近验证</dt><dd>{formatTimestamp(session?.last_validated_at)}</dd><dt>最近使用</dt><dd>{formatTimestamp(session?.last_used_at)}</dd><dt>自动恢复</dt><dd>{session?.auto_resume_enabled?"已启用":"默认关闭"}</dd><dt>最近同步</dt><dd>{formatTimestamp(status.data.active_job?.last_successful_poll_at)}</dd></dl>
  <fieldset className="range-panel"><legend>执行模式与读取边界</legend>
   <div className="mode-cards"><label><input type="radio" name="persistent-polling-mode" checked={!periodicPolling} onChange={()=>{setPeriodicPolling(false);setConfirmed(false)}}/><b>一次性读取</b><span>只发出一个已预留的账单日志 GET，收到首个响应后结束。</span></label><label><input type="radio" name="persistent-polling-mode" checked={periodicPolling} onChange={()=>{setPeriodicPolling(true);setConfirmed(false)}}/><b>有界周期同步</b><span>请求完成后至少等待 {interval} 秒；空响应也计入读取次数。</span></label></div>
   {periodicPolling&&<label>最大 HTTP 读取次数<input aria-label="最大 HTTP 读取次数" type="number" min="1" max="20" value={maximumHttpReads} onChange={e=>{setMaximumHttpReads(Number(e.target.value));setConfirmed(false)}}/></label>}
  </fieldset>
  <div className="confirmation-summary"><b>无界面同步确认</b><p>国内 UAT · {dateFrom} 至 {dateTo} · {timezoneName} · {periodicPolling?`每 ${interval} 秒，最多 ${maximumHttpReads} 次读取`:"一次性读取 1 次"} · 最多 {maximum} 条</p><label className="confirmation"><input aria-label="确认启动持久同步" type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} disabled={blockers.length>0}/>我确认使用本机加密登录状态启动只读无界面同步。</label></div>
  {blockers.length>0&&<div className="state error" role="alert" aria-label="无法启动同步的原因"><b>当前不能启动同步</b><ul>{blockers.map(reason=><li key={reason}>{reason}</li>)}</ul></div>}
  <div className="uat-actions"><button onClick={()=>start.mutate()} disabled={blockers.length>0||!confirmed||start.isPending}>{periodicPolling?"启动周期同步":"启动一次性同步"}</button>{status.data.active_job&&<button className="secondary" onClick={()=>api.stopLogSync(status.data!.active_job!.sync_job_id,"china_uat").then(refresh)}>停止同步</button>}</div>
  {session&&<><div className="uat-actions"><button className="secondary" onClick={()=>validate.mutate()} disabled={validate.isPending}>验证加密会话</button></div><label className="confirmation"><input aria-label="启用自动恢复" type="checkbox" checked={false} disabled readOnly/>自动恢复暂未开放；后端重启时会安全终止旧任务并释放租约，请人工重新启动。</label>
  <div className="danger-zone"><label>清除确认文字<input aria-label="清除持久会话确认文字" value={revokeText} onChange={e=>setRevokeText(e.target.value)} placeholder="我确认清除本机保存的UAT登录状态。"/></label><button className="danger" onClick={()=>revoke.mutate()} disabled={revokeText!=="我确认清除本机保存的UAT登录状态。"||revoke.isPending}>清除本地登录状态</button><p className="field-help">只删除本机加密副本，不一定注销平台服务端 UAT 会话。</p></div></>}
 </section>;
}

export default function RealtimeLogSyncPage({environment}:{environment:EnvironmentFilter}){
 const concrete=environment==="all"?null:environment as EnvironmentId;
 const qc=useQueryClient();
 const [jobId,setJobId]=useState<string|null>(null);
 const [interval,setIntervalValue]=useState(7);
 const [maximum,setMaximum]=useState(500);
 const [quickRange,setQuickRange]=useState<LogSyncQuickRange>("24h");
 const initialRange=useRef(calculateQuickRange("24h"));
 const [dateFrom,setDateFrom]=useState(initialRange.current.dateFrom);
 const [dateTo,setDateTo]=useState(initialRange.current.dateTo);
 const [timezoneName,setTimezoneName]=useState(browserTimezone());
 const [confirmed,setConfirmed]=useState(false);
 const [notice,setNotice]=useState("");
 const [syncMode,setSyncMode]=useState<"temporary"|"persistent">(
  concrete==="china_uat"?"persistent":"temporary");
 const previousInserted=useRef(0);
 const environmentStatus=useQuery({queryKey:["collector-status",concrete],
  queryFn:({signal})=>api.collectorStatus(concrete!,signal),enabled:Boolean(concrete)});
 const jobs=useQuery({queryKey:["log-sync-jobs",concrete],
  queryFn:({signal})=>api.logSyncJobs(concrete!,signal),enabled:Boolean(concrete)});
 const job=useQuery({queryKey:["log-sync-job",concrete,jobId],
  queryFn:({signal})=>api.logSyncJob(jobId!,signal),enabled:Boolean(jobId&&concrete),
  refetchInterval:q=>active.has(q.state.data?.state||"")?2000:false});
 const events=useQuery({queryKey:["log-sync-events",concrete],
  queryFn:({signal})=>api.logSyncEvents(concrete!,0,signal),enabled:Boolean(concrete),
  refetchInterval:5000});
 const reconciliation=useQuery({queryKey:["log-sync-reconciliation",concrete],
  queryFn:({signal})=>api.logSyncReconciliation(concrete!,signal),enabled:Boolean(concrete),
  refetchInterval:5000});
 const maximumRangeHours=Number(import.meta.env.VITE_LOG_SYNC_MAX_RANGE_HOURS||168);
 const clockSkewSeconds=Number(import.meta.env.VITE_LOG_SYNC_CLOCK_SKEW_SECONDS||300);
 const rangeError=validateLogSyncRange(
  dateFrom,dateTo,new Date(),maximumRangeHours,clockSkewSeconds);
 const utcRange=rangeError?null:rangeAsUtc(dateFrom,dateTo);
 const start=useMutation({mutationFn:()=>api.startLogSync({
  environment_id:concrete!,date_from:utcRange!.dateFromUtc,date_to:utcRange!.dateToUtc,
  timezone:timezoneName,sync_interval_seconds:interval,maximum_records:maximum,
 }),
  onSuccess:data=>{setJobId(data.sync_job_id);setNotice("已请求打开新的非持久浏览器窗口。");void jobs.refetch()},
  onError:error=>setNotice(message(error))});
 const confirm=useMutation({mutationFn:()=>api.confirmLogSyncLogin(jobId!,concrete!),
  onSuccess:()=>{setNotice("已确认登录，开始只读近实时同步。");void job.refetch()},
  onError:error=>setNotice(message(error))});
 const stop=useMutation({mutationFn:()=>api.stopLogSync(jobId!,concrete!),
  onSuccess:()=>{setNotice("同步已停止，浏览器上下文和会话材料已销毁。");void job.refetch();void jobs.refetch()},
  onError:error=>setNotice(message(error))});
 useEffect(()=>{
  const next=calculateQuickRange("24h");
  setJobId(null);setNotice("");previousInserted.current=0;
  setQuickRange("24h");setDateFrom(next.dateFrom);setDateTo(next.dateTo);
  setTimezoneName(browserTimezone());setConfirmed(false);
  setSyncMode(concrete==="china_uat"?"persistent":"temporary");
 },[concrete]);
 useEffect(()=>{
  const count=job.data?.inserted_count||0;
  if(count>previousInserted.current){
   previousInserted.current=count;
   void qc.invalidateQueries();
  }
 },[job.data?.inserted_count,qc]);
 if(!concrete)return <div className="state error"><b>请选择具体环境</b><span>近实时日志同步不会混合国内 UAT 与海外站。</span></div>;
 if(environmentStatus.isLoading)return <div className="state loading">正在读取日志同步配置…</div>;
 const status=environmentStatus.data;
 const current=job.data;
 const pending=Math.max(0,(current?.inserted_count||0)-(current?.correlated_count||0)-(current?.ambiguous_count||0));
 const temporaryBlockers=[
  ...(!status?.log_page_url?["日志页尚未确认"]:[]),
  ...(!status?.allowed_hosts.length?["允许主机未配置"]:[]),
  ...(rangeError?[rangeError]:[]),
  ...(current&&active.has(current.state)?["已有活动同步任务"]:[]),
 ];
  return <main className="collector-page">
   <header className="ops-page-header collector-hero"><div><p className="eyebrow">{concrete==="china_uat"?"FORMAL CHROME · PERSISTENT PROFILE · READ-ONLY":"VISIBLE · TEMPORARY · READ-ONLY"}</p><h1>UAT 日志近实时同步</h1><p>平台日志可能延迟到达。Completion 响应元数据不等同于平台账单证据，费用和实际渠道只在平台日志明确提供后展示。</p></div><div className="collector-orbit"><span className={current&&active.has(current.state)?"pulse":""}/><b>{labels[current?.state||"idle"]}</b><small>{status?.environment_name||concrete}</small></div></header>
   {concrete==="china_uat"&&<DomesticAccessRecoveryPanel/>}
   <section className="ops-section mode-selector"><h2>同步模式</h2><div className="mode-cards">{concrete==="china_uat"?<div className="state success"><b>正式 Chrome 人工认证 + 加密持久后台同步</b><span>人工认证只使用项目专用 Google Chrome；临时 Playwright Chromium 仅用于 Mock E2E，不作为真人认证入口。</span></div>:<label><input type="radio" name="sync-mode" checked={syncMode==="temporary"} onChange={()=>setSyncMode("temporary")}/><b>临时后台同步</b><span>仅用于已评审的非国内 UAT 环境。</span></label>}</div></section>
  {syncMode==="persistent"?<PersistentModePanel environment={concrete} dateFrom={dateFrom} dateTo={dateTo} timezoneName={timezoneName} interval={interval} maximum={maximum} rangeError={rangeError}/>:<>
  <section className="ops-section collector-config"><header><div><h2>启动只读同步</h2><p>点击后打开可见临时 Chromium；请在新窗口中手动完成登录、CAPTCHA 或 MFA。</p></div></header>
   <dl className="environment-config-grid"><dt>环境</dt><dd>{status?.environment_name||concrete}</dd><dt>批准日志页</dt><dd>{status?.log_page_url||"尚未确认"}</dd><dt>允许主机</dt><dd>{status?.allowed_hosts.join("、")||"—"}</dd><dt>认证材料</dt><dd>不持久化 Cookie、密码或 Authorization</dd></dl>
   <fieldset className="range-panel"><legend>日期与时间范围</legend>
    <div className="quick-ranges" aria-label="快捷时间范围">{([
     ["15m","最近15分钟"],["1h","最近1小时"],["24h","最近24小时"],["today","今天"],["custom","自定义"],
    ] as Array<[LogSyncQuickRange,string]>).map(([value,label])=><button type="button" key={value} className={quickRange===value?"active secondary":"secondary"} aria-pressed={quickRange===value} onClick={()=>{
      setQuickRange(value);setConfirmed(false);
      if(value!=="custom"){const next=calculateQuickRange(value);setDateFrom(next.dateFrom);setDateTo(next.dateTo)}
     }}>{label}</button>)}</div>
    <div className="scope-grid"><label>开始日期和时间<input aria-label="开始日期和时间" type="datetime-local" value={dateFrom} onChange={e=>{setDateFrom(e.target.value);setQuickRange("custom");setConfirmed(false)}}/></label><label>结束日期和时间<input aria-label="结束日期和时间" type="datetime-local" value={dateTo} onChange={e=>{setDateTo(e.target.value);setQuickRange("custom");setConfirmed(false)}}/></label></div>
    <p className="field-help">浏览器本地时区：<b>{timezoneName}</b></p>
    {rangeError&&<div className="state error" role="alert">{rangeError}</div>}
    {utcRange&&<details><summary>技术时间详情（UTC）</summary><dl><dt>date_from</dt><dd><code>{utcRange.dateFromUtc}</code></dd><dt>date_to</dt><dd><code>{utcRange.dateToUtc}</code></dd><dt>timezone</dt><dd>{timezoneName}</dd></dl></details>}
   </fieldset>
   <div className="scope-grid"><label>同步间隔（秒）<input aria-label="同步间隔" type="number" min="7" max="30" value={interval} onChange={e=>{setIntervalValue(Number(e.target.value));setConfirmed(false)}}/></label><label>最多记录<input aria-label="最多记录" type="number" min="1" max="1000" value={maximum} onChange={e=>{setMaximum(Number(e.target.value));setConfirmed(false)}}/></label></div>
   <div className="confirmation-summary"><b>启动确认摘要</b><p>{status?.environment_name||concrete} · {dateFrom||"未填写"} 至 {dateTo||"未填写"} · {timezoneName} · 每 {interval} 秒 · 最多 {maximum} 条</p><label className="confirmation"><input aria-label="确认同步范围" type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} disabled={Boolean(rangeError)}/>我已核对环境与时间范围，并确认只读同步。</label></div>
    {temporaryBlockers.length>0&&<div className="state error" role="alert" aria-label="无法启动同步的原因"><b>当前不能启动同步</b><ul>{temporaryBlockers.map(reason=><li key={reason}>{reason}</li>)}</ul></div>}
    <div className="uat-actions"><button disabled={temporaryBlockers.length>0||!confirmed||start.isPending} onClick={()=>start.mutate()}>{start.isPending?"正在打开安全浏览器":"启动实时日志同步"}</button>{current&&active.has(current.state)&&<button className="secondary" onClick={()=>stop.mutate()} disabled={stop.isPending}>停止同步</button>}</div>
   {notice&&<div className="notice">{notice}</div>}
   <details className="collector-cli"><summary>诊断后备方式</summary><p>正常流程不需要终端或文件上传；旧 Collector CLI 仅保留用于受控诊断。</p></details>
  </section>
  {current&&<section className="ops-section collector-progress"><header><div><h2>同步状态</h2><p>这是近实时轮询状态，不是模拟进度。</p></div><span className={`run-state state-${current.state}`}>{labels[current.state]||current.state}</span></header>
   <dl className="environment-config-grid"><dt>任务 ID</dt><dd><code>{current.sync_job_id}</code></dd><dt>环境</dt><dd>{current.environment_id}</dd><dt>所选范围</dt><dd>{formatTimestamp(current.date_from_utc)} 至 {formatTimestamp(current.date_to_utc)}（{current.timezone}）</dd><dt>浏览器状态</dt><dd>{current.browser_context_active?"可见临时浏览器已打开":"浏览器上下文未活动"}</dd><dt>Schema 状态</dt><dd>{current.schema_status}</dd></dl>
   <div className="heartbeat-strip"><span className="heartbeat-dot"/><b>{current.current_safe_url||current.safe_log_page_url}</b><small>最近同步：{formatTimestamp(current.last_successful_poll_at)}</small></div>
   <div className="ops-metrics"><MetricCard label="已观察" value={String(current.collected_count)} context="平台日志候选" source={current.source_type}/><MetricCard label="已接受" value={String(current.inserted_count)} context="范围内幂等新增" source="SQLite"/><MetricCard label="范围外" value={String(current.out_of_range_count)} context="严格排除" source="平台时间戳"/><MetricCard label="时间未解析" value={String(current.missing_timestamp_count)} context="不猜测" source="平台证据"/><MetricCard label="重复" value={String(current.duplicate_count)} context="未重复写入" source="增量 cursor"/><MetricCard label="精确关联" value={String(current.correlated_count)} context="Request ID / Response ID" source="平台证据"/><MetricCard label="歧义关联" value={String(current.ambiguous_count)} context="不会自动接受" source="候选匹配"/><MetricCard label="等待平台日志" value={String(pending)} context="不推断费用或渠道" source="本地执行"/></div>
   {["waiting_for_manual_login","waiting_for_operator_confirmation"].includes(current.state)&&<div className="login-gate"><b>请在新窗口中手动登录并导航到批准日志页</b><p>应用不会读取或保存密码，也不会处理 CAPTCHA/MFA。</p><button disabled={!current.current_safe_url||confirm.isPending} onClick={()=>confirm.mutate()}>我已完成登录，开始只读同步</button></div>}
   {current.state==="syncing"&&<progress max={current.maximum_records} value={current.inserted_count}/>}
   {current.state==="session_expired"&&<div className="state error"><b>登录会话已失效</b><span>已停止采集并保留已有证据；请重新启动并手动登录。</span></div>}
   {current.safe_error_message&&<div className="state error"><b>{current.safe_error_message}</b><small>停止原因：{current.stop_reason||current.error_code||"待确认"}</small><small>发生时间：{formatTimestamp(current.error_at||current.stopped_at)}</small><small>建议操作：{current.suggested_next_action||"查看最近事件并修正本地条件后重试。"}</small></div>}
   <TechnicalDataDrawer data={{sync_job_id:current.sync_job_id,source_type:current.source_type,network_called:current.network_called,cookies_persisted:current.cookies_persisted,credentials_persisted:current.credentials_persisted,poll_interval_seconds:current.poll_interval_seconds,last_poll_at:current.last_poll_at}}/>
  </section>}
  </>}
  <section className="ops-section"><header><div><h2>费用与渠道对账</h2><p>同时保留估算值与平台实际值；Scheduler 推荐绝不替代平台实际渠道。</p></div></header>
   <div className="table-wrap dense"><table><thead><tr><th>执行 ID</th><th>平台日志</th><th>估算费用</th><th>平台实际费用</th><th>Scheduler 推荐渠道</th><th>平台实际渠道</th><th>状态</th></tr></thead><tbody>
    {reconciliation.data?.items.map((item,index)=><tr key={`${item.execution_id}-${item.platform_log_id}-${index}`}><td><code>{item.execution_id||"待确认"}</code></td><td>{item.platform_log_id||"—"}</td><td>{money(item.estimated_cost,item.currency)}</td><td>{money(item.actual_cost,item.currency)}</td><td>{item.scheduler_recommendation||"未记录"}</td><td>{item.actual_channel_name||item.actual_channel_id||"待平台渠道日志确认"}</td><td>{reconciliationLabels[item.reconciliation_status]||item.reconciliation_status}</td></tr>)}
    {!reconciliation.data?.items.length&&<tr><td colSpan={7}>等待平台日志；当前没有可展示的费用或渠道关联。</td></tr>}
   </tbody></table></div>
  </section>
  <section className="ops-section"><h2>最近同步事件</h2><div className="table-wrap dense"><table><thead><tr><th>时间</th><th>事件</th></tr></thead><tbody>{events.data?.items.slice(-10).reverse().map(event=><tr key={event.event_id}><td>{formatTimestamp(event.created_at)}</td><td>{event.event_type}</td></tr>)}{!events.data?.items.length&&<tr><td colSpan={2}>暂无同步事件。</td></tr>}</tbody></table></div></section>
 </main>;
}
