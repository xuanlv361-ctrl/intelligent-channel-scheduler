import {useEffect,useMemo,useRef,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {Link} from "react-router-dom";
import {api,ApiError,DataMode,EnvironmentId,StandardizedCallLog,UatModel,UatRequestBody} from "../services/api";
import {EmptyState,ErrorState,MetricCard,PageHeader,Section,StatusBadge,formatLocalTime,formatValue} from "../components/console/ConsoleUI";
import {UatWorkbenchPage} from "./UatWorkbenchPage";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

const errorMessage=(error:unknown)=>error instanceof ApiError?error.message:error instanceof Error?error.message:"请求失败";

const humanStatus=(value:unknown)=>{
  const key=String(value??"").toLowerCase();
  const labels:Record<string,string>={
    ready:"可用",success:"成功",failed:"失败",error:"失败",healthy:"健康",degraded:"降级",
    blocked:"已阻断",draft:"草稿",approved:"已批准",active:"执行中",stopped:"已停止",
    expired:"已过期",budget_exhausted:"预算已用尽",killed:"已终止",pending_confirmation:"待确认",
    capability_pending:"当前模型或渠道的能力信息待确认，暂不能执行真实请求。",
    capability_pending_confirmation:"当前模型或渠道的能力信息待确认，暂不能执行真实请求。",
    real_execution_disabled:"UAT 环境当前已关闭，请打开 UAT 环境后再执行。",
  };
  return labels[key]||formatValue(value);
};
const toneFor=(value:unknown):"success"|"warning"|"danger"|"neutral"|"info"=>{
  const key=String(value??"").toLowerCase();
  if(["ready","success","healthy","active","confirmed"].includes(key))return "success";
  if(["failed","error","blocked","killed","budget_exhausted","unhealthy"].includes(key))return "danger";
  if(["pending_confirmation","capability_pending","degraded","expired","warning"].includes(key))return "warning";
  if(["approved","running"].includes(key))return "info";
  return "neutral";
};

export function DashboardPage({mode}:{mode:DataMode}){
  const [timeRange,setTimeRange]=useState("all");
  const overview=useQuery({queryKey:["console-overview",mode,timeRange],
    queryFn:({signal})=>api.overview(mode,signal,timeRange),refetchInterval:7000});
  if(overview.error)return <ErrorState title="总览数据读取失败" description={(overview.error as Error).message} onRetry={()=>overview.refetch()}/>;
  const data=overview.data;
  const metrics=data?.metrics;
  const channels=data?.channel_summary??[];
  const recent=data?.recent_executions??[];
  const totalTokens=metrics?metrics.input_tokens+metrics.output_tokens:null;
  const hasRealData=mode==="uat"&&data?.is_mock===false;
  const coverage=metrics?.measurement_coverage;
  const trend=metrics?.trend??[];
  const maximumRequests=Math.max(1,...trend.map(item=>item.request_count));
  const cost=metrics?.total_cost;
  return <>
    <PageHeader title="总览" description="从标准化调用日志还原当前 UAT 运行状态、成本与调度证据。" actions={<><button className="secondary" onClick={()=>overview.refetch()} disabled={overview.isFetching}>{overview.isFetching?"刷新中":"刷新数据"}</button><Link className="button-link" to="/routing/execute">发起调度</Link></>}/>
    <BusinessSkillAction skillId="analyze-model-cost" label="分析 Token 与费用" argumentsValue={{environment_id:"china_uat",group_by:"model"}}/>
    <div className="overview-filter-bar">
      <label>统计范围<select aria-label="总览统计范围" value={timeRange} onChange={event=>setTimeRange(event.target.value)}><option value="24h">最近 24 小时</option><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option><option value="all">全部已载入证据</option></select></label>
      <span>环境：<b>国内 UAT</b></span><span>数据模式：<StatusBadge tone={hasRealData?"success":"warning"}>{hasRealData?"真实数据":"Mock 数据"}</StatusBadge></span><span>数据截至：{formatLocalTime(data?.data_as_of)}</span>
    </div>
    {mode==="demo"&&<div className="overview-mock-warning" role="status"><b>当前为 Mock 数据</b><span>仅用于界面演示，不代表 UAT 连接、日志、渠道或费用状态。</span></div>}
    {data?.runtime&&<section className="overview-runtime-strip" aria-label="运行状态">
      <div><span>UAT 环境</span><StatusBadge tone={data.runtime.environment_enabled?"success":"danger"}>{data.runtime.environment_enabled?"已开启":"已关闭"}</StatusBadge></div>
      <div><span>API Key</span><StatusBadge tone={data.runtime.credential_configured?"success":"danger"}>{data.runtime.credential_configured?"已配置":"未配置"}</StatusBadge></div>
      <div><span>连接测试</span><StatusBadge tone={data.runtime.connection_status==="success"?"success":"warning"}>{data.runtime.connection_status==="success"?"已通过":"未通过"}</StatusBadge></div>
      <div><span>日志数据</span><StatusBadge tone={data.runtime.log_sync_status==="synchronized"?"success":"warning"}>{data.runtime.log_sync_status==="synchronized"?"已同步":"从未同步"}</StatusBadge></div>
      <div><span>最近同步</span><b>{formatLocalTime(data.last_synced_at)}</b></div>
      <div><span>新鲜度</span><b>{data.runtime.freshness_status==="available"?"实时证据可用":data.runtime.freshness_status==="historical_only"?"仅历史证据":"暂无数据"}</b></div>
    </section>}
    <div className="metric-grid overview-metrics">
      <MetricCard label="业务请求" value={data?data.records.toLocaleString("zh-CN"):"读取中"} meta="按父请求计数，不把重试 attempt 重复计入"/>
      <MetricCard label="成功率" value={metrics?.success_rate===null||metrics?.success_rate===undefined?"未提供":`${(metrics.success_rate*100).toFixed(1)}%`} meta={metrics?`${metrics.success_count} 成功 / ${metrics.failure_count} 失败`:"等待日志"}/>
      <MetricCard label="P95 总延迟" value={metrics?.p95_latency_ms===null||metrics?.p95_latency_ms===undefined?"未提供":`${Math.round(metrics.p95_latency_ms)} ms`} meta={coverage?`${coverage.latency} 条含延迟证据`:"等待日志"}/>
      <MetricCard label="Token 总量" value={totalTokens===null?"未提供":totalTokens.toLocaleString("zh-CN")} meta={coverage?`${coverage.tokens} 条含 Token 证据`:"缺失值不按 0 伪造"}/>
      <MetricCard label="观测费用" value={cost===null||cost===undefined?"未提供":`CNY ¥${Number(cost).toFixed(6)}`} meta={coverage?`${coverage.cost} 条含费用证据`:"缺失费用不按 ¥0 展示"}/>
      <MetricCard label="Fallback / 重试" value={metrics?`${metrics.fallback_count} / ${metrics.retry_count}`:"未提供"} meta="来自实际 total_attempts"/>
    </div>
    <div className="split-layout overview-risk-grid">
      <Section title="当前执行阻断" description="只列出当前执行条件；历史失败不会自动成为阻断。">
        {!data?.blockers?.length?<p className="positive-line">当前没有总览可识别的执行阻断。</p>:<ul className="issue-list">{data.blockers.map(item=><li key={item.code}><b>{item.message}</b> <Link className="text-link" to={item.action_path}>处理</Link></li>)}</ul>}
      </Section>
      <Section title="历史失败观测" description="用于分析错误，不代表当前环境一定不可执行。">
        <MetricCard label="历史失败记录" value={data?.historical_failures?.count??data?.errors??"读取中"} meta="分类：历史观测 · 不阻断当前执行"/>
      </Section>
    </div>
    <Section title="调用趋势" description={`真实调用账本按中国时区日期聚合；聚合时间 ${formatLocalTime(metrics?.generated_at)}。`}>
      {!trend.length?<EmptyState title="当前范围没有趋势数据" description="不会回退到演示曲线；调整时间范围或等待真实日志写入。"/>:<div className="overview-trends">{trend.map(item=><div className="overview-trend-row" key={item.date}><time>{item.date}</time><span className="trend-track"><i style={{width:`${Math.max(3,item.request_count/maximumRequests*100)}%`}}/></span><b>{item.request_count} 次</b><small>成功率 {item.success_rate===null?"未提供":`${(item.success_rate*100).toFixed(1)}%`} · P均 {item.average_latency_ms===null?"未提供":`${Math.round(item.average_latency_ms)} ms`} · 费用 {item.total_cost===null?"未提供":`${item.currency??"CNY"} ¥${Number(item.total_cost).toFixed(6)}`}</small></div>)}</div>}
    </Section>
    <Section title="真实渠道分布" description={data?.channel_evidence_note||"只统计具备权威渠道标识的实时执行日志。"}>
      {!channels.length?<EmptyState title="暂无可归属的真实渠道记录" description="当前历史 CSV 没有 channel_id，因此不会生成虚构渠道，也不会形成渠道健康结论。"/>:<div className="channel-distribution">{channels.map(item=><div key={item.channel_id}><header><b>{item.channel_name||item.channel_id}</b><span>{item.request_count} 次 · 占比 {item.share===null?"未提供":`${(item.share*100).toFixed(1)}%`} · 成功率 {(item.success_rate*100).toFixed(1)}%</span></header><div><i style={{width:`${Math.max(2,(item.share??0)*100)}%`}}/></div><small>渠道 ID：{item.channel_id} · 最近观测 {formatLocalTime(item.last_observation)}</small></div>)}</div>}
    </Section>
    <Section title="最近执行" description="最近 10 条标准化历史或实时调用；标识缺失时如实显示未提供。" actions={<Link className="text-link" to="/routing/runs">查看全部</Link>}>
      {!recent.length?<EmptyState title="暂无执行记录" description="新调用完成后会在 7 秒内自动刷新；页面刷新不会丢失记录。"/>:<div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>来源</th><th>Request ID</th><th>Decision ID</th><th>模型</th><th>渠道</th><th>状态</th><th>延迟</th><th>Token</th><th>费用</th></tr></thead><tbody>{recent.slice(0,10).map(row=><tr key={row.record_id}><td>{formatLocalTime(row.occurred_at)}</td><td>{row.is_historical?"历史 UAT":"实时执行"}</td><td>{formatValue(row.request_id,"历史数据未提供")}</td><td>{row.decision_id?<Link to={`/routing/decisions/${encodeURIComponent(row.decision_id)}`}>{row.decision_id}</Link>:"未提供"}</td><td>{row.actual_model&&row.actual_model!==row.requested_model?`${row.requested_model} → ${row.actual_model}`:row.requested_model}</td><td>{formatValue(row.channel_name||row.channel_id,"历史数据未提供")}</td><td><StatusBadge tone={toneFor(row.request_status)}>{humanStatus(row.request_status)}</StatusBadge></td><td>{row.total_latency_ms===null?"未提供":`${Math.round(row.total_latency_ms)} ms`}</td><td>{[row.input_tokens,row.output_tokens].every(value=>value===null)?"未提供":((row.input_tokens??0)+(row.output_tokens??0)).toLocaleString("zh-CN")}</td><td>{row.cost_amount===null?(row.is_historical?"未提供":"待 Provider 日志同步"):`${row.currency||"CNY"} ¥${row.cost_amount}`}</td></tr>)}</tbody></table></div>}
    </Section>
  </>;
}

type RequestKind="text"|"image"|"audio"|"video";
/** @deprecated Kept temporarily for compatibility with older embedded imports. */
export function RoutingExecutePageLegacy({environment,onEnvironmentChange}:{environment:EnvironmentId;onEnvironmentChange:(value:EnvironmentId)=>void}){
  const qc=useQueryClient();
  const status=useQuery({queryKey:["routing-execute-status",environment],queryFn:({signal})=>api.environmentStatus(environment,signal)});
  const credential=useQuery({queryKey:["routing-execute-credential",environment],queryFn:({signal})=>api.environmentCredentialStatus(environment,signal)});
  const catalog=useQuery({queryKey:["routing-execute-models",environment],queryFn:({signal})=>api.environmentModels(environment,false,signal)});
  const options=useQuery({queryKey:["routing-execute-options"],queryFn:({signal})=>api.uatEditorOptions(signal)});
  const control=useQuery({queryKey:["routing-execute-control"],queryFn:({signal})=>api.uatExecutionControl(signal)});
  const environmentState=useQuery({queryKey:["routing-execute-environment-state",environment],queryFn:({signal})=>api.environmentExecutionState(environment,signal),enabled:environment==="china_uat"});
  const connectionTest=useMutation({mutationFn:api.testCredential});
  const environmentSwitch=useMutation({
    mutationFn:(enabled:boolean)=>api.setEnvironmentExecutionState(environment,enabled),
    onSuccess:data=>{
      qc.setQueryData(["routing-execute-environment-state",environment],data);
      qc.invalidateQueries({queryKey:["routing-execute-status",environment]});
      qc.invalidateQueries({queryKey:["environment-status",environment]});
      connectionTest.reset();
      if(data.enabled)connectionTest.mutate();
    },
  });
  const [requestKind,setRequestKind]=useState<RequestKind>("text");
  const [selectionMode,setSelectionMode]=useState<"model"|"capability">("model");
  const [model,setModel]=useState("");
  const [modelSearch,setModelSearch]=useState("");
  const [stream,setStream]=useState(false);
  const [maxTokens,setMaxTokens]=useState(2048);
  const [prompt,setPrompt]=useState("请用简洁中文回答，并给出可核验的结论。\n");
  const [mediaFile,setMediaFile]=useState<File|null>(null);
  const [mediaChannel,setMediaChannel]=useState("");
  const [mediaInstruction,setMediaInstruction]=useState("");
  const [strategy,setStrategy]=useState("latency_first");
  const [previewOpen,setPreviewOpen]=useState(false);
  const [detailOpen,setDetailOpen]=useState(false);
  const [confirmed,setConfirmed]=useState(false);
  useEffect(()=>{
    if(environment==="china_uat"&&environmentState.data?.enabled&&credential.data?.configured&&connectionTest.isIdle)connectionTest.mutate();
  },[environment,environmentState.data?.enabled,credential.data?.configured,connectionTest]);
  const models=useMemo(()=>{
    const query=modelSearch.trim().toLowerCase();
    return (catalog.data?.models??[]).filter(item=>!query||[item.id,item.display_name,item.owned_by].some(value=>String(value??"").toLowerCase().includes(query)));
  },[catalog.data,modelSearch]);
  const selected=catalog.data?.models.find(item=>item.id===model);
  const capability=options.data?.model_output_capabilities.find(item=>item.model_id===model);
  const limit=[capability?.confirmed_max_output_tokens,capability?.confirmed_channel_max_output_tokens,capability?.max_context_tokens].filter((item):item is number=>typeof item==="number"&&item>0);
  const effectiveLimit=limit.length===3?Math.min(...limit):null;
  const blockers:string[]=[];
  if(status.data&&!status.data.key_configured)blockers.push("API 密钥尚未配置");
  if(environment==="china_uat"&&environmentState.data&&!environmentState.data.enabled)blockers.push("UAT 环境当前已关闭，请打开 UAT 环境后再执行");
  if(environment==="china_uat"&&environmentState.data?.enabled&&connectionTest.data?.connection_status!=="success")blockers.push(connectionTest.isPending?"正在测试 UAT 连接":"UAT 连接尚未测试成功");
  if(control.data?.status!=="ACTIVE")blockers.push("受控执行任务尚未激活");
  if(selectionMode==="model"&&!model)blockers.push("尚未选择模型");
  if(selectionMode==="capability")blockers.push("按能力自动选择的两阶段调度接口尚未接入");
  if(requestKind!=="text")blockers.push(`${{image:"图片",audio:"音频",video:"视频"}[requestKind]}请求需先通过能力证据校验，真实 Provider 执行适配仍受现有门禁限制`);
  if(model&&effectiveLimit===null)blockers.push("当前模型或渠道的能力信息待确认，暂不能执行真实请求");
  if(effectiveLimit!==null&&maxTokens>effectiveLimit)blockers.push(`最大输出不能超过已确认上限 ${effectiveLimit.toLocaleString("zh-CN")}`);
  if(stream&&!options.data?.stream_execution_ready)blockers.push("流式真实执行尚未开放");
  const body:UatRequestBody={environment_id:environment,mode:"real_uat_execute",confirmation:{confirmed,confirmation_text:confirmed?"I understand this will call the Weimeta UAT API and may incur UAT cost.":""},request:{requested_model:model,channel_id:"unified-routing",messages:[{role:"user",content:prompt}],stream,max_tokens:maxTokens},measurement:{plan_id:`UI-${Date.now()}`,request_profile_id:"CUSTOM",session_id:"LOCAL-CONSOLE"},shadow:{run_before_execution:true,strategy}};
  const validation=useMutation({mutationFn:()=>api.validateEnvironmentRequest(environment,body)});
  const execution=useMutation({mutationFn:()=>api.executeEnvironmentRequest(environment,body),onSuccess:()=>{setConfirmed(false);qc.invalidateQueries({queryKey:["console-runs"]});}});
  const multimodalValidation=useMutation({mutationFn:()=>{
    if(!mediaFile||!model||!mediaChannel.trim())throw new Error("请选择文件、模型并填写真实渠道 ID");
    return api.validateMultimodal({environment_id:environment,model_id:model,
      channel_id:mediaChannel.trim(),subject_version:"1",request_type:requestKind as "image"|"audio"|"video",
      mime_type:mediaFile.type||"application/octet-stream",input_bytes:mediaFile.size});
  }});
  const estimated=validation.data?.cost_estimate.estimated_cost_cny;
  return <div className="execute-page">
    <PageHeader title="发起调度" description="根据能力、指标和策略选择渠道，并在受控环境中执行请求。" actions={<><Link className="secondary button-link" to="/routing/runs">查看执行记录</Link><Link className="secondary button-link" to="/settings/environments">环境详情</Link></>}/>
    <div className="execution-status-bar"><label className="status-environment"><span>环境</span><select value={environment} onChange={event=>{setModel("");connectionTest.reset();onEnvironmentChange(event.target.value as EnvironmentId)}}><option value="china_uat">国内 UAT</option><option value="overseas">海外环境</option></select></label>{environment==="china_uat"?(environmentState.data?<><label className="uat-environment-toggle"><span>UAT 环境</span><input aria-label="UAT 环境开关" type="checkbox" role="switch" checked={environmentState.data.enabled} disabled={environmentSwitch.isPending} onChange={event=>environmentSwitch.mutate(event.target.checked)}/><b>{environmentSwitch.isPending?"保存中…":environmentState.data.enabled?"开启":"关闭"}</b></label><span className={environmentState.data.enabled?"environment-state-on":"environment-state-off"}>{environmentState.data.enabled?"UAT 环境已开启，可以发送真实 UAT 请求。":"UAT 环境已关闭，不能发送真实 UAT 请求。"}</span></>:<span role="status">正在读取 UAT 环境状态…</span>):<StatusBadge tone="warning">海外执行待确认</StatusBadge>}<span>接口检测：{connectionTest.isPending?"测试中":connectionTest.data?.connection_status==="success"?"成功":connectionTest.isError||connectionTest.data?"失败":"未测试"}</span><span>请求执行条件：{blockers.length?"存在阻断":"可执行"}</span><span>受控任务：{humanStatus(control.data?.status)}</span><button className="secondary compact" onClick={()=>setDetailOpen(true)}>查看详情</button></div>
    {environmentSwitch.error&&<ErrorState title="UAT 环境状态保存失败" description={environmentSwitch.error instanceof ApiError?environmentSwitch.error.message:"保存失败，已恢复原状态。"}/>} {connectionTest.error&&<ErrorState title="UAT 连接测试失败" description={`${errorMessage(connectionTest.error)}；环境开关保持开启，但真实执行继续禁用。`}/>} 
    {blockers.length>0&&<div className="primary-blocker" role="alert"><div><b>当前不能执行真实请求：{blockers[0]}。</b>{blockers.length>1&&<span>另有 {blockers.length-1} 项需要处理</span>}</div><div><Link to={blockers[0].includes("密钥")?"/settings/security":"/data/capabilities"}>解决问题</Link><button className="secondary" onClick={()=>validation.mutate()} disabled={!model||validation.isPending}>仅验证请求</button></div></div>}
    <Section title="配置请求" description="先选择请求类型，再填写当前类型所需内容。" className="request-config">
      <div className="segmented" role="tablist">{(["text","image","audio","video"] as RequestKind[]).map(kind=><button key={kind} role="tab" aria-selected={requestKind===kind} className={requestKind===kind?"active":""} onClick={()=>setRequestKind(kind)}>{{text:"文本",image:"图片",audio:"音频",video:"视频"}[kind]}</button>)}</div>
      <div className="selection-mode"><span>选择方式</span><label><input type="radio" checked={selectionMode==="model"} onChange={()=>setSelectionMode("model")}/>指定模型</label><label><input type="radio" checked={selectionMode==="capability"} onChange={()=>setSelectionMode("capability")}/>按能力自动选择</label></div>
      {selectionMode==="model"?<div className="model-combobox"><label>模型目录<input value={modelSearch} onChange={event=>setModelSearch(event.target.value)} placeholder="搜索模型 ID、名称或供应商"/></label><div className="model-option-list" role="listbox" aria-label="真实模型目录">{catalog.isLoading?<p>正在读取 UAT 模型目录…</p>:models.length?models.slice(0,60).map(item=><button type="button" role="option" aria-selected={model===item.id} className={model===item.id?"selected":""} key={item.id} onClick={()=>setModel(item.id)}><b>{item.display_name}</b><span>{item.owned_by||"供应商未提供"} · OpenAI 兼容 · {item.execution_allowed?"目录可用":"不可执行"}</span><small>渠道关系：待后端确认 · 流式能力：待确认</small></button>):<EmptyState title="当前目录没有匹配模型" description={catalog.data?.error?.message||"当前 Key 未返回可用模型。"}/>}</div>{selected&&<div className="selected-model"><b>{selected.display_name}</b><span>{selected.owned_by||"供应商未提供"} · 模型 ID：{selected.id}</span></div>}</div>:<div className="capability-builder"><p>能力自动选择会先过滤模型，再在合格模型的真实渠道中进行调度。</p><div className="inline-fields"><label>请求能力<select><option>{requestKind==="text"?"文本生成":`${{image:"图片",audio:"音频",video:"视频"}[requestKind]}处理`}</option></select></label><label>上下文要求<input type="number" min="1" placeholder="Token"/></label><label>协议<select><option>OpenAI 兼容</option><option>不限</option></select></label></div><StatusBadge tone="warning">后端两阶段决策接口尚未提供，仅可保存本地表单状态</StatusBadge></div>}
      {requestKind==="text"?<div className="form-grid clean"><label>响应方式<div className="segmented small"><button className={!stream?"active":""} onClick={()=>setStream(false)}>非流式</button><button className={stream?"active":""} disabled={!options.data?.stream_execution_ready} onClick={()=>setStream(true)}>流式</button></div></label><label>最大输出 Token<input type="number" min="1" value={maxTokens} onChange={event=>setMaxTokens(Math.max(0,Number(event.target.value)))}/><div className="token-presets">{[2048,4096,8192].map(value=><button type="button" key={value} className="secondary compact" onClick={()=>setMaxTokens(value)}>{value}</button>)}</div>{effectiveLimit!==null&&maxTokens>effectiveLimit&&<small className="field-error">超过已确认上限 {effectiveLimit.toLocaleString("zh-CN")}</small>}</label><label className="wide">请求内容<textarea value={prompt} onChange={event=>setPrompt(event.target.value)} rows={6}/><span className="field-meta">{prompt.length.toLocaleString("zh-CN")} 字符 · 预计约 {Math.ceil(prompt.length/2).toLocaleString("zh-CN")} Token <button type="button" className="text-button" onClick={()=>setPrompt("")}>清空</button></span></label></div>:<div className="multimodal-contract"><div className="form-grid clean"><label>{requestKind==="image"?"图片文件":requestKind==="audio"?"音频文件":"视频文件"}<input type="file" accept={requestKind==="image"?"image/jpeg,image/png,image/webp":requestKind==="audio"?"audio/mpeg,audio/wav,audio/ogg":"video/mp4,video/webm,video/quicktime"} onChange={event=>{setMediaFile(event.target.files?.[0]??null);multimodalValidation.reset()}}/><small>{mediaFile?`${mediaFile.type||"未知 MIME"} · ${(mediaFile.size/1024).toFixed(1)} KB`:"文件内容不会由本地校验接口持久化"}</small></label><label>真实渠道 ID<input value={mediaChannel} onChange={event=>{setMediaChannel(event.target.value);multimodalValidation.reset()}} placeholder="由模型—渠道关系提供，不使用端点名代替"/></label><label className="wide">文本指令<textarea rows={4} value={mediaInstruction} onChange={event=>setMediaInstruction(event.target.value)} placeholder="说明需要对媒体执行的任务"/></label></div><div className="section-footer"><Link className="text-link" to="/data/capabilities">查看能力矩阵</Link><button type="button" disabled={!mediaFile||!model||!mediaChannel.trim()||multimodalValidation.isPending} onClick={()=>multimodalValidation.mutate()}>校验多模态能力</button></div>{multimodalValidation.error&&<ErrorState title="能力校验未通过" description={multimodalValidation.error instanceof Error?multimodalValidation.error.message:"多模态能力待确认"}/>} {multimodalValidation.data&&<div className="preview-summary"><StatusBadge tone={multimodalValidation.data.allowed?"success":"warning"}>{multimodalValidation.data.allowed?"能力证据已确认":"能力证据不足"}</StatusBadge><span>校验 ID：{multimodalValidation.data.validation_id}</span><span>媒体未落盘 · 外部调用 0</span></div>}</div>}
    </Section>
    <Section title="路由策略" description="默认只显示影响本次决策的核心约束。"><div className="inline-fields"><label>策略<select value={strategy} onChange={event=>setStrategy(event.target.value)}><option value="latency_first">延迟优先</option><option value="cost_first">成本优先</option><option value="confidence_aware_v2">置信度感知</option></select></label><label>数据来源<select><option>动态指标</option><option>固定快照</option></select></label><label>最大尝试次数<input value="2" readOnly/></label><label>最大预算<input value="CNY ¥3.00" readOnly/></label></div><details><summary>高级约束</summary><p>黏性窗口、受控探索、指标新鲜度与熔断过滤仍由现有调度策略服务端强制执行。</p></details></Section>
    <Section title="执行条件检查" description="仅警告和未通过项默认展开。">{blockers.length?<ul className="check-list">{blockers.map(item=><li key={item}><span aria-hidden="true">!</span><b>{item}</b><Link to={item.includes("密钥")?"/settings/security":"/data/capabilities"}>处理</Link></li>)}</ul>:<p className="positive-line">全部执行条件已通过。</p>}</Section>
    <Section title="候选与决策预览" description="真实渠道关系必须由后端证据提供，端点不会被当作渠道。"><EmptyState title="尚未生成候选预览" description="点击“仅验证请求”后显示资格过滤与费用估算；当前目录只确认模型和协议端点，真实渠道关系待后端确认。" action={<button className="secondary" onClick={()=>validation.mutate()} disabled={!model||validation.isPending}>仅验证请求</button>}/>{validation.data&&<div className="preview-summary"><StatusBadge tone={validation.data.valid?"success":"danger"}>{validation.data.valid?"验证通过":"验证未通过"}</StatusBadge><span>预计费用：{estimated===undefined?"无法估算":`CNY ¥${estimated.toFixed(4)}`}</span><span>Guard：{humanStatus(validation.data.guard_status)}</span></div>}</Section>
    <Section title="请求预览" description={`${environment} · ${model||"未选择模型"} · ${stream?"流式":"非流式"} · max_tokens ${maxTokens}`} actions={<button className="secondary compact" onClick={()=>setPreviewOpen(value=>!value)}>{previewOpen?"收起":"展开"}</button>}>{previewOpen&&<pre className="safe-preview">{JSON.stringify({environment_id:environment,endpoint:"/v1/chat/completions",model:model||null,request_type:requestKind,stream,max_tokens:maxTokens,routing_policy:strategy,messages:"[已脱敏请求内容]"},null,2)}</pre>}</Section>
    <div className="execution-action-bar"><div><b>{estimated===undefined?"费用待验证":`预计最多 CNY ¥${estimated.toFixed(4)}`}</b><span>最多尝试 2 次 · {blockers.length?`阻断：${blockers[0]}`:"当前可执行"}</span></div><div><button className="secondary">保存草稿</button><button className="secondary" onClick={()=>validation.mutate()} disabled={!model}>仅模拟决策</button><button className="secondary" onClick={()=>validation.mutate()} disabled={!model}>验证请求</button><label className="execute-confirm"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>确认真实费用</label><button onClick={()=>execution.mutate()} disabled={blockers.length>0||!confirmed||execution.isPending}>执行真实请求</button></div></div>
    {execution.error&&<ErrorState title="真实请求未执行" description={execution.error instanceof ApiError?execution.error.message:"执行请求失败。"}/>} {execution.data&&<Section title="执行结果" description="结果已写入现有执行记录和审计链。"><div className="preview-summary"><StatusBadge tone={toneFor(execution.data.execution?.status)}>{humanStatus(execution.data.execution?.status)}</StatusBadge><span>Execution ID：{formatValue(execution.data.execution?.execution_id)}</span><span>Decision ID：{formatValue(execution.data.execution?.decision_id)}</span></div></Section>}
    {detailOpen&&<div className="drawer-backdrop" onClick={()=>setDetailOpen(false)}><aside className="console-drawer" aria-label="环境详情" onClick={event=>event.stopPropagation()}><header><div><p>环境详情</p><h2>{environment==="china_uat"?"国内 UAT":"海外环境"}</h2></div><button aria-label="关闭" onClick={()=>setDetailOpen(false)}>×</button></header><dl><dt>环境 ID</dt><dd>{environment}</dd><dt>API 地址</dt><dd>{formatValue(status.data?.base_url)}</dd><dt>UAT 环境</dt><dd>{environmentState.data?environmentState.data.enabled?"已开启":"已关闭":"未读取"}</dd><dt>接口检测</dt><dd>{connectionTest.isPending?"测试中":connectionTest.data?.connection_status==="success"?"成功":connectionTest.isError||connectionTest.data?"失败":"未测试"}</dd><dt>请求执行条件</dt><dd>{blockers.length?"存在阻断":"可执行"}</dd><dt>受控任务</dt><dd>{humanStatus(control.data?.status)}</dd><dt>密钥来源</dt><dd>{credential.data?.credential_source==="windows_encrypted_vault"?"Windows 加密保险库":humanStatus(credential.data?.credential_source)}</dd><dt>密钥指纹</dt><dd>{formatValue(credential.data?.key_fingerprint)}</dd><dt>今日请求</dt><dd>{formatValue(status.data?.requests_used_today)}</dd><dt>今日费用</dt><dd>{status.data?.estimated_cost_used_today===undefined?"未提供":`CNY ¥${status.data.estimated_cost_used_today.toFixed(4)}`}</dd><dt>流式能力</dt><dd>{options.data?.stream_execution_ready?"已确认":"待确认"}</dd></dl><details><summary>技术字段</summary><pre>{JSON.stringify({execution_ready:status.data?.execution_ready,blocking_reasons:status.data?.blocking_reasons,credential_source:credential.data?.credential_source},null,2)}</pre></details><footer><Link className="button-link" to="/settings/security">管理密钥与权限</Link></footer></aside></div>}
  </div>;
}

const MOCK_MODELS:UatModel[]=[
  {id:"mock-text-standard",display_name:"Mock 文本模型",owned_by:"本地模拟器",execution_allowed:true,stream_capability:"pending_confirmation"},
  {id:"mock-text-economy",display_name:"Mock 经济模型",owned_by:"本地模拟器",execution_allowed:true,stream_capability:"pending_confirmation"},
];

export function RoutingExecutePageV2Legacy({mode,environment,onEnvironmentChange}:{mode:DataMode;environment:EnvironmentId;onEnvironmentChange:(value:EnvironmentId)=>void}){
  void onEnvironmentChange;
  const qc=useQueryClient();
  const realMode=mode==="uat";
  const keyInputRef=useRef<HTMLInputElement>(null);
  const [showKey,setShowKey]=useState(false);
  const [showKeyValue,setShowKeyValue]=useState(false);
  const [apiKey,setApiKey]=useState("");
  const [requestKind,setRequestKind]=useState<RequestKind>("text");
  const [model,setModel]=useState("");
  const [modelSearch,setModelSearch]=useState("");
  const [stream,setStream]=useState(false);
  const [maxTokens,setMaxTokens]=useState("");
  const [prompt,setPrompt]=useState("");
  const [strategy,setStrategy]=useState("latency_first");
  const [metricSource,setMetricSource]=useState("");
  const [maxAttempts,setMaxAttempts]=useState("");
  const [budget,setBudget]=useState("");
  const [previewOpen,setPreviewOpen]=useState(false);
  const [detailOpen,setDetailOpen]=useState(false);
  const [confirmed,setConfirmed]=useState(false);
  const [mediaFile,setMediaFile]=useState<File|null>(null);
  const [mediaChannel,setMediaChannel]=useState("");
  const [mediaInstruction,setMediaInstruction]=useState("");

  const status=useQuery({queryKey:["routing-execute-status",environment],queryFn:({signal})=>api.environmentStatus(environment,signal),enabled:realMode});
  const credential=useQuery({queryKey:["routing-execute-credential",environment],queryFn:({signal})=>api.environmentCredentialStatus(environment,signal),enabled:realMode});
  const environmentState=useQuery({queryKey:["routing-execute-environment-state",environment],queryFn:({signal})=>api.environmentExecutionState(environment,signal),enabled:realMode&&environment==="china_uat"});
  const options=useQuery({queryKey:["routing-execute-options"],queryFn:({signal})=>api.uatEditorOptions(signal),enabled:realMode});
  const control=useQuery({queryKey:["routing-execute-control"],queryFn:({signal})=>api.uatExecutionControl(signal),enabled:realMode});
  const connectionTest=useMutation({mutationFn:api.testCredential,onSuccess:data=>{if(data.connection_status==="success")qc.invalidateQueries({queryKey:["routing-execute-models",environment]});}});
  const connectionReady=connectionTest.data?.connection_status==="success"||(realMode&&environmentState.data?.enabled===false);
  const catalog=useQuery({queryKey:["routing-execute-models",environment],queryFn:({signal})=>api.environmentModels(environment,false,signal),enabled:realMode});
  const saveCredential=useMutation({
    mutationFn:()=>api.setEnvironmentCredential(environment,apiKey.trim()),
    onSuccess:async data=>{setApiKey("");setShowKey(false);setShowKeyValue(false);qc.setQueryData(["routing-execute-credential",environment],data);await qc.invalidateQueries({queryKey:["routing-execute-status",environment]});connectionTest.reset();connectionTest.mutate();},
  });
  const clearCredential=useMutation({mutationFn:()=>api.clearEnvironmentCredential(environment),onSuccess:async()=>{setModel("");connectionTest.reset();await Promise.all([qc.invalidateQueries({queryKey:["routing-execute-credential",environment]}),qc.invalidateQueries({queryKey:["routing-execute-status",environment]})]);}});
  const environmentSwitch=useMutation({
    mutationFn:(enabled:boolean)=>api.setEnvironmentExecutionState(environment,enabled),
    onSuccess:data=>{qc.setQueryData(["routing-execute-environment-state",environment],data);qc.invalidateQueries({queryKey:["routing-execute-status",environment]});connectionTest.reset();if(data.enabled&&credential.data?.configured)connectionTest.mutate();},
  });
  const mockRun=useMutation({mutationFn:()=>api.runReplay(strategy)});

  // Mutation objects are intentionally excluded: this reset is keyed only by the selected data boundary.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(()=>{setModel("");setModelSearch("");setConfirmed(false);setShowKey(false);connectionTest.reset();mockRun.reset();},[mode,environment]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(()=>{if(realMode&&environmentState.data?.enabled&&credential.data?.configured&&connectionTest.isIdle)connectionTest.mutate();},[realMode,environmentState.data?.enabled,credential.data?.configured]);

  const availableModels=useMemo(()=>realMode?(catalog.data?.models??[]):MOCK_MODELS,[realMode,catalog.data?.models]);
  const models=useMemo(()=>{const query=modelSearch.trim().toLowerCase();return availableModels.filter(item=>!query||[item.id,item.display_name,item.owned_by].some(value=>String(value??"").toLowerCase().includes(query)));},[availableModels,modelSearch]);
  const selected=availableModels.find(item=>item.id===model);
  const capability=realMode?options.data?.model_output_capabilities.find(item=>item.model_id===model):undefined;
  const confirmedLimits=[capability?.confirmed_max_output_tokens,capability?.confirmed_channel_max_output_tokens,capability?.max_context_tokens].filter((item):item is number=>typeof item==="number"&&item>0);
  const effectiveLimit=realMode?(confirmedLimits.length===3?Math.min(...confirmedLimits):null):8192;
  const maxTokensNumber=Number(maxTokens);
  const inputValid=Boolean(model&&prompt.trim()&&Number.isInteger(maxTokensNumber)&&maxTokensNumber>0);
  const blockers:string[]=[];
  if(realMode&&!credential.data?.configured)blockers.push("API 密钥尚未配置");
  if(realMode&&environment==="china_uat"&&environmentState.data&&!environmentState.data.enabled)blockers.push("UAT 环境当前已关闭，请打开 UAT 环境后再执行");
  if(realMode&&environmentState.data?.enabled&&credential.data?.configured&&!connectionReady)blockers.push(connectionTest.isPending?"正在测试 UAT 连接":"UAT 连接尚未测试成功");
  if(!model)blockers.push("尚未选择模型");
  if(!prompt.trim())blockers.push("请求内容不能为空");
  if(!Number.isInteger(maxTokensNumber)||maxTokensNumber<=0)blockers.push("最大输出 Token 必须是正整数");
  if(realMode&&model&&effectiveLimit===null)blockers.push("当前模型或渠道的能力信息待确认，暂不能执行真实请求");
  if(effectiveLimit!==null&&maxTokensNumber>effectiveLimit)blockers.push(`最大输出不能超过已确认上限 ${effectiveLimit.toLocaleString("zh-CN")}`);
  if(realMode&&stream&&!options.data?.stream_execution_ready)blockers.push("流式真实执行尚未开放");
  if(realMode&&requestKind!=="text")blockers.push("当前多模态请求需先通过模型与渠道能力审核");

  const body:UatRequestBody={environment_id:environment,mode:"real_uat_execute",confirmation:{confirmed,confirmation_text:confirmed?"I understand this will call the Weimeta UAT API and may incur UAT cost.":""},request:{requested_model:model,channel_id:"unified-routing",messages:[{role:"user",content:prompt}],stream,max_tokens:maxTokensNumber},measurement:{plan_id:`UI-${Date.now()}`,request_profile_id:"CUSTOM",session_id:"LOCAL-CONSOLE"},shadow:{run_before_execution:true,strategy}};
  const validation=useMutation<unknown,Error>({mutationFn:async()=>realMode?api.validateEnvironmentRequest(environment,body):api.runReplay(strategy)});
  const execution=useMutation({mutationFn:()=>api.executeEnvironmentRequest(environment,body),onSuccess:()=>{setConfirmed(false);qc.invalidateQueries({queryKey:["console-runs"]});}});
  const multimodalValidation=useMutation({mutationFn:()=>{if(!mediaFile||!model||!mediaChannel.trim())throw new Error("请选择文件、模型并填写真实渠道 ID");return api.validateMultimodal({environment_id:environment,model_id:model,channel_id:mediaChannel.trim(),subject_version:"1",request_type:requestKind as "image"|"audio"|"video",mime_type:mediaFile.type||"application/octet-stream",input_bytes:mediaFile.size});}});
  const focusKey=()=>{setShowKey(true);window.setTimeout(()=>keyInputRef.current?.focus(),0)};
  const connectionLabel=!realMode?"不适用":connectionTest.isPending?"测试中":connectionReady?"成功":connectionTest.data||connectionTest.isError?"失败":"未测试";
  const keySource=credential.data?.credential_source==="windows_encrypted_vault"?"本机加密密钥":credential.data?.credential_source==="environment"?"后端配置":"未配置";
  const validationData=validation.data;
  const estimated=validationData!==null&&typeof validationData==="object"&&"cost_estimate" in validationData?(validationData as {cost_estimate?:{estimated_cost_cny?:number}}).cost_estimate?.estimated_cost_cny:undefined;

  return <div className="execute-page execute-page-v2">
    <PageHeader title="发起调度" description="根据能力、指标和策略选择渠道，并在当前数据模式中执行或模拟请求。" actions={<><Link className="secondary button-link" to="/routing/runs">查看执行记录</Link><button className="secondary" onClick={()=>setDetailOpen(true)}>环境详情</button></>}/>
    {!realMode&&<div className="mock-mode-banner" role="status"><b>当前为 Mock 数据，不会发送真实请求</b><span>模型、渠道和指标均来自本地模拟器，适合验证页面与调度流程。</span></div>}
    <section className="execution-readiness-card" aria-label="环境与执行状态">
      <div><span>环境与模式</span><b>{realMode?(environment==="china_uat"?"国内 UAT":"海外环境"):"本地 Mock"}</b><small>{realMode?(environmentState.data?.enabled?"UAT 环境已开启":"UAT 环境已关闭"):"网络调用 0"}</small></div>
      <div><span>API 密钥</span><b>{realMode?(credential.data?.configured?"已配置":"未配置"):"无需密钥"}</b><small>{realMode?`来源：${keySource}`:"Mock 模式不读取真实凭据"}</small></div>
      <div><span>连接与执行</span><b>{realMode?`接口检测：${connectionLabel}`:"Mock 调度可运行"}</b><small>{realMode?`请求条件：${blockers.length?"存在阻断":"可执行"}`:"真实 UAT 执行按钮已隐藏"}</small></div>
      {realMode&&environment==="china_uat"&&environmentState.data&&<label className="uat-environment-toggle readiness-toggle"><span>UAT 环境</span><input aria-label="UAT 环境开关" type="checkbox" role="switch" checked={environmentState.data.enabled} disabled={environmentSwitch.isPending} onChange={event=>environmentSwitch.mutate(event.target.checked)}/><b>{environmentSwitch.isPending?"保存中…":environmentState.data.enabled?"开启":"关闭"}</b></label>}
    </section>
    {realMode&&!credential.data?.configured&&<div className="primary-blocker" role="alert"><div><b>当前不能执行真实请求：API 密钥尚未配置。</b><span>可在本页安全配置，保存后立即进行连接测试。</span></div><button onClick={focusKey}>配置 API Key</button></div>}
    {realMode&&credential.data?.configured&&blockers.length>0&&<div className="primary-blocker" role="alert"><div><b>当前不能执行真实请求：{blockers[0]}。</b>{blockers.length>1&&<span>另有 {blockers.length-1} 项需要处理</span>}</div><button className="secondary" onClick={()=>connectionTest.mutate()} disabled={connectionTest.isPending}>重新测试连接</button></div>}
    {realMode&&(showKey||!credential.data?.configured)&&<section className="credential-inline-panel" aria-label="API Key 配置"><header><div><h2>配置国内 UAT API Key</h2><p>密钥只提交给本地后端并加密保存，不写入浏览器存储、URL、日志或请求预览。</p></div><StatusBadge tone="warning">敏感信息</StatusBadge></header><div className="credential-input-row"><label>API Key<input ref={keyInputRef} type={showKeyValue?"text":"password"} autoComplete="new-password" data-1p-ignore="true" value={apiKey} onChange={event=>setApiKey(event.target.value)} placeholder="请输入当前环境的 API Key"/></label><button className="secondary" type="button" onClick={()=>setShowKeyValue(value=>!value)}>{showKeyValue?"隐藏":"显示"}</button><button type="button" disabled={!apiKey.trim()||saveCredential.isPending} onClick={()=>saveCredential.mutate()}>{saveCredential.isPending?"保存中…":"保存并测试连接"}</button></div>{saveCredential.error&&<ErrorState title="API Key 保存失败" description={errorMessage(saveCredential.error)}/>}</section>}
    <Section title="配置请求" description="模型、响应方式和请求内容构成本次调度输入。" className="request-config request-config-v2">
      <div className="segmented" role="tablist">{(["text","image","audio","video"] as RequestKind[]).map(kind=><button key={kind} role="tab" aria-selected={requestKind===kind} className={requestKind===kind?"active":""} onClick={()=>setRequestKind(kind)}>{{text:"文本",image:"图片",audio:"音频",video:"视频"}[kind]}</button>)}</div>
      <div className="request-config-grid"><label className="wide model-search-field">模型<span className="input-source">{realMode?"来源：UAT 模型目录":"来源：本地 Mock 目录"}</span><input value={modelSearch} onChange={event=>setModelSearch(event.target.value)} placeholder="搜索模型 ID、名称或供应商"/></label><div className="wide compact-model-picker" role="listbox" aria-label={realMode?"真实模型目录":"Mock 模型目录"}>{realMode&&!credential.data?.configured?<div className="compact-empty">配置 API Key 并完成连接测试后读取真实模型目录。</div>:realMode&&!connectionReady?<div className="compact-empty">连接测试成功后加载真实模型目录。</div>:catalog.isError?<ErrorState title="模型目录读取失败" description={errorMessage(catalog.error)}/>:models.length?models.slice(0,8).map(item=><button type="button" role="option" aria-selected={model===item.id} className={model===item.id?"selected":""} key={item.id} onClick={()=>setModel(item.id)}><b>{item.display_name}</b><span>{item.owned_by||"供应商未提供"} · {realMode?"真实目录":"Mock"}</span></button>):<div className="compact-empty">{modelSearch?"没有匹配的模型。":"当前目录未返回可用模型。"}</div>}</div>{selected&&<div className="wide selected-model compact-selected"><b>{selected.display_name}</b><span>{selected.owned_by||"供应商未提供"} · 模型 ID：{selected.id}</span></div>}
        <label>响应方式<div className="segmented small"><button type="button" className={!stream?"active":""} onClick={()=>setStream(false)}>非流式</button><button type="button" className={stream?"active":""} disabled={realMode&&!options.data?.stream_execution_ready} onClick={()=>setStream(true)}>流式</button></div></label>
        <label>最大输出 Token<input type="number" min="1" value={maxTokens} onChange={event=>setMaxTokens(event.target.value)} placeholder="请输入正整数"/><div className="token-presets">{[2048,4096,8192].map(value=><button type="button" key={value} className="secondary compact" onClick={()=>setMaxTokens(String(value))}>{value}</button>)}</div>{effectiveLimit!==null&&maxTokensNumber>effectiveLimit&&<small className="field-error">超过已确认上限 {effectiveLimit.toLocaleString("zh-CN")}</small>}</label>
        {requestKind==="text"?<label className="wide prompt-field">请求内容<textarea value={prompt} onChange={event=>setPrompt(event.target.value)} placeholder="输入要发送给模型的安全请求内容"/><span className="field-meta"><span>{prompt.length.toLocaleString("zh-CN")} 字符 · 预计约 {Math.ceil(prompt.length/2).toLocaleString("zh-CN")} Token</span><button type="button" className="text-button" onClick={()=>setPrompt("")}>清空</button></span></label>:<div className="wide multimodal-contract"><label>{requestKind==="image"?"图片文件":requestKind==="audio"?"音频文件":"视频文件"}<input type="file" onChange={event=>setMediaFile(event.target.files?.[0]??null)}/></label><label>真实渠道 ID<input value={mediaChannel} onChange={event=>setMediaChannel(event.target.value)} placeholder="由模型—渠道关系提供"/></label><label>文本指令<textarea value={mediaInstruction} onChange={event=>setMediaInstruction(event.target.value)}/></label><button type="button" disabled={!realMode||!mediaFile||!model||!mediaChannel.trim()} onClick={()=>multimodalValidation.mutate()}>校验多模态能力</button>{multimodalValidation.error&&<ErrorState title="能力校验未通过" description={errorMessage(multimodalValidation.error)}/>} {multimodalValidation.data&&<div className="preview-summary"><StatusBadge tone={multimodalValidation.data.allowed?"success":"warning"}>{multimodalValidation.data.allowed?"能力证据已确认":"能力证据不足"}</StatusBadge><span>校验 ID：{multimodalValidation.data.validation_id}</span><span>媒体未落盘 · 外部调用 0</span></div>}</div>}
      </div>
    </Section>
    <Section title="路由策略" description="未由模板、策略或审批提供的限制保持为空，不使用隐藏默认值。"><div className="routing-summary-grid"><label>策略<select value={strategy} onChange={event=>setStrategy(event.target.value)}><option value="latency_first">延迟优先</option><option value="cost_first">成本优先</option><option value="confidence_aware_v2">置信度感知</option></select><small>来源：用户选择</small></label><label>数据来源<select value={metricSource} onChange={event=>setMetricSource(event.target.value)}><option value="">请选择</option><option value="dynamic">动态指标</option><option value="snapshot">固定快照</option></select><small>{metricSource?"来源：用户选择":"未提供"}</small></label><label>最大尝试次数<input type="number" min="1" value={maxAttempts} onChange={event=>setMaxAttempts(event.target.value)} placeholder="未提供"/><small>仅约束本次请求</small></label><label>最大预算<input type="number" min="0" step="0.01" value={budget} onChange={event=>setBudget(event.target.value)} placeholder="未提供"/><small>币种：CNY</small></label></div></Section>
    <Section title="执行条件检查" description="只展示需要处理的警告和阻断。">{blockers.length?<ul className="check-list">{blockers.map(item=><li key={item}><span aria-hidden="true">!</span><b>{item}</b>{item.includes("密钥")?<button className="text-button" onClick={focusKey}>处理</button>:<Link to="/data/capabilities">处理</Link>}</li>)}</ul>:<details><summary>{realMode?"全部执行条件已通过":"Mock 调度条件已通过"}</summary><p>模型、请求参数与当前模式均可用。</p></details>}</Section>
    <Section title="候选与决策预览" description="生成预览后才展示候选、过滤原因与费用估算。" className="candidate-preview-collapsed">{!validation.data&&!mockRun.data?<div className="collapsed-preview-row"><span>尚未生成预览</span><button className="secondary compact" disabled={!inputValid||validation.isPending} onClick={()=>validation.mutate()}>{realMode?"仅验证请求":"预览 Mock 决策"}</button></div>:<div className="preview-summary"><StatusBadge tone="success">预览已生成</StatusBadge><span>预计费用：{estimated===undefined?"无法估算":`CNY ¥${estimated.toFixed(4)}`}</span><span>{realMode?"真实渠道关系以服务端证据为准":"Mock 渠道：local-simulator"}</span></div>}</Section>
    <Section title="请求预览" description={`${realMode?"POST /v1/chat/completions":"LOCAL /replay/run"} · ${model||"未选择模型"} · ${stream?"流式":"非流式"} · max_tokens ${maxTokens||"未提供"}`} actions={<button className="secondary compact" onClick={()=>setPreviewOpen(value=>!value)}>{previewOpen?"收起":"展开"}</button>}>{previewOpen&&<pre className="safe-preview">{JSON.stringify({environment_id:realMode?environment:"local_mock",model:model||null,request_type:requestKind,stream,max_tokens:maxTokens||null,routing_policy:strategy,messages:"[已脱敏请求内容]"},null,2)}</pre>}</Section>
    <div className="execution-action-bar"><div><b>{realMode?(estimated===undefined?"费用待验证":`预计最多 CNY ¥${estimated.toFixed(4)}`):"Mock 模式 · 外部调用 0"}</b><span>{maxAttempts?`最多尝试 ${maxAttempts} 次 · `:""}{blockers.length?`阻断：${blockers[0]}`:"当前可执行"}</span></div><div><button className="secondary">保存草稿</button><button className="secondary" disabled={!inputValid||validation.isPending} onClick={()=>validation.mutate()}>{realMode?"仅模拟决策":"验证请求"}</button>{realMode&&<><label className="execute-confirm"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>确认真实费用</label><button disabled={blockers.length>0||!confirmed||execution.isPending} onClick={()=>execution.mutate()}>执行真实 UAT 请求</button></>}{!realMode&&<button disabled={!inputValid||mockRun.isPending} onClick={()=>mockRun.mutate()}>运行 Mock 调度</button>}</div></div>
    {(environmentSwitch.error||connectionTest.error||execution.error)&&<ErrorState title="操作未完成" description={errorMessage(environmentSwitch.error||connectionTest.error||execution.error)}/>} 
    {realMode&&credential.data?.configured&&<div className="credential-safe-actions"><span>密钥来源：{keySource} · 指纹：{formatValue(credential.data.key_fingerprint)}</span><button className="text-button" onClick={focusKey}>更换密钥</button><button className="text-button danger-text" disabled={clearCredential.isPending} onClick={()=>clearCredential.mutate()}>清除本机密钥</button></div>}
    {detailOpen&&<div className="drawer-backdrop" onClick={()=>setDetailOpen(false)}><aside className="console-drawer" aria-label="环境详情" onClick={event=>event.stopPropagation()}><header><div><p>环境详情</p><h2>{realMode?(environment==="china_uat"?"国内 UAT":"海外环境"):"本地 Mock"}</h2></div><button aria-label="关闭" onClick={()=>setDetailOpen(false)}>×</button></header><dl><dt>数据模式</dt><dd>{realMode?"真实数据":"Mock 数据"}</dd><dt>API 地址</dt><dd>{realMode?formatValue(status.data?.base_url):"不适用"}</dd><dt>密钥来源</dt><dd>{realMode?keySource:"无需密钥"}</dd><dt>接口检测</dt><dd>{connectionLabel}</dd><dt>受控任务</dt><dd>{realMode?humanStatus(control.data?.status):"不适用"}</dd></dl><details><summary>技术字段</summary><pre>{JSON.stringify({mode,environment_id:environment,connection_status:connectionTest.data?.connection_status??null,controlled_task_status:control.data?.status??null},null,2)}</pre></details></aside></div>}
  </div>;
}

export const RoutingExecutePage=UatWorkbenchPage;

export function RoutingRunsPage({environment}:{environment:EnvironmentId}){
  const [cursor,setCursor]=useState(0);
  const [records,setRecords]=useState<StandardizedCallLog[]>([]);
  const [lastUpdated,setLastUpdated]=useState<string|null>(null);
  const runs=useQuery({queryKey:["routing-runs",environment,cursor],queryFn:({signal})=>api.callLogs(environment,cursor,1000,signal),refetchInterval:7000});
  const analytics=useQuery({queryKey:["routing-runs-analytics",environment],queryFn:({signal})=>api.callLogAnalytics(environment,signal),refetchInterval:7000});
  const control=useQuery({queryKey:["routing-runs-control"],queryFn:({signal})=>api.uatExecutionControl(signal)});
  const catalog=useQuery({queryKey:["routing-runs-models",environment],queryFn:({signal})=>api.environmentModels(environment,false,signal)});
  const environmentStatus=useQuery({queryKey:["routing-runs-status",environment],queryFn:({signal})=>api.environmentStatus(environment,signal)});
  const [filter,setFilter]=useState("all");
  const [modelFilter,setModelFilter]=useState("");
  const [channelFilter,setChannelFilter]=useState("all");
  const [sourceFilter,setSourceFilter]=useState("all");
  const [streamFilter,setStreamFilter]=useState("all");
  const [errorFilter,setErrorFilter]=useState("all");
  const [selected,setSelected]=useState<Record<string,unknown>|null>(null);
  const [showCreate,setShowCreate]=useState(false);
  const [confirmOpen,setConfirmOpen]=useState(false);
  const [selectedModel,setSelectedModel]=useState("");
  const [maxRequests,setMaxRequests]=useState("");
  const [maxCost,setMaxCost]=useState("");
  const [duration,setDuration]=useState("");
  const [durationUnit,setDurationUnit]=useState<"minutes"|"hours">("minutes");
  const [maxConcurrency,setMaxConcurrency]=useState("");
  const [maxAttempts,setMaxAttempts]=useState("");
  const [approvalReference,setApprovalReference]=useState("");
  useEffect(()=>{setCursor(0);setRecords([])},[environment]);
  useEffect(()=>{
    if(!runs.data)return;
    setRecords(current=>{
      const merged=new Map(current.map(item=>[item.record_id,item]));
      runs.data.items.forEach(item=>merged.set(item.record_id,item));
      return Array.from(merged.values()).sort((a,b)=>b.occurred_at.localeCompare(a.occurred_at));
    });
    if(runs.data.updated_at)setLastUpdated(current=>!current||runs.data!.updated_at!>current?runs.data!.updated_at:current);
    if(runs.data.next_cursor>cursor)setCursor(runs.data.next_cursor);
  },[runs.data,cursor]);
  const invalidateConfirmation=()=>setConfirmOpen(false);
  const durationSeconds=Number(duration)*(durationUnit==="hours"?3600:60);
  const numericValues=[Number(maxRequests),Number(maxCost),durationSeconds,Number(maxConcurrency),Number(maxAttempts)];
  const formComplete=Boolean(selectedModel&&approvalReference.trim()&&numericValues.every(value=>Number.isFinite(value)&&value>0));
  const clientLimitErrors:string[]=[];
  if(maxRequests&&Number(maxRequests)>24)clientLimitErrors.push("超过当前环境策略的请求数上限 24");
  if(maxCost&&Number(maxCost)>3)clientLimitErrors.push("超过当前环境策略的费用上限 CNY ¥3.00");
  if(duration&&durationSeconds>1200)clientLimitErrors.push("超过当前权限允许的有效时长 20 分钟");
  if(maxConcurrency&&Number(maxConcurrency)>1)clientLimitErrors.push("超过当前环境允许的最大并发 1");
  if(maxAttempts&&Number(maxAttempts)>3)clientLimitErrors.push("超过安全策略允许的最大尝试次数 3");
  if(maxCost&&environmentStatus.data?.remaining_budget_cny!==null&&environmentStatus.data?.remaining_budget_cny!==undefined&&Number(maxCost)>environmentStatus.data.remaining_budget_cny)clientLimitErrors.push("超过当前环境剩余预算");
  const activateTask=useMutation({mutationFn:async()=>{
    const expiresAt=new Date(Date.now()+durationSeconds*1000).toISOString();
    const draft=await api.createUatExecutionControl({environment_id:"china_uat",allowed_models:[selectedModel],allowed_channels:["unified-routing"],max_requests:Number(maxRequests),max_total_cost:maxCost,cost_currency:"CNY",max_duration_seconds:durationSeconds,max_concurrency:Number(maxConcurrency),max_attempts_per_request:Number(maxAttempts),expires_at:expiresAt,explicit_confirmation:true});
    if(!draft.task)throw new Error("受控任务创建后未返回任务信息");
    await api.approveUatExecutionControl(draft.task.task_id,{approval_reference:approvalReference,approval_expires_at:expiresAt});
    return api.activateUatExecutionControl(draft.task.task_id);
  },onSuccess:async()=>{setConfirmOpen(false);setShowCreate(false);await control.refetch();}});
  const channels=Array.from(new Set(records.map(row=>row.channel_id).filter(Boolean))) as string[];
  const errors=Array.from(new Set(records.map(row=>row.error_category).filter(Boolean))) as string[];
  const rows=records.filter(row=>(filter==="all"||String(row.request_status??"").toLowerCase()===filter)
    &&(!modelFilter||[row.requested_model,row.actual_model,row.local_request_id,row.request_id,
      row.provider_request_id,row.provider_response_id,row.provider_trace_id,row.provider_log_id,
      row.decision_id].some(value=>String(value??"").toLowerCase().includes(modelFilter.toLowerCase())))
    &&(channelFilter==="all"||row.channel_id===channelFilter)
    &&(sourceFilter==="all"||row.source_type===sourceFilter)
    &&(streamFilter==="all"||row.stream===(streamFilter==="stream"))
    &&(errorFilter==="all"||row.error_category===errorFilter));
  const explainableDecision=records.find(row=>Boolean(row.decision_id))?.decision_id;
  return <><PageHeader title="执行记录" description="历史 UAT 与新调用共用标准化日志；每 7 秒按服务端游标读取最新状态。" actions={<><button className="secondary" onClick={()=>{runs.refetch();analytics.refetch()}}>刷新</button><button className="secondary" onClick={()=>setShowCreate(value=>!value)}>创建受控任务</button><Link className="button-link" to="/routing/execute">发起调度</Link></>}/>
    <BusinessSkillAction skillId="explain-routing-decision" label="解释最近一次真实决策" argumentsValue={{decision_id:explainableDecision??"",include_metrics:true,include_execution:true}} disabledReason={explainableDecision?undefined:"当前没有可解释的真实 decision_id"}/>
    <div className="execution-ledger-summary"><div><b>当前环境</b><span>{environment==="china_uat"?"国内 UAT":"海外环境"}</span></div><div><b>数据更新时间</b><span>{formatLocalTime(lastUpdated)}</span></div><div><b>实时更新</b><span>{runs.isError?"实时更新暂时中断":"每 7 秒按游标增量续传"}</span></div><div><b>历史记录</b><span>{runs.data?.historical_count??"读取中"}</span></div><div><b>今日 Token</b><span>{analytics.data?analytics.data.today_tokens.toLocaleString("zh-CN"):"读取中"}</span></div><div><b>今日费用</b><span>{analytics.data?(analytics.data.today_cost==null?"未提供":`${analytics.data.currency||"CNY"} ¥${analytics.data.today_cost}`):"读取中"}</span></div></div>
    {showCreate&&<Section title="受控开启参数" description="所有业务数值必须由有权限的用户填写；建议值不会自动代替确认。">
      <div className="policy-hint"><b>当前组织策略建议范围</b><span>请求数 ≤ 24 · 费用 ≤ CNY ¥3.00 · 时长 ≤ 20 分钟 · 并发 ≤ 1 · 尝试次数 ≤ 3</span><small>来源：本地 UAT 安全策略；不是表单默认值。</small></div>
      <div className="form-grid clean controlled-task-form"><label>允许模型<select value={selectedModel} onChange={event=>{setSelectedModel(event.target.value);invalidateConfirmation()}}><option value="">请选择真实模型目录中的模型</option>{(catalog.data?.models??[]).filter(item=>item.execution_allowed).map(item=><option key={item.id} value={item.id}>{item.display_name} · {item.owned_by||"供应商未提供"}</option>)}</select><small>来源：当前 Key 的真实模型目录</small></label><label>最大请求数<input inputMode="numeric" placeholder="请输入本次测试允许的最大请求数" value={maxRequests} onChange={event=>{setMaxRequests(event.target.value);invalidateConfirmation()}}/></label><label>最大测试费用<div className="input-with-unit"><input inputMode="decimal" placeholder="请输入本次测试预算" value={maxCost} onChange={event=>{setMaxCost(event.target.value);invalidateConfirmation()}}/><span>CNY</span></div></label><label>有效时长<div className="input-pair"><input inputMode="decimal" placeholder="请输入开启时长" value={duration} onChange={event=>{setDuration(event.target.value);invalidateConfirmation()}}/><select aria-label="时长单位" value={durationUnit} onChange={event=>{setDurationUnit(event.target.value as "minutes"|"hours");invalidateConfirmation()}}><option value="minutes">分钟</option><option value="hours">小时</option></select></div></label><label>最大并发<input inputMode="numeric" placeholder="请输入最大并发数" value={maxConcurrency} onChange={event=>{setMaxConcurrency(event.target.value);invalidateConfirmation()}}/></label><label>单次请求最大尝试次数<input inputMode="numeric" placeholder="请输入单个请求允许的总尝试次数" value={maxAttempts} onChange={event=>{setMaxAttempts(event.target.value);invalidateConfirmation()}}/></label><label className="wide">审批/授权引用<input placeholder="请输入或选择有效审批记录" value={approvalReference} onChange={event=>{setApprovalReference(event.target.value);invalidateConfirmation()}}/><small>来源：用户输入。当前后端没有审批单查询接口，因此不会伪造或自动回填审批记录。</small></label></div>
      {clientLimitErrors.length>0&&<ul className="issue-list">{clientLimitErrors.map(item=><li key={item}>{item}</li>)}</ul>}
      <div className="section-footer"><button className="secondary" onClick={()=>setShowCreate(false)}>取消</button><button disabled={!formComplete||clientLimitErrors.length>0} onClick={()=>setConfirmOpen(true)}>审核开启参数</button></div>
    </Section>}
    {control.data?.task&&<Section title="当前受控任务" description="任务审批和预算进度已从请求编辑器移到这里。"><div className="metric-grid four"><MetricCard label="状态" value={humanStatus(control.data.status)}/><MetricCard label="请求进度" value={`${control.data.task.used_requests} / ${control.data.task.max_requests}`}/><MetricCard label="费用进度" value={`${control.data.task.used_cost} / ${control.data.task.max_total_cost} ${control.data.task.cost_currency}`}/><MetricCard label="剩余时间" value={formatLocalTime(control.data.task.expires_at)}/></div></Section>}
    <Section title="执行列表" actions={<div className="segmented small">{["all","running","success","failed","timeout","interrupted"].map(item=><button className={filter===item?"active":""} key={item} onClick={()=>setFilter(item)}>{item==="all"?"全部":humanStatus(item)}</button>)}</div>}>
      <div className="filter-bar call-log-filters">
        <input aria-label="搜索模型或精确标识" placeholder="模型、本地 ID、Provider ID 或 decision_id" value={modelFilter} onChange={event=>setModelFilter(event.target.value)}/>
        <select aria-label="渠道" value={channelFilter} onChange={event=>setChannelFilter(event.target.value)}><option value="all">全部渠道</option>{channels.map(item=><option key={item}>{item}</option>)}</select>
        <select aria-label="来源" value={sourceFilter} onChange={event=>setSourceFilter(event.target.value)}><option value="all">全部来源</option><option value="historical_uat_csv">历史 UAT</option><option value="realtime_execution">实时执行</option></select>
        <select aria-label="响应方式" value={streamFilter} onChange={event=>setStreamFilter(event.target.value)}><option value="all">流式与非流式</option><option value="stream">流式</option><option value="nonstream">非流式</option></select>
        <select aria-label="错误分类" value={errorFilter} onChange={event=>setErrorFilter(event.target.value)}><option value="all">全部错误分类</option>{errors.map(item=><option key={item}>{item}</option>)}</select>
      </div>
      {runs.isError&&<ErrorState title="实时更新暂时中断" description="已保留当前页面数据；恢复连接后会从服务端游标继续读取。" onRetry={()=>runs.refetch()}/>} 
      {runs.isLoading&&records.length===0?<p>正在读取执行记录…</p>:rows.length?<div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>来源</th><th>Request ID</th><th>模型 / 实际模型</th><th>渠道</th><th>状态</th><th>总耗时 / 首字</th><th>输入 / 缓存 / 输出</th><th>费用</th><th>尝试</th><th>操作</th></tr></thead><tbody>{rows.map(row=><tr key={row.record_id}><td>{formatLocalTime(row.occurred_at)}</td><td>{row.is_historical?"历史 UAT":"实时执行"}</td><td>{row.request_id??"历史数据未提供"}</td><td><b>{row.requested_model}</b><small>{row.actual_model&&row.actual_model!==row.requested_model?`实际：${row.actual_model}`:"实际模型相同"}</small></td><td>{row.channel_id??"历史数据未提供"}</td><td><StatusBadge tone={toneFor(row.request_status)}>{humanStatus(row.request_status)}</StatusBadge></td><td>{row.total_latency_ms===null?"未提供":`${row.total_latency_ms.toLocaleString("zh-CN")} ms`}<small>{row.first_token_latency_ms===null?"非流式或未提供":`首字 ${row.first_token_latency_ms} ms`}</small></td><td>{row.input_tokens??0} / {row.cached_input_tokens??0} / {row.output_tokens??0}</td><td>{row.cost_amount===null?(row.is_historical?"未提供":"待 Provider 日志同步"):`${row.currency??"CNY"} ¥${row.cost_amount}`}</td><td>{row.total_attempts}</td><td>{row.decision_id?<Link className="text-button" to={`/routing/decisions/${encodeURIComponent(row.decision_id)}`}>决策日志</Link>:null}<button className="text-button" onClick={()=>setSelected(row as unknown as Record<string,unknown>)}>查看详情</button></td></tr>)}</tbody></table></div>:<EmptyState title="暂无执行记录" description="后端初始化或产生新调用后，这里会自动显示标准化日志。"/>}
    </Section>{selected&&<div className="drawer-backdrop" onClick={()=>setSelected(null)}><aside className="console-drawer wide" onClick={event=>event.stopPropagation()}><header><div><p>执行详情</p><h2>{formatValue(selected.local_request_id??selected.request_id)}</h2></div><button onClick={()=>setSelected(null)}>×</button></header><div className="detail-tabs"><button className="active">概览</button><button>请求与响应</button><button>决策过程</button><button>费用</button><button>证据</button></div><section className="trace-identifiers"><div><small>本地 Request ID</small><code>{formatValue(selected.local_request_id??selected.request_id)}</code></div><div><small>Provider Request ID</small><code>{formatValue(selected.provider_request_id,"未记录")}</code></div><div><small>Provider Response ID</small><code>{formatValue(selected.provider_response_id,"未记录")}</code></div><div><small>Provider Trace ID</small><code>{formatValue(selected.provider_trace_id,"未记录")}</code></div></section>{selected.cost_status==="pending_provider_sync"&&<div className="state warning"><b>待 Provider 日志同步</b><span>{selected.provider_sync_failure_reason==="historical_provider_identifier_not_captured"?"历史执行未保存可与账单日志精确关联的 Provider 标识。":"已保存响应侧 Provider 标识，但账单接口尚未暴露完全相等的公共标识。"}</span></div>}<details><summary>技术详情</summary><dl>{Object.entries(selected).filter(([key])=>!["assistant_content","reasoning_content","request_body","provider_identifiers_json"].includes(key)).slice(0,40).map(([key,value])=><><dt key={`${key}-dt`}>{key}</dt><dd key={`${key}-dd`}>{formatValue(value)}</dd></>)}</dl></details></aside></div>}
    {confirmOpen&&<div className="drawer-backdrop centered" onClick={()=>setConfirmOpen(false)}><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="control-confirm-title" onClick={event=>event.stopPropagation()}><header><h2 id="control-confirm-title">确认开启真实 UAT 执行</h2><p>确认后将创建、批准并激活一项受预算限制的任务。</p></header><dl><dt>环境</dt><dd>国内 UAT</dd><dt>允许模型</dt><dd>{selectedModel}</dd><dt>最大请求数</dt><dd>{maxRequests}</dd><dt>最大测试费用</dt><dd>CNY ¥{maxCost}</dd><dt>有效时长</dt><dd>{duration} {durationUnit==="hours"?"小时":"分钟"}</dd><dt>最大并发</dt><dd>{maxConcurrency}</dd><dt>最大尝试次数</dt><dd>{maxAttempts}</dd><dt>审批引用</dt><dd>{approvalReference}</dd></dl>{activateTask.error&&<ErrorState title="受控任务未开启" description={activateTask.error instanceof Error?activateTask.error.message:"后端拒绝了开启请求。"}/>}<footer><button className="secondary" onClick={()=>setConfirmOpen(false)}>返回修改</button><button disabled={activateTask.isPending} onClick={()=>activateTask.mutate()}>{activateTask.isPending?"正在开启…":"确认并开启"}</button></footer></section></div>}</>;
}

export function ModelCatalogPage({environment}:{environment:EnvironmentId}){
  const catalog=useQuery({queryKey:["model-catalog-page",environment],queryFn:({signal})=>api.environmentModels(environment,false,signal)});
  const capabilities=useQuery({queryKey:["model-catalog-capabilities"],queryFn:({signal})=>api.uatEditorOptions(signal)});
  const [search,setSearch]=useState("");
  const [provider,setProvider]=useState("all");
  const [selected,setSelected]=useState<UatModel|null>(null);
  const capabilityByModel=new Map((capabilities.data?.model_output_capabilities??[]).map(item=>[item.model_id,item]));
  const providers=Array.from(new Set((catalog.data?.models??[]).map(item=>item.owned_by||"未提供"))).sort();
  const rows=(catalog.data?.models??[]).filter(item=>(provider==="all"||(item.owned_by||"未提供")===provider)&&(!search||[item.id,item.display_name,item.owned_by].some(value=>String(value??"").toLowerCase().includes(search.toLowerCase()))));
  const confirmed=rows.filter(item=>capabilityByModel.get(item.id)?.status==="confirmed").length;
  return <><PageHeader title="模型目录" description="模型来自当前环境的真实模型目录；端点、渠道和能力证据分别展示。" actions={<><button className="secondary" onClick={()=>catalog.refetch()}>刷新模型目录</button><Link className="button-link secondary" to="/data/capabilities">同步能力信息</Link></>}/>
    <div className="metric-grid five"><MetricCard label="模型总数" value={catalog.data?.model_count??"读取中"}/><MetricCard label="目录可用" value={rows.filter(item=>item.execution_allowed).length}/><MetricCard label="能力已确认" value={confirmed}/><MetricCard label="能力待确认" value={Math.max(0,rows.length-confirmed)}/><MetricCard label="最后同步" value={formatLocalTime(catalog.data?.fetched_at)}/></div>
    <Section title="筛选目录"><div className="filter-bar"><input aria-label="搜索模型" placeholder="搜索模型 ID、名称或供应商" value={search} onChange={event=>setSearch(event.target.value)}/><select aria-label="供应商" value={provider} onChange={event=>setProvider(event.target.value)}><option value="all">全部供应商</option>{providers.map(item=><option key={item}>{item}</option>)}</select><select aria-label="能力类型"><option>全部能力</option><option>能力待确认</option></select><select aria-label="协议"><option>全部协议</option><option>OpenAI 兼容</option></select></div></Section>
    <Section title={`模型列表（${rows.length}）`} description="价格和渠道关系缺失时明确显示待确认，不估算为零。">
      {catalog.isLoading?<p>正在读取真实模型目录…</p>:catalog.data?.error?<ErrorState title="模型目录不可用" description={catalog.data.error.message} onRetry={()=>catalog.refetch()}/>:rows.length?<div className="table-scroll tall"><table className="console-table sticky-head"><thead><tr><th>模型</th><th>供应商</th><th>能力</th><th>协议</th><th>计费</th><th>输入价格</th><th>输出价格</th><th>渠道数</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>{rows.map(item=>{const cap=capabilityByModel.get(item.id);return <tr key={item.id}><td><b>{item.display_name}</b><small>{item.id}</small></td><td>{item.owned_by||"未提供"}</td><td><StatusBadge tone={cap?.status==="confirmed"?"success":"warning"}>{cap?.status==="confirmed"?"已确认":"待确认"}</StatusBadge></td><td>OpenAI 兼容</td><td>未配置</td><td>未配置</td><td>未配置</td><td>待后端确认</td><td><StatusBadge tone={item.execution_allowed?"success":"neutral"}>{item.execution_allowed?"目录可用":"不可用"}</StatusBadge></td><td>{formatLocalTime(catalog.data?.fetched_at)}</td><td><button className="text-button" onClick={()=>setSelected(item)}>详情</button></td></tr>})}</tbody></table></div>:<EmptyState title="当前 Key 未返回可用模型" description="不会回退到写死模型列表。配置有效密钥后重新读取真实目录。"/>}
    </Section>{selected&&<div className="drawer-backdrop" onClick={()=>setSelected(null)}><aside className="console-drawer wide" onClick={event=>event.stopPropagation()}><header><div><p>模型详情</p><h2>{selected.display_name}</h2></div><button onClick={()=>setSelected(null)}>×</button></header><div className="detail-tabs"><button className="active">基本信息</button><button>能力</button><button>价格</button><button>渠道</button><button>测试记录</button><button>版本</button></div><dl><dt>模型 ID</dt><dd>{selected.id}</dd><dt>供应商</dt><dd>{selected.owned_by||"未提供"}</dd><dt>目录状态</dt><dd>{selected.execution_allowed?"可用":"不可用"}</dd><dt>可用端点</dt><dd>OpenAI 兼容</dd><dt>渠道关系</dt><dd>当前仅获取到模型与端点信息，真实渠道关系待后端确认。</dd><dt>最大输出</dt><dd>{formatValue(capabilityByModel.get(selected.id)?.confirmed_max_output_tokens,"待确认")}</dd><dt>能力来源</dt><dd>{formatValue(capabilityByModel.get(selected.id)?.evidence_source,"待确认")}</dd></dl></aside></div>}</>;
}

export function AcceptanceCenterPage(){
  const active=useQuery({queryKey:["active-acceptance-run"],queryFn:({signal})=>api.activeAcceptanceRun(signal),refetchInterval:5000,retry:false});
  const summary=useQuery({queryKey:["acceptance-live-summary",active.data?.acceptance_run_id],queryFn:({signal})=>api.acceptanceLiveSummary(active.data!.acceptance_run_id,signal),enabled:Boolean(active.data?.acceptance_run_id),refetchInterval:5000});
  const data=summary.data;
  const liveRows=(data?.six_model_results??[]).filter(row=>!row.is_fault_injected);
  const uniqueModels=new Set(liveRows.map(row=>row.requested_model)).size;
  const successful=liveRows.filter(row=>row.request_status==="SUCCESS").length;
  const canary=data?.traffic_proposals?.at(-1);
  return <><PageHeader title="验收中心" description="本页只展示当前国内 UAT 验收运行的真实持久化证据，每 5 秒更新。"/>
    <BusinessSkillAction skillId="collect-acceptance-evidence" label="生成验收证据" argumentsValue={{acceptance_suite_id:active.data?.acceptance_run_id??"current",environment_id:"china_uat",time_range:"current_run"}}/>
    {!active.data?<EmptyState title="当前没有运行中的真实验收" description="开始验收运行后，请求、决策、故障、灰度和审计会在此统一显示。"/>:<>
      <Section title="唯一验收基线" description={`开始时间 ${formatLocalTime(active.data.started_at)}`}><div className="status-strip"><StatusBadge tone="info">国内 UAT</StatusBadge><b>{active.data.acceptance_run_id}</b><span>状态：{humanStatus(active.data.status)}</span><span>最后刷新：{formatLocalTime(new Date().toISOString())}</span></div></Section>
      <div className="metric-grid five"><MetricCard label="本轮真实记录" value={data?.evidence_counts.realtime??"读取中"} meta="统一调用日志"/><MetricCard label="已覆盖模型" value={`${uniqueModels}/6`} meta="现场模型目录"/><MetricCard label="真实成功调用" value={successful} meta={`共 ${liveRows.length} 条非注入记录`}/><MetricCard label="受控注入记录" value={data?.evidence_counts.injected??"读取中"} meta="与 Provider 错误分开"/><MetricCard label="模型级灰度" value={canary?humanStatus(canary.state):"暂无"} meta={canary?String(canary.proposal_id):"未创建提案"}/></div>
      <Section title="六模型现场对比" description="相同 Prompt 与参数；费用缺失时显示“未提供”，不会填 0。">
        {summary.isLoading?<p>正在读取真实验收日志……</p>:liveRows.length?<div className="table-scroll"><table className="console-table sticky-head"><thead><tr><th>模型</th><th>方式</th><th>状态</th><th>HTTP</th><th>总耗时</th><th>首 Token</th><th>输入/输出 Token</th><th>费用</th><th>Request ID</th><th>决策</th></tr></thead><tbody>{liveRows.map((row,index)=><tr key={`${row.request_id}-${index}`}><td><b>{row.actual_model||row.requested_model}</b></td><td>{row.stream?"流式":"非流式"}</td><td><StatusBadge tone={row.request_status==="SUCCESS"?"success":"danger"}>{humanStatus(row.request_status)}</StatusBadge></td><td>{formatValue(row.http_status)}</td><td>{row.total_latency_ms==null?"未提供":`${Math.round(row.total_latency_ms)} ms`}</td><td>{row.first_token_latency_ms==null?"不适用/未提供":`${Math.round(row.first_token_latency_ms)} ms`}</td><td>{formatValue(row.input_tokens)} / {formatValue(row.output_tokens)}</td><td>{row.cost_amount==null?"Provider 未提供":`${row.currency||"CNY"} ${row.cost_amount}`}</td><td><small>{formatValue(row.request_id)}</small></td><td>{row.decision_id?<Link to={`/routing/decisions/${encodeURIComponent(row.decision_id)}`}>查看决策</Link>:"未生成"}</td></tr>)}</tbody></table></div>:<EmptyState title="尚无六模型真实记录" description="不会回退到 Demo 或 Mock 数据。"/>}
      </Section>
      <Section title="故障、Fallback 与灰度证据"><div className="table-scroll"><table className="console-table"><thead><tr><th>证据类型</th><th>数量/状态</th><th>说明</th><th>入口</th></tr></thead><tbody><tr><td>UAT 受控故障</td><td>{data?.fault_injection_results.length??0} 条</td><td>统一日志标记为受控注入，不冒充 Provider 故障</td><td><Link to="/observability/errors">错误记录</Link></td></tr><tr><td>灰度提案</td><td>{data?.traffic_proposals.length??0} 个</td><td>{canary?`${formatValue(canary.source_model_id)} → ${formatValue(canary.target_model_id)}，${humanStatus(canary.state)}`:"暂无真实提案"}</td><td><Link to="/governance/traffic">流量切换</Link></td></tr><tr><td>决策索引</td><td>{data?.evidence_index.decision_id?.length??0} 个</td><td>可按 decision_id 下钻候选、执行与 Fallback</td><td><Link to="/observability/decisions">决策日志</Link></td></tr></tbody></table></div></Section>
    </>}
  </>;
}

export function ApiGapPage({title,description,available,children}:{title:string;description:string;available:string[];children?:React.ReactNode}){
  return <><PageHeader title={title} description={description}/><Section title="数据接入状态"><div className="api-contract-list">{available.map(item=><div key={item}><StatusBadge tone="success">已接入</StatusBadge><span>{item}</span></div>)}</div>{children||<EmptyState title="暂无可展示记录" description="对应后端未返回数据时保持空状态，不生成示例成功数据。"/>}</Section></>;
}
