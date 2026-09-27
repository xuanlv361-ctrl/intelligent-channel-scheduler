import {useState} from "react";
import {useMutation, useQuery, useQueryClient} from "@tanstack/react-query";
import {Link,useSearchParams} from "react-router-dom";
import {api,ApiError,BugReport,DataMode,ReplayResult} from "../services/api";
import {DataSourceBanner,EmptyChart,MetricCard,TechnicalDataDrawer} from "../components/operations/OperationsUI";
import {formatBoolean,formatCompatibilityStatus,formatErrorCategory,formatMappingStatus,formatMissingValue,formatPercentage,formatSourceType,labelEnum} from "../lib/presentation";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

function downloadText(filename:string,text:string,mime="text/plain"){
  const url=URL.createObjectURL(new Blob([text],{type:mime}));
  const a=document.createElement("a");a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url);
}

function Header({title,purpose}:{title:string;purpose:string}){return <header className="ops-page-header"><div><p className="eyebrow">OPERATIONS CONSOLE</p><h1>{title}</h1><p>{purpose}</p></div></header>}
function Loading(){return <div className="state loading" role="status">正在读取本地证据…</div>}
function Failure({retry}:{retry:()=>void}){return <div className="state error" role="alert">页面数据加载失败。<button onClick={retry}>重试</button></div>}
function usePage(path:string,mode:DataMode){return useQuery({queryKey:[path,mode],queryFn:({signal})=>api.get<Record<string,unknown>>(path,mode,signal)})}
function Provenance({mode,source,sample}:{mode:DataMode;source:unknown;sample:number}){const raw=String(source|| (mode==="demo"?"demo_mock":"measured_uat"));return <DataSourceBanner genuine={raw!=="demo_mock"&&raw!=="integration_test_fixture"} label={formatSourceType(raw)} sample={sample} kind={mode==="demo"?"demo":"uat"}/>}

export function CompatibilityPage({mode}:{mode:DataMode}){
 const q=usePage("/api/v1/compatibility",mode);if(q.isLoading)return <Loading/>;if(q.error)return <Failure retry={()=>q.refetch()}/>;
 const d=q.data!,items=(d.items||[]) as Record<string,unknown>[];
 return <><Header title="兼容性测试" purpose="按能力项核对协议支持情况；HTTP 200 不等于完整兼容。"/><Provenance mode={mode} source={d.source_type} sample={Number(d.sample_size||0)}/>
  <section className="ops-metrics"><MetricCard label="能力项" value={String(items.length)} context="当前矩阵行数" source={formatSourceType(d.source_type)}/><MetricCard label="已通过" value={String(items.filter(x=>x.status==="passed").length)} context="有证据支持" source="兼容性证据"/><MetricCard label="待确认" value={String(items.filter(x=>x.status==="pending_confirmation").length)} context="不可推断支持" source="兼容性证据"/></section>
  <section className="ops-section"><header><div><h2>渠道能力矩阵</h2><p>每个状态同时显示文字，不依赖颜色。</p></div></header><div className="table-wrap dense"><table><thead><tr><th>能力</th><th>状态</th><th className="num">证据数</th><th>最后测试</th><th>说明</th></tr></thead><tbody>{items.map((x,i)=><tr key={i}><td>{formatMissingValue(x.capability)}</td><td>{formatCompatibilityStatus(x.status)}</td><td className="num">{formatMissingValue(x.evidence_count,"no-sample")}</td><td>{formatMissingValue(x.last_tested,"not-executed")}</td><td>{formatMissingValue(x.notes)}</td></tr>)}</tbody></table></div></section>
  <section className="interpretation"><h2>兼容性说明</h2><p>“待确认”和“未测试”不会被当作支持；需要对应能力的请求与响应证据。</p></section><TechnicalDataDrawer data={d}/></>
}

export function ShadowRoutingPage({mode}:{mode:DataMode}){
 const qc=useQueryClient();
 const [search,setSearch]=useState("");
 const [association,setAssociation]=useState("all");
 const [modelFilter,setModelFilter]=useState("all");
 const [channelFilter,setChannelFilter]=useState("all");
 const [agreementFilter,setAgreementFilter]=useState("all");
 const [page,setPage]=useState(1);
 const [pageSize,setPageSize]=useState(25);
 const [manualRecord,setManualRecord]=useState<string|null>(null);
 const [manualExecution,setManualExecution]=useState("");
 const q=useQuery({queryKey:["shadow-dashboard"],queryFn:({signal})=>api.shadowDashboard(signal),refetchInterval:5000});
 const sync=useMutation({mutationFn:api.ensureLogSync,onSuccess:()=>qc.invalidateQueries({queryKey:["shadow-dashboard"]})});
 const confirm=useMutation({mutationFn:({recordId,executionId}:{recordId:string;executionId:string})=>api.confirmShadowCorrelation(recordId,executionId),onSuccess:()=>{setManualRecord(null);setManualExecution("");qc.invalidateQueries({queryKey:["shadow-dashboard"]});}});
 if(q.isLoading)return <Loading/>;if(q.error)return <Failure retry={()=>q.refetch()}/>;
 const d=q.data!;
 const text=search.trim().toLowerCase();
 const filtered=d.items.filter(row=>(association==="all"||row.correlation_status===association)&&
  (modelFilter==="all"||row.model===modelFilter)&&(channelFilter==="all"||row.actual_channel===channelFilter)&&
  (agreementFilter==="all"||(agreementFilter==="comparable"&&row.comparable)||(agreementFilter==="matched"&&row.matches===true)||(agreementFilter==="different"&&row.matches===false)||(agreementFilter==="not_comparable"&&!row.comparable))&&(!text||[
  row.request_id,row.response_id,row.decision_id,row.model,row.actual_channel,row.platform_log_id
 ].some(value=>String(value||"").toLowerCase().includes(text))));
 const models=[...new Set(d.items.map(row=>row.model).filter((value):value is string=>Boolean(value)))].sort();
 const channels=[...new Set(d.items.map(row=>row.actual_channel).filter((value):value is string=>Boolean(value)))].sort();
 const totalPages=Math.max(1,Math.ceil(filtered.length/pageSize));
 const currentPage=Math.min(page,totalPages);
 const items=filtered.slice((currentPage-1)*pageSize,currentPage*pageSize);
 const connectionLabel:Record<string,string>={connected:"会话可用",not_configured:"自动恢复待执行",authentication_required:"自动恢复中"};
 const syncLabel:Record<string,string>={syncing:"同步中",sync_succeeded:"同步成功",sync_failed:"同步失败",not_configured:"未配置",stopped:"已停止",completed:"已完成",blocked:"已阻断"};
 const relationLabel:Record<string,string>={exact_provider_request_id:"Provider Request ID 精确关联",exact_provider_response_id:"Provider Response ID 精确关联",exact_provider_trace_id:"Provider Trace ID 精确关联",exact_client_correlation_id:"客户端关联 ID 精确关联",manually_confirmed:"人工确认",strong_candidate:"候选关联",unmatched:"未关联",ambiguous:"冲突"};
 const localTime=(value:string|null)=>value?new Date(value).toLocaleString():"尚未同步";
 const pageHeader=<header className="ops-page-header shadow-header"><div><p className="eyebrow">REAL EVIDENCE WORKSPACE</p><h1>影子调度</h1><p>系统自动同步平台日志；关联与影子计算分别展示，不会因关联证据不足隐藏已同步数据。</p></div>{mode==="uat"&&<div className="page-actions"><button onClick={()=>sync.mutate()} disabled={sync.isPending}>{sync.isPending?"正在检查新日志…":"立即刷新"}</button><a className="button secondary" href="#sync-history">查看同步历史</a></div>}</header>;
 if(mode==="demo")return <>{pageHeader}<div className="state empty" role="status"><b>影子调度未配置演示数据。</b><span>请在顶部切换到“真实数据”查看同步日志；系统不会用 demo_mock 指标替代真实日志或关联结果。</span></div></>;
 const explainableDecision=d.items.find(row=>Boolean(row.decision_id))?.decision_id;
 return <>{pageHeader}
 <BusinessSkillAction skillId="explain-routing-decision" label="解释影子关联决策" argumentsValue={{decision_id:explainableDecision??"",include_metrics:true,include_execution:true}} disabledReason={explainableDecision?undefined:"当前同步日志没有精确关联的 decision_id"}/>
 {sync.error&&<div className="notice warning" role="status">自动同步正在重试，当前继续展示最近一次已持久化数据。</div>}
 {d.connection_status!=="connected"&&<div className="notice warning" role="status">正在自动恢复日志会话，当前展示最近一次已同步数据。</div>}
 <section className="ops-section shadow-sync-strip" aria-label="日志同步状态"><div><small>日志同步状态</small><strong>{syncLabel[d.sync_status]||labelEnum(d.sync_status)}</strong></div><div><small>决策关联状态</small><strong>{d.metrics.linked_executions?`已精确关联 ${d.metrics.linked_executions} 条`:"历史标识不足"}</strong></div><div><small>影子计算状态</small><strong>{d.metrics.comparable_logs?`已生成 ${d.metrics.comparable_logs} 条可比结果`:"决策证据不足"}</strong></div><div><small>上次成功</small><strong>{localTime(d.last_successful_at)}</strong></div><div><small>本次新增</small><strong>{d.latest_job?.inserted_count??0} 条</strong></div><div><small>会话</small><strong>{connectionLabel[d.connection_status]||labelEnum(d.connection_status)}</strong></div></section>
 <section className="ops-metrics shadow-metrics"><MetricCard label="已同步日志" value={String(d.metrics.synced_logs)} context="真实日志存储" source="真实日志"/><MetricCard label="已关联执行" value={String(d.metrics.linked_executions)} context="仅精确或人工确认" source="真实关联"/><MetricCard label="未关联日志" value={String(d.metrics.unlinked_logs)} context="可在下方查看原因" source="真实日志"/><MetricCard label="建议一致率" value={d.metrics.agreement_rate===null?"暂无数据":formatPercentage(d.metrics.agreement_rate)} context={`分母 ${d.metrics.comparable_logs} 条可比较日志`} source="真实关联"/><MetricCard label="平均延迟差异" value={d.metrics.average_latency_difference_ms===null?"不可计算":`${d.metrics.average_latency_difference_ms.toFixed(0)} ms`} context="缺少建议侧估计时不推断" source="可比证据"/><MetricCard label="预计费用差异" value={d.metrics.estimated_cost_difference===null?"不可计算":`¥${d.metrics.estimated_cost_difference.toFixed(4)}`} context="仅使用同币种可比记录" source="可比证据"/></section>
 <section className="ops-section shadow-comparison"><header><div><h2>实际与建议对比</h2><p>只有真实日志与执行证据精确关联后才进入一致率分母。</p></div></header>{d.metrics.comparable_logs?<div className="shadow-mini-chart"><div><span>一致</span><b>{Math.round((d.metrics.agreement_rate||0)*d.metrics.comparable_logs)}</b></div><div><span>不一致</span><b>{d.metrics.comparable_logs-Math.round((d.metrics.agreement_rate||0)*d.metrics.comparable_logs)}</b></div></div>:<div className="compact-empty">暂无可比较的真实关联数据；反事实结果不可计算。请查看未关联日志。</div>}</section>
 <section className="ops-section"><header><div><h2>日志明细</h2><p>敏感 Prompt、响应正文和认证材料不进入此列表。</p></div></header><div className="filter-bar"><label>搜索<input value={search} onChange={e=>{setSearch(e.target.value);setPage(1)}} placeholder="Request ID、Response ID、decision_id"/></label><label>模型<select value={modelFilter} onChange={e=>{setModelFilter(e.target.value);setPage(1)}}><option value="all">全部模型</option>{models.map(value=><option key={value}>{value}</option>)}</select></label><label>渠道<select value={channelFilter} onChange={e=>{setChannelFilter(e.target.value);setPage(1)}}><option value="all">全部渠道</option>{channels.map(value=><option key={value}>{value}</option>)}</select></label><label>关联状态<select value={association} onChange={e=>{setAssociation(e.target.value);setPage(1)}}><option value="all">全部</option><option value="exact_request_id">Request ID 精确关联</option><option value="exact_response_id">Response ID 精确关联</option><option value="exact_decision_id">decision_id 精确关联</option><option value="manually_confirmed">人工确认</option><option value="unmatched">未关联</option><option value="strong_candidate">候选关联</option><option value="ambiguous">冲突</option></select></label><label>一致性<select value={agreementFilter} onChange={e=>{setAgreementFilter(e.target.value);setPage(1)}}><option value="all">全部</option><option value="comparable">可比较</option><option value="matched">一致</option><option value="different">不一致</option><option value="not_comparable">不可比较</option></select></label></div>{items.length?<><div className="table-wrap dense"><table><thead><tr><th>时间</th><th>Request ID</th><th>决策</th><th>模型</th><th>实际渠道</th><th>影子建议</th><th>关联状态</th><th>一致性</th><th>延迟</th><th>费用</th><th>状态</th></tr></thead><tbody>{items.map(row=><tr key={row.record_id}><td>{localTime(row.occurred_at)}</td><td><code>{row.request_id||"未提供"}</code></td><td>{row.decision_id?<Link to={`/routing/decisions/${encodeURIComponent(row.decision_id)}`}>决策日志</Link>:"未记录"}</td><td>{row.model||"未提供"}</td><td>{row.actual_channel||"平台未提供"}</td><td>{row.shadow_recommendation||"待生成"}</td><td>{relationLabel[row.correlation_status]||labelEnum(row.correlation_status)}</td><td>{row.matches===null?"不可比较":row.matches?"一致":"不一致"}</td><td>{row.latency_ms===null?"未提供":`${row.latency_ms} ms`}</td><td>{row.actual_cost===null?"未提供":`${row.currency||"CNY"} ${row.actual_cost}`}</td><td>{row.result||row.http_status||"未提供"}</td></tr>)}</tbody></table></div><div className="pagination"><span>共 {filtered.length} 条，第 {currentPage}/{totalPages} 页</span><label>每页<select value={pageSize} onChange={e=>{setPageSize(Number(e.target.value));setPage(1)}}><option>25</option><option>50</option><option>100</option></select></label><button className="secondary" disabled={currentPage<=1} onClick={()=>setPage(value=>value-1)}>上一页</button><button className="secondary" disabled={currentPage>=totalPages} onClick={()=>setPage(value=>value+1)}>下一页</button></div></>:<div className="compact-empty">当前筛选条件下暂无真实日志。</div>}</section>
 <section className="ops-section"><h2>待关联日志</h2><p>自动关联只使用完全相等的Provider标识；不会使用模型、时间、Token或费用进行模糊匹配。</p>{confirm.error&&<div className="state error" role="alert">{confirm.error instanceof ApiError?confirm.error.message:"人工关联失败"}</div>}{manualRecord&&<div className="manual-correlation" role="group" aria-label="人工确认关联"><label>执行 ID<input value={manualExecution} onChange={e=>setManualExecution(e.target.value)} placeholder="请输入已核对的 execution_id"/></label><button disabled={!manualExecution.trim()||confirm.isPending} onClick={()=>confirm.mutate({recordId:manualRecord,executionId:manualExecution.trim()})}>{confirm.isPending?"正在确认…":"确认关联"}</button><button className="secondary" onClick={()=>{setManualRecord(null);setManualExecution("")}}>取消</button></div>}{d.unlinked.length?<div className="table-wrap dense"><table><thead><tr><th>平台日志 ID</th><th>Provider Request ID</th><th>模型</th><th>时间</th><th>原因</th><th>操作</th></tr></thead><tbody>{d.unlinked.slice(0,50).map(row=><tr key={`u-${row.record_id}`}><td>{row.platform_log_id||"未提供"}</td><td>{row.provider_request_id||"未提供"}</td><td>{row.model||"未提供"}</td><td>{localTime(row.occurred_at)}</td><td>历史执行未保存Provider关联标识</td><td><button className="secondary" onClick={()=>{setManualRecord(row.record_id);setManualExecution(row.execution_id||"")}}>人工确认</button></td></tr>)}</tbody></table></div>:<div className="compact-empty">没有待关联日志。</div>}</section>
 <section className="ops-section" id="sync-history"><h2>同步历史</h2>{d.sync_history.length?<div className="table-wrap dense"><table><thead><tr><th>批次 ID</th><th>查询区间</th><th>读取/页</th><th>新增</th><th>重复</th><th>拒绝</th><th>游标/水位</th><th>状态</th><th>错误</th></tr></thead><tbody>{d.sync_history.slice(0,30).map(job=><tr key={job.sync_job_id}><td><code>{job.sync_job_id}</code><small>{job.source_type}</small></td><td>{localTime(job.date_from_utc)}<br/>{localTime(job.date_to_utc)}</td><td>{job.http_read_count} / {job.pages_read}</td><td>{job.inserted_count}</td><td>{job.duplicate_count}</td><td>{job.rejected_count+job.out_of_range_count+job.missing_timestamp_count}</td><td>{job.watermark_utc?localTime(job.watermark_utc):job.source_cursor||"未生成"}</td><td>{syncLabel[job.state]||labelEnum(job.state)}<br/><small>{job.stop_reason||"未结束"}</small></td><td>{job.safe_error_message||"无"}</td></tr>)}</tbody></table></div>:<div className="compact-empty">尚无同步批次。</div>}</section>
 <details className="ops-section"><summary>查看技术字段</summary><dl className="technical-grid"><dt>游标存储</dt><dd>{String(d.technical.cursor_storage)}</dd><dt>日志存储</dt><dd>{String(d.technical.record_storage)}</dd><dt>关联存储</dt><dd>{String(d.technical.correlation_storage)}</dd><dt>Completion 调用</dt><dd>{String(d.technical.completion_calls)}</dd><dt>当前游标</dt><dd>{d.cursor?JSON.stringify(d.cursor):"待生成"}</dd><dt>来源</dt><dd>{d.source_type}</dd></dl></details></>
}

const REPLAY_STRATEGIES=["fastest_first","cheapest_first","reliability_first","round_robin","fixed_channel","weighted_v1","confidence_aware_v2","health_aware_scheduler"];
export function ReplayPage(){
 const [baseline,setBaseline]=useState("health_aware_scheduler");
 const [candidate,setCandidate]=useState("fastest_first");
 const mutation=useMutation({mutationFn:()=>api.runReplay(candidate)});
 const d=mutation.data as ReplayResult|undefined;
 return <><Header title="历史回放" purpose="使用既有证据比较候选策略；结果属于离线估计，不是已实现收益。"/><DataSourceBanner genuine={false} label="演示数据" sample={d?5:0} kind="demo"/>
  <div className="filter-bar"><label>基线策略<select value={baseline} onChange={e=>setBaseline(e.target.value)}>{REPLAY_STRATEGIES.map(s=><option key={s} value={s}>{s}</option>)}</select></label><label>候选策略<select value={candidate} onChange={e=>setCandidate(e.target.value)}>{REPLAY_STRATEGIES.filter(s=>s!==baseline).map(s=><option key={s} value={s}>{s}</option>)}</select></label><button onClick={()=>mutation.mutate()} disabled={mutation.isPending}>{mutation.isPending?"回放中…":"运行离线回放"}</button></div>
  {mutation.error&&<div className="state error" role="alert">{mutation.error instanceof ApiError?mutation.error.message:"回放失败"}</div>}
  {d?<><section className="ops-metrics">
      <MetricCard label="推荐候选" value={d.selected_candidate} context={`策略：${d.strategy}`} source="离线估计"/>
      <MetricCard label="选择变化率" value={formatPercentage(d.changed_decision_rate)} context="候选与基线不同" source="离线估计"/>
      <MetricCard label="不可调度" value={String(d.unroutable_count)} context="风险请求" source="离线估计"/>
      <MetricCard label="网络调用" value={String(d.network_calls)} context="未调用外部 API" source="安全边界"/>
    </section>
    <section className="ops-section"><h2>回放明细</h2><div className="table-wrap dense"><table><tbody>
      <tr><th>回放 ID</th><td><code>{d.replay_id}</code></td></tr>
      <tr><th>执行模式</th><td>{labelEnum(d.execution_mode)}</td></tr>
      <tr><th>预估成本</th><td>{d.cost_estimate_cny===null||d.cost_estimate_cny===undefined?"待确认":`¥${Number(d.cost_estimate_cny).toFixed(4)}`}</td></tr>
      <tr><th>预估延迟</th><td>{d.latency_estimate_ms} ms</td></tr>
      <tr><th>SLA 风险</th><td>{formatPercentage(d.sla_risk_estimate)}</td></tr>
      <tr><th>集中度风险</th><td>{formatPercentage(d.concentration_risk)}</td></tr>
      <tr><th>低置信度候选</th><td>{d.low_confidence_count}</td></tr>
      <tr><th>过期数据</th><td>{d.stale_data_count}</td></tr>
    </tbody></table></div></section>
    <TechnicalDataDrawer data={d}/></>
   :<EmptyChart>选择策略后运行本地回放；不会访问外部网络。</EmptyChart>}
  <section className="interpretation"><h2>如何解释</h2><p>回放结果用于发现选择变化和风险，不表示已经降低成本或改善延迟。</p></section></>
}

export function ErrorCenterPage({mode}:{mode:DataMode}){
 const [searchParams,setSearchParams]=useSearchParams();
 const q=usePage("/api/v1/errors",mode);if(q.isLoading)return <Loading/>;if(q.error)return <Failure retry={()=>q.refetch()}/>;
 const query=(searchParams.get("q")||"").trim().toLowerCase();
 const d=q.data!,allItems=(d.items||[]) as Record<string,unknown>[];
 const items=query?allItems.filter(x=>[x.error_id,x.request_id,x.channel_id,x.error_category,x.error_layer].some(v=>String(v??"").toLowerCase().includes(query))):allItems;
 const analyzable=items.find(item=>item.request_id||item.error_id);
 return <><Header title="错误中心" purpose="按类别、层级和安全重试边界整理失败请求。"/><Provenance mode={mode} source={d.source_type} sample={Number(d.sample_size||0)}/>
 <BusinessSkillAction skillId="classify-provider-error" label="分析最近错误" argumentsValue={{request_id:analyzable?.request_id??"",error_id:analyzable?.error_id??""}} disabledReason={analyzable?undefined:"当前没有可分析的真实错误记录"}/>
  {query&&<div className="notice">按“{searchParams.get("q")}”过滤：{items.length} / {allItems.length} 条匹配（顶栏搜索仅在本页对已加载证据做客户端过滤，不发起新请求）。<button className="link-button" onClick={()=>setSearchParams({})}>清除过滤</button></div>}
  <section className="ops-metrics"><MetricCard label="错误数" value={String(items.length)} context="当前范围" source={formatSourceType(d.source_type)}/><MetricCard label="可重试" value={String(items.filter(x=>x.retryable).length)} context="仍受最大尝试次数限制" source="错误策略"/><MetricCard label="允许回退" value={String(items.filter(x=>x.fallback_allowed).length)} context="需满足执行策略" source="错误策略"/><MetricCard label="受影响渠道" value={String(new Set(items.map(x=>x.channel_id).filter(Boolean)).size)} context="按有权威标识的观测渠道去重" source="错误证据"/></section>
  <section className="ops-section"><header><div><h2>错误明细</h2><p>原始错误枚举仅保留在技术数据中。</p></div></header>{items.length?<div className="table-wrap dense"><table><thead><tr><th>错误 ID</th><th>请求 ID</th><th>决策</th><th>渠道</th><th>错误类别</th><th>层级</th><th>可重试</th><th>允许回退</th><th>建议</th></tr></thead><tbody>{items.map((x,i)=><tr key={i}><td><code>{formatMissingValue(x.error_id)}</code></td><td>{formatMissingValue(x.request_id)}</td><td>{x.decision_id?<Link to={`/routing/decisions/${encodeURIComponent(String(x.decision_id))}`}>决策日志</Link>:"未记录"}</td><td>{formatMissingValue(x.channel_id)}</td><td>{formatErrorCategory(x.error_category)}</td><td>{labelEnum(x.error_layer)}</td><td>{formatBoolean(x.retryable)}</td><td>{formatBoolean(x.fallback_allowed)}</td><td>{formatMissingValue(x.recommended_action)}</td></tr>)}</tbody></table></div>:<EmptyChart>{query?"没有匹配当前搜索关键词的错误记录。":"当前范围没有错误记录。"}</EmptyChart>}</section>
  <section className="interpretation"><h2>重试边界</h2><p>“可重试”不代表自动无限重试；所有请求仍受最大尝试次数、预算和停止规则约束。</p></section><TechnicalDataDrawer data={d}/></>
}

export function BugReportsPage(){
 const [requestId,setRequestId]=useState("");
 const [decisionId,setDecisionId]=useState("");
 const [model,setModel]=useState("");
 const [channel,setChannel]=useState("");
 const [httpStatus,setHttpStatus]=useState("");
 const [message,setMessage]=useState("");
 const [reports,setReports]=useState<BugReport[]>([]);
 const mutation=useMutation({
  mutationFn:()=>api.generateBugReport({
   request_id:requestId||undefined,decision_id:decisionId||undefined,model:model||undefined,
   channel:channel||undefined,http_status:httpStatus?Number(httpStatus):null,message:message||undefined,
  }),
  onSuccess:report=>setReports(prev=>[report,...prev.filter(x=>x.bug_id!==report.bug_id)]),
 });
 return <><Header title="Bug 报告" purpose="从已脱敏的代表性错误生成可复现报告，并保留证据引用。"/><DataSourceBanner genuine={false} label={reports.length?"本次会话生成":"当前未选择数据源"} sample={reports.length}/>
 <section className="ops-section"><header><div><h2>生成新报告</h2><p>填写代表性失败的字段（可从错误中心复制 Request ID、渠道与 HTTP 状态码），本页不会发起任何外部请求。</p></div></header>
  <div className="form-grid">
   <label>Request ID<input value={requestId} onChange={e=>setRequestId(e.target.value)}/></label>
   <label>Decision ID（可选，用于关联真实 fallback_trace）<input value={decisionId} onChange={e=>setDecisionId(e.target.value)}/></label>
   <label>模型<input value={model} onChange={e=>setModel(e.target.value)}/></label>
   <label>渠道<input value={channel} onChange={e=>setChannel(e.target.value)}/></label>
   <label>HTTP 状态码<input type="number" value={httpStatus} onChange={e=>setHttpStatus(e.target.value)}/></label>
   <label>脱敏错误信息<input value={message} onChange={e=>setMessage(e.target.value)}/></label>
  </div>
  <button onClick={()=>mutation.mutate()} disabled={mutation.isPending}>{mutation.isPending?"生成中…":"生成 Bug 报告"}</button>
  {mutation.error&&<div className="state error" role="alert">{mutation.error instanceof ApiError?mutation.error.message:"生成失败"}</div>}
 </section>
 <section className="ops-metrics"><MetricCard label="报告总数" value={String(reports.length)} context="本次会话生成" source="本地报告库"/><MetricCard label="可回退" value={String(reports.filter(r=>r.fallback_allowed).length)} context="允许切换到备用渠道" source="本地报告库"/></section>
 <section className="ops-section"><header><div><h2>报告列表</h2><p>从错误中心选择代表性失败后生成报告。</p></div></header><div className="table-wrap dense"><table><thead><tr><th>Bug ID</th><th>分类</th><th>层级</th><th>模型</th><th>渠道</th><th>发生时间</th><th>Fallback 证据</th><th>建议负责人</th><th>操作</th></tr></thead><tbody>
  {reports.length?reports.map(r=><tr key={r.bug_id}><td><code>{r.bug_id}</code></td><td>{formatErrorCategory(r.structured_classification.error_category as string)}</td><td>{labelEnum(String(r.structured_classification.error_layer))}</td><td>{r.model||"未填写"}</td><td>{r.channel||"未填写"}</td><td>{r.detected_time}</td><td>{r.fallback_trace.length?r.fallback_trace.join(" → "):labelEnum(r.fallback_evidence_status)}</td><td>{r.suggested_owner}</td><td><button className="link-button" onClick={()=>downloadText(`${r.bug_id}.md`,r.markdown,"text/markdown")}>下载 Markdown</button></td></tr>):
   <tr><td colSpan={9}>暂无 Bug 报告。请先在错误中心选择一条已脱敏记录，或在上方填写字段生成。</td></tr>}
 </tbody></table></div></section>
 <section className="interpretation"><h2>报告规则</h2><p>报告不会包含 API Key、Authorization、原始提示词或未经脱敏的提供方响应。</p></section></>
}

export function ModelMappingPage({mode}:{mode:DataMode}){
 const q=usePage("/api/v1/mappings",mode);if(q.isLoading)return <Loading/>;if(q.error)return <Failure retry={()=>q.refetch()}/>;
 const d=q.data!,items=(d.items||[]) as Record<string,unknown>[];
 return <><Header title="模型映射" purpose="核对请求模型、实际模型和映射证据，不用请求值填补缺失的实际模型。"/><Provenance mode={mode} source={d.source_type} sample={Number(d.sample_size||0)}/>
 <section className="ops-metrics"><MetricCard label="映射组合" value={String(items.length)} context="请求与实际组合" source={formatSourceType(d.source_type)}/><MetricCard label="不匹配" value={String(items.reduce((s,x)=>s+Number(x.mismatch_count||0),0))} context="有实际模型证据" source="映射证据"/><MetricCard label="实际模型待确认" value={String(items.reduce((s,x)=>s+Number(x.missing_actual_model_count||0),0))} context="不会自动回填" source="证据缺口"/></section>
 <section className="ops-section"><h2>模型映射明细</h2><div className="table-wrap dense"><table><thead><tr><th>渠道</th><th>请求模型</th><th>实际模型</th><th>匹配状态</th><th className="num">观测数</th><th className="num">不匹配</th><th>证据来源</th></tr></thead><tbody>{items.map((x,i)=><tr key={i}><td>{formatMissingValue(x.channel_id)}</td><td>{formatMissingValue(x.requested_model)}</td><td>{x.actual_model?String(x.actual_model):"实际模型待确认"}</td><td>{formatMappingStatus(x.mapping_status)}</td><td className="num">{formatMissingValue(x.observed_count)}</td><td className="num">{formatMissingValue(x.mismatch_count)}</td><td>{formatSourceType(x.evidence_source)}</td></tr>)}</tbody></table></div></section><TechnicalDataDrawer data={d}/></>
}

export function ReportsPage({mode}:{mode:DataMode}){
 const q=usePage("/api/v1/exports",mode);
 const qc=useQueryClient();
 const [generated,setGenerated]=useState<Record<string,{sha256:string;generatedAt:string;content:string;format:string}>>({});
 const [previewName,setPreviewName]=useState<string|null>(null);
 const [pending,setPending]=useState<string|null>(null);
 const mutation=useMutation({
  mutationFn:(name:string)=>{setPending(name);return api.generateExport(name,mode);},
  onSuccess:async result=>{setGenerated(prev=>({...prev,[result.name]:{sha256:result.sha256,generatedAt:result.generated_at,content:result.content,format:result.format}}));setPreviewName(result.name);await qc.invalidateQueries({queryKey:["/api/v1/exports",mode]});},
  onSettled:()=>setPending(null),
 });
 if(q.isLoading)return <Loading/>;if(q.error)return <Failure retry={()=>q.refetch()}/>;
 const d=q.data!,items=(d.items||[]) as Record<string,unknown>[];
 const renderPreview=(content:string)=>content.split("\n").filter(Boolean).slice(0,80).map((line,index)=>
  line.startsWith("# ")?<h2 key={index}>{line.slice(2)}</h2>:line.startsWith("## ")?<h3 key={index}>{line.slice(3)}</h3>:line.startsWith("- ")?<li key={index}>{line.slice(2)}</li>:line.startsWith("|")?<p key={index} className="report-table-line">{line.replaceAll("|"," · ")}</p>:<p key={index}>{line}</p>);
 return <><Header title="报告与导出" purpose="查看报告目录、证据来源和生成状态；导出内容保持脱敏。"/><Provenance mode={mode} source={d.source_type} sample={Number(d.sample_size||0)}/>
 {mutation.error instanceof ApiError&&<div className="state error" role="alert">{mutation.error.message}</div>}
 <section className="ops-section"><h2>报告目录</h2><div className="table-wrap dense"><table><thead><tr><th>报告名称</th><th>类型</th><th>数据来源</th><th>生成时间</th><th>样本范围</th><th>SHA-256</th><th>状态</th><th>操作</th></tr></thead><tbody>{items.map((x,i)=>{
  const name=String(x.name),format=String(x.format),entry=generated[name];
  return <tr key={i}><td>{formatMissingValue(name)}</td><td>{formatMissingValue(format)}</td><td>{formatSourceType(d.source_type)}</td>
   <td>{entry?entry.generatedAt:"按需生成"}</td><td>{d.sample_size as number} 条</td>
   <td>{entry?<code title={entry.sha256}>{entry.sha256.slice(0,12)}…</code>:"生成后提供"}</td>
   <td>{entry?"已生成":"可生成"}</td>
   <td><button className="link-button" disabled={pending===name} onClick={()=>mutation.mutate(name)}>{pending===name?"生成中…":entry?"重新生成":"生成报告"}</button>
    {entry&&<><button className="link-button" onClick={()=>setPreviewName(name)}>内部预览</button><button className="link-button" onClick={()=>downloadText(`${name}.md`,entry.content,"text/markdown")}>下载 Markdown</button></>}</td>
  </tr>;
 })}</tbody></table></div></section>
 {previewName&&generated[previewName]&&<section className="ops-section report-preview" aria-label="报告内部预览"><header><div><h2>{previewName} · 内部预览</h2><p>生成时间：{generated[previewName].generatedAt} · SHA-256：{generated[previewName].sha256.slice(0,16)}…</p></div><button className="secondary" onClick={()=>setPreviewName(null)}>关闭预览</button></header><article>{renderPreview(generated[previewName].content)}</article></section>}
 <section className="interpretation"><h2>导出说明</h2><p>下载文件不包含密钥、Authorization、原始提示词或未脱敏响应。</p></section><TechnicalDataDrawer data={d}/></>
}

export function SystemDescriptionPage(){
 return <><Header title="系统说明" purpose="说明 Routing Quality Console 的用途、数据边界和本地启动方式。"/><section className="documentation-grid"><article><h2>系统用途</h2><p>面向 QA、后端工程师、渠道运营、产品经理和管理员，统一查看路由质量、UAT 证据、错误与成本。</p></article><article><h2>架构</h2><ol><li>React/Vite 展示结构化证据</li><li>本地 FastAPI 执行校验与聚合</li><li>SQLite 保存导入批次和 UAT 证据</li><li>外部执行始终经过 ExecutionGuard</li></ol></article><article><h2>数据流</h2><p>文件或受控 UAT 响应 → 后端验证与脱敏 → SQLite 证据 → 页面指标与表格。</p></article><article><h2>Demo 与 UAT</h2><p>演示数据只说明界面流程；真实 UAT 页面只使用已导入或受控执行形成的证据，两者默认不混合。</p></article><article><h2>安全边界</h2><p>浏览器不直接调用 Weimeta。密钥不写入数据库、文件、URL、Web Storage 或审计载荷。</p></article><article><h2>本地启动</h2><ol><li>在项目目录启动 FastAPI</li><li>在 web 目录运行 Vite</li><li>打开本地页面并选择数据来源</li></ol></article><article><h2>限制</h2><p>小样本不代表长期可靠性；离线回放不是实际收益；连接测试不证明聊天执行权限。</p></article><article><h2>证据政策</h2><p>原始证据不可静默改写。解析修正通过带 SHA-256 的 amendment 记录。</p></article></section></>
}

export function VisualTestPage(){
 const labels=["演示数据","真实 UAT 数据","预算正常","接近预算上限","等待后台日志","精确匹配","样本不足","数据已过期","实际模型待确认","流式能力待确认"];
 return <><Header title="中文视觉测试" purpose="开发环境字体、换行、状态、数值和缺失值检查。"/><section className="ops-section"><h2>状态与字符</h2><div className="visual-label-grid">{labels.map(x=><span key={x}>{x}</span>)}</div><p>玫瑰-多模型测试 · 青绿四号 · 北京云雀未来-Deepseek · 阿里云ADB-DeepSeek-Test · Test-Duan-DeepSeek测试 · 水循环 · 渠道健康 · 成本与预算</p></section><section className="ops-section"><h2>长文本与数值</h2><div className="table-wrap dense"><table><thead><tr><th>长渠道名</th><th>缺失值</th><th>金额</th><th>时间</th><th>警告</th></tr></thead><tbody><tr><td title="这是一个用于测试换行和截断的超长中文渠道名称">这是一个用于测试换行和截断的超长中文渠道名称</td><td>待确认 / 暂无数据 / 不适用</td><td>¥0.1150</td><td>2026-07-27 15:00:00</td><td>接近预算上限</td></tr></tbody></table></div></section></>
}
