import {useEffect,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {Bar,BarChart,CartesianGrid,Legend,Tooltip,XAxis,YAxis} from "recharts";
import {api,ApiError,type GovernanceKillSwitch} from "../services/api";

const stateLabel:Record<string,string>={
  CLOSED:"关闭（正常放行）",OPEN:"打开（阻止流量）",HALF_OPEN:"半开（仅授权探针）",
  supported:"已确认支持",unsupported:"已确认不支持",unknown:"待确认（阻止）",
  conflicting:"证据冲突（阻止）",pending_confirmation:"待确认（阻止）",stale:"证据陈旧（阻止）",
  expired:"已过期（阻止）",revoked:"已撤销（阻止）",authoritatively_attributed:"已由可信执行层归因",
  authoritative_execution_attribution_missing:"缺少权威实际渠道（阻止推断）",
  PROPOSED:"待审批",APPROVED:"已审批",ACTIVE:"沙箱灰度中",ROLLED_BACK:"已回滚",
  CANCELLED:"已取消",RESERVED:"已预留",RUNNING:"Mock 运行中",RECONCILED:"已对账",
  EMERGENCY_STOPPED:"紧急停止",SUCCEEDED:"成功",FAILED:"失败",STOPPED:"已停止",
};
const stageLabel:Record<string,string>={
  candidate_loading:"候选加载",metric_lookup:"指标查询",eligibility_and_scoring:"资格判断与评分",
  decision_persistence:"决策持久化",audit_emission:"审计写入",scheduler_total:"Scheduler 总计",
};
const formatTime=(value?:string|null)=>value?new Date(value).toLocaleString():"待确认";
const count=(values:Record<string,number>)=>Object.values(values).reduce((sum,value)=>sum+value,0);
const activeSwitches=(items:GovernanceKillSwitch[])=>items.filter(item=>item.active).length;
const textValue=(row:Record<string,unknown>,key:string)=>{
  const value=row[key];return value===null||value===undefined||value===""?"待确认":String(value);
};
const numberValue=(value:number|null)=>value===null?"待确认":`${value.toFixed(2)} ms`;

function ModeNotice({enabled,mode}:{enabled:boolean;mode:string}){
  return <div className="notice" role="note"><b>{enabled?"本地治理已启用":"默认禁用"}</b>
    <span>{mode}；真实平台执行未授权。任何变更仍需要独立身份提供方、角色授权与人工审批。</span></div>;
}

export default function SafetyGovernancePage(){
  const [selectedDecisionId,setSelectedDecisionId]=useState("");
  const circuit=useQuery({queryKey:["circuit-breakers"],queryFn:({signal})=>api.circuitBreakerStatus(signal)});
  const exploration=useQuery({queryKey:["exploration-governance"],queryFn:({signal})=>api.explorationStatus(signal)});
  const capabilities=useQuery({queryKey:["capability-evidence"],queryFn:({signal})=>api.capabilityEvidence(signal)});
  const traffic=useQuery({queryKey:["traffic-change-governance"],queryFn:({signal})=>api.trafficChangeStatus(signal)});
  const probes=useQuery({queryKey:["probe-governance"],queryFn:({signal})=>api.probeGovernanceStatus(signal)});
  const highCost=useQuery({queryKey:["high-cost-test-governance"],queryFn:({signal})=>api.highCostTestStatus(signal)});
  const timeline=useQuery({queryKey:["safety-governance-timeline"],queryFn:({signal})=>api.safetyGovernanceTimeline(signal)});
  const skillDiscovery=useQuery({queryKey:["formal-agent-skill-discovery"],queryFn:({signal})=>api.formalAgentSkillDiscovery(signal)});
  const skillStatus=useQuery({queryKey:["formal-agent-skill-status"],queryFn:({signal})=>api.formalAgentSkillStatus(signal)});
  const attribution=useQuery({queryKey:["scheduler-attribution"],queryFn:({signal})=>api.schedulerAttribution(signal)});
  const price=useQuery({queryKey:["price-status"],queryFn:({signal})=>api.priceStatus(signal)});
  const priceAudit=useQuery({queryKey:["price-audit"],queryFn:({signal})=>api.priceAudit(signal)});
  const overhead=useQuery({queryKey:["scheduler-overhead",60],queryFn:({signal})=>api.schedulerOverhead(60,signal)});
  const reconstruction=useQuery({queryKey:["decision-reconstruction",selectedDecisionId],
    queryFn:({signal})=>api.decisionReconstruction(selectedDecisionId,signal),enabled:Boolean(selectedDecisionId)});
  const queries=[circuit,exploration,capabilities,traffic,probes,highCost,timeline,skillDiscovery,skillStatus,attribution,price,priceAudit,overhead];
  useEffect(()=>{
    if(!selectedDecisionId&&attribution.data?.items[0])setSelectedDecisionId(attribution.data.items[0].decision_id);
  },[attribution.data,selectedDecisionId]);

  if(queries.some(query=>query.isLoading))return <main className="governance-workspace"><header className="ops-page-header"><div><p className="eyebrow">FAIL-CLOSED · 只读监控</p><h1>安全治理与执行归因</h1><p>加载期间未知状态保持阻止，不回退为可用。</p></div></header><div className="state loading" role="status"><b>正在读取安全治理状态…</b><span>仅查询本地证据，不访问 UAT 或模型平台。</span></div></main>;
  const error=queries.find(query=>query.error)?.error;
  if(error)return <main className="governance-workspace"><header className="ops-page-header"><div><p className="eyebrow">FAIL-CLOSED · 只读监控</p><h1>安全治理与执行归因</h1><p>读取失败不会改变任何治理状态。</p></div></header><div className="state error" role="alert"><b>{error instanceof ApiError?error.message:"安全治理状态读取失败"}</b><span>未知状态保持阻止，不回退为可用。</span><button type="button" onClick={()=>queries.forEach(query=>query.refetch())}>重新读取</button></div></main>;

  const cb=circuit.data!,exp=exploration.data!,caps=capabilities.data!,f=traffic.data!,g=probes.data!,h=highCost.data!;
  const history=timeline.data!,skills=skillDiscovery.data!,skillRuntime=skillStatus.data!,attrs=attribution.data!;
  const prices=price.data!,audits=priceAudit.data!,timing=overhead.data!;
  const missingAttribution=attrs.items.filter(item=>item.status!=="authoritatively_attributed").length;
  const priceConclusion=prices.status==="fresh"
    ?`价格目录新鲜，可供精确范围的 Scheduler 成本门控使用（${prices.record_count} 条记录）。`
    :prices.status==="stale"?"价格目录陈旧，成本门控保持阻止。"
    :prices.status==="expired"?"价格目录已过期，成本门控保持阻止。"
    :"价格目录不可用，成本门控保持阻止。";
  const chartData=timing.stages.map(item=>({name:stageLabel[item.stage]||item.stage,p50:item.p50_ms,p95:item.p95_ms,p99:item.p99_ms}));
  const rec=reconstruction.data;

  return <main className="governance-workspace">
    <header className="ops-page-header"><div><p className="eyebrow">FAIL-CLOSED · 只读监控</p><h1>安全治理与执行归因</h1><p>展示本地治理、价格版本、Scheduler 开销和可复核的决策重建；本页不授权任何真实平台执行。</p></div></header>
    <section className="ops-metrics" aria-label="治理摘要">
      <article className="ops-metric"><header>ADV-017</header><div><strong>{f.enabled?"沙箱":"禁用"}</strong></div><small>流量变更记录 {count(f.state_counts)}</small></article>
      <article className="ops-metric"><header>ADV-018</header><div><strong>{g.enabled?"Mock":"禁用"}</strong></div><small>活动租约 {g.active_lease_count}</small></article>
      <article className="ops-metric"><header>ADV-019</header><div><strong>{h.enabled?"Mock":"禁用"}</strong></div><small>高成本测试 {count(h.state_counts)}</small></article>
      <article className="ops-metric"><header>价格新鲜度</header><div><strong>{prices.status==="fresh"?"新鲜":"阻止"}</strong></div><small>{prices.catalog_version||"版本待确认"}</small></article>
      <article className="ops-metric"><header>开销样本</header><div><strong>{timing.sample_count}</strong></div><small>最近 {timing.window_minutes} 分钟</small></article>
      <article className="ops-metric"><header>缺少执行归因</header><div><strong>{missingAttribution}</strong></div><small>不推断实际渠道</small></article>
    </section>

    <section className="ops-section" aria-labelledby="price-title"><header><div><h2 id="price-title">价格目录版本与新鲜度</h2><p>只接受已批准价格源中的新鲜、精确范围记录；陈旧、过期、未知均阻止成本门控。</p></div><small>{prices.source_id}</small></header>
      <div className={`notice ${prices.status==="fresh"?"":"warning"}`} role="note"><b>{priceConclusion}</b><span>自动同步：{prices.automatic_sync_state||"待确认"}；Scheduler 消费规则：{prices.scheduler_consumption||"待确认（阻止）"}</span></div>
      <div className="table-wrap"><table><caption className="sr-only">价格目录状态</caption><thead><tr><th>状态</th><th>目录版本</th><th>记录数</th><th>获取时间</th><th>最近错误</th></tr></thead><tbody><tr><td>{prices.status==="fresh"?"新鲜":prices.status==="stale"?"陈旧（阻止）":prices.status==="expired"?"已过期（阻止）":"不可用（阻止）"}</td><td>{prices.catalog_version||"待确认"}</td><td>{prices.record_count}</td><td>{formatTime(prices.fetched_at)}</td><td>{prices.last_error_code||"无已记录错误"}</td></tr></tbody></table></div>
      <h3>价格审计事件</h3>
      {!audits.items.length?<div className="state empty"><b>尚无价格审计事件</b><span>不生成演示记录，也不推断价格版本。</span></div>:<div className="table-wrap"><table><caption className="sr-only">价格目录审计事件</caption><thead><tr><th>时间</th><th>事件</th><th>来源</th><th>审计编号</th></tr></thead><tbody>{audits.items.map(item=><tr key={item.audit_id}><td>{formatTime(item.created_at)}</td><td>{item.event_type}</td><td>{item.source_id}</td><td><code>{item.audit_id}</code></td></tr>)}</tbody></table></div>}
    </section>

    <section className="ops-section" aria-labelledby="overhead-title"><header><div><h2 id="overhead-title">Scheduler 分阶段开销</h2><p>p50、p95、p99 仅覆盖 Scheduler 计算与本地持久化，不包含供应商网络、流式生成或客户端渲染。</p></div><small>{timing.sample_count} 个样本</small></header>
      {timing.status==="never_measured"?<div className="state empty"><b>尚无 Scheduler 开销样本</b><span>所有分位数保持待确认，不以零值代替。</span></div>:<><div className="table-wrap" aria-label="Scheduler 分阶段延迟柱状图"><BarChart width={760} height={280} data={chartData} margin={{top:12,right:12,left:4,bottom:40}}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="name" angle={-18} textAnchor="end" interval={0}/><YAxis unit=" ms"/><Tooltip/><Legend/><Bar dataKey="p50" name="p50" fill="#2f7d71"/><Bar dataKey="p95" name="p95" fill="#d29135"/><Bar dataKey="p99" name="p99" fill="#b84a54"/></BarChart></div><div className="table-wrap"><table><caption className="sr-only">Scheduler 分阶段延迟分位数</caption><thead><tr><th>阶段</th><th>样本</th><th>p50</th><th>p95</th><th>p99</th><th>最大值</th></tr></thead><tbody>{timing.stages.map(item=><tr key={item.stage}><td>{stageLabel[item.stage]||item.stage}</td><td>{item.sample_count}</td><td>{numberValue(item.p50_ms)}</td><td>{numberValue(item.p95_ms)}</td><td>{numberValue(item.p99_ms)}</td><td>{numberValue(item.max_ms)}</td></tr>)}</tbody></table></div></>}
      <div className="notice" role="note"><b>{timing.provider_latency_included?"包含供应商延迟":"不包含供应商延迟"}</b><span>冷启动样本 {timing.cold_start_sample_count}；排除范围：{timing.excluded_time.join("、")||"待确认"}。</span></div>
    </section>

    <section className="ops-section" aria-labelledby="reconstruction-title"><header><div><h2 id="reconstruction-title">决策重建</h2><p>按持久化证据展示候选、排除、排序、回退、执行尝试与来源哈希，不用当前状态补写历史。</p></div><label>选择决策 <select aria-label="选择决策" value={selectedDecisionId} onChange={event=>setSelectedDecisionId(event.target.value)}><option value="">无可选决策</option>{attrs.items.map(item=><option key={item.decision_id} value={item.decision_id}>{item.decision_id}</option>)}</select></label></header>
      {!selectedDecisionId?<div className="state empty"><b>尚无可重建决策</b><span>不生成候选、排序或执行尝试。</span></div>:reconstruction.isLoading?<div className="state loading" role="status"><b>正在读取决策重建…</b></div>:reconstruction.error?<div className="state error" role="alert"><b>决策重建读取失败</b><span>该决策保持不可复核状态，不推断候选或执行结果。</span></div>:rec?.status!=="ready"?<div className="state error" role="alert"><b>决策重建被阻止</b><span>原因：{rec?.reason||"未知"}；缺失字段：{rec?.missing_fields?.join("、")||"待确认"}。</span></div>:<>
        <p className="governance-inline-summary">候选 {rec.candidate_evaluation?.candidate_count??"待确认"} · 合格 {rec.candidate_evaluation?.eligible_count??"待确认"} · 排除 {rec.candidate_evaluation?.excluded_count??"待确认"} · 执行状态 {rec.execution_trace?.execution_status||"待确认"}</p>
        <div className="table-wrap"><table><caption className="sr-only">候选排序与排除原因</caption><thead><tr><th>排名</th><th>候选</th><th>渠道</th><th>合格</th><th>最终分数</th><th>排除原因</th></tr></thead><tbody>{(rec.candidate_evaluation?.candidate_ranking||[]).map((row,index)=><tr key={`${textValue(row,"candidate_id")}-${index}`}><td>{textValue(row,"rank")}</td><td>{textValue(row,"candidate_id")}</td><td>{textValue(row,"channel_id")}</td><td>{row.eligible===true?"是":row.eligible===false?"否（阻止）":"待确认（阻止）"}</td><td>{textValue(row,"final_score")}</td><td>{textValue(row,"exclusion_reason")}</td></tr>)}</tbody></table></div>
        <h3>回退顺序</h3>{rec.candidate_evaluation?.fallback_order.length?<ol>{rec.candidate_evaluation.fallback_order.map((item,index)=><li key={`${item}-${index}`}>{item}</li>)}</ol>:<p>未记录回退顺序。</p>}
        <h3>执行尝试</h3>{rec.execution_trace?.attempts.length?<div className="table-wrap"><table><caption className="sr-only">决策执行尝试</caption><thead><tr><th>序号</th><th>候选</th><th>渠道</th><th>结果</th><th>错误</th><th>延迟</th><th>网络调用</th></tr></thead><tbody>{rec.execution_trace.attempts.map((row,index)=><tr key={`${textValue(row,"attempt_number")}-${index}`}><td>{textValue(row,"attempt_number")}</td><td>{textValue(row,"candidate_id")}</td><td>{textValue(row,"channel_id")}</td><td>{textValue(row,"result")}</td><td>{textValue(row,"error")}</td><td>{row.latency_ms===null||row.latency_ms===undefined?"待确认":`${row.latency_ms} ms`}</td><td>{row.network_called===true?"是":row.network_called===false?"否":"待确认"}</td></tr>)}</tbody></table></div>:<div className="state empty"><b>未记录执行尝试</b><span>不推断是否发生网络调用。</span></div>}
        <h3>版本与来源</h3><div className="table-wrap"><table><caption className="sr-only">决策重建版本和来源哈希</caption><tbody><tr><th>运行时版本</th><td>{rec.policy_binding?.runtime_version||"待确认"}</td><th>价格目录版本</th><td>{rec.policy_binding?.catalog_version||"待确认"}</td></tr><tr><th>决策策略版本</th><td>{rec.policy_binding?.decision_policy_version||"待确认"}</td><th>目录哈希</th><td><code>{rec.policy_binding?.catalog_sha256||"待确认"}</code></td></tr><tr><th>指标快照</th><td>{rec.policy_binding?.metric_snapshot_ids?.join("、")||"待确认"}</td><th>置信度快照</th><td>{rec.policy_binding?.confidence_snapshot_ids?.join("、")||"待确认"}</td></tr><tr><th>运行记录哈希</th><td><code>{rec.runtime_record_sha256||"待确认"}</code></td><th>重建哈希</th><td><code>{rec.reconstruction_sha256||"待确认"}</code></td></tr></tbody></table></div>
      </>}
    </section>

    <section className="ops-section" aria-labelledby="timeline-title"><header><div><h2 id="timeline-title">治理状态变化时间线</h2><p>仅来自本地持久化审计时间戳，不生成演示数据。</p></div><small>{history.freshness_status==="fresh"?"数据新鲜":history.freshness_status==="stale"?"数据陈旧（阻止）":"新鲜度待确认（阻止）"}</small></header>
      {!history.series.length?<div className="state empty"><b>尚无运行时证据</b><span>首次发生真实的本地治理状态变化后才会出现时间点。</span></div>:<div className="table-wrap"><table><caption className="sr-only">本地治理状态变化时间序列</caption><thead><tr><th>时间</th><th>治理域</th><th>事件</th></tr></thead><tbody>{history.series.map((item,index)=><tr key={`${item.timestamp}-${index}`}><td>{formatTime(item.timestamp)}</td><td>{item.domain}</td><td>{item.event_type}</td></tr>)}</tbody></table></div>}
    </section>

    <section className="ops-section" aria-labelledby="adv017-title"><header><div><h2 id="adv017-title">ADV-017 · 生产流量变更治理</h2><p>双人审批、版本状态机、沙箱灰度、到期回滚和 Kill Switch 的只读投影。</p></div><small>{f.policy_version}</small></header><ModeNotice enabled={f.enabled} mode="offline_sandbox / real_execution_allowed=false"/><p className="governance-inline-summary">灰度上限 {f.maximum_rollout_percent}% · 必需门控 {f.required_gates.join("、")} · 激活 Kill Switch {activeSwitches(f.kill_switches)}</p>{!f.items.length&&<div className="state empty"><b>尚无流量变更提议</b><span>本页不提供提议、审批或执行入口。</span></div>}</section>
    <section className="ops-section" aria-labelledby="adv018-title"><header><div><h2 id="adv018-title">ADV-018 · 持续探针治理</h2><p>审批窗口、额度、退避、停止条件、熔断与租约恢复。</p></div><small>{g.policy_version}</small></header><ModeNotice enabled={g.enabled} mode="mock_only / live_execution_allowed=false"/><p className="governance-inline-summary">有效审批 {g.active_approval_count} · 活动租约 {g.active_lease_count} · 停止范围 {g.stopped_scopes.length} · 激活 Kill Switch {activeSwitches(g.kill_switches)}</p>{!g.items.length&&<div className="state empty"><b>尚无探针租约</b><span>默认禁用；真实探针需要独立授权。</span></div>}</section>
    <section className="ops-section" aria-labelledby="adv019-title"><header><div><h2 id="adv019-title">ADV-019 · 高成本测试治理</h2><p>按环境、模型和币种预留预算；未知币种保持阻止，完成后按实际成本对账。</p></div><small>{h.policy_version}</small></header><ModeNotice enabled={h.enabled} mode="mock_only / real_execution_allowed=false"/><p className="governance-inline-summary">币种 {h.known_currencies.join("、")||"待确认（阻止）"} · 激活 Kill Switch {activeSwitches(h.kill_switches)}</p>{!h.items.length&&<div className="state empty"><b>尚无高成本测试</b><span>预算估算不等于预留；本页不开放审批和执行。</span></div>}</section>
    <section className="ops-section" aria-labelledby="exploration-title"><header><div><h2 id="exploration-title">受控探索 · 影子决策</h2><p>候选范围、审批、预算、健康、熔断、停止条件和 Kill Switch 必须全部通过。</p></div><small>{exp.policy_version}</small></header><ModeNotice enabled={exp.feature_enabled} mode="shadow_only / execution_authorized=false"/><p className="governance-inline-summary">已用请求 {exp.usage.requests} · 已用成本 {exp.usage.cost.toFixed(6)} · 允许熔断状态 {exp.allowed_circuit_states.join("、")||"待确认（阻止）"}</p></section>
    <section className="ops-section" aria-labelledby="cap-title"><header><div><h2 id="cap-title">能力证据与 Formal Agent Skill</h2><p>未确认、冲突、陈旧、过期或撤销的能力证据均保持阻止。</p></div><small>注册表 {caps.registry_version}</small></header><p className="governance-inline-summary">能力证据 {caps.items.length} · Formal Agent Skill {skills.skill_count} · 仓库运行链 {skillRuntime.status==="ready"?"就绪":"阻止"}</p>{!caps.items.length?<div className="state empty"><b>{caps.status==="pending_confirmation"?"能力证据待确认（阻止）":"尚无能力证据"}</b><span>不从模型名称或供应商宣传推断能力。</span></div>:<div className="table-wrap"><table><caption className="sr-only">能力证据</caption><thead><tr><th>主体</th><th>要求</th><th>状态</th><th>验证</th><th>有效期</th></tr></thead><tbody>{caps.items.map(item=><tr key={item.evidence_id}><td>{item.subject_id}<small>{item.subject_version}</small></td><td>{item.requirement}</td><td>{stateLabel[item.state]||item.state}</td><td>{stateLabel[item.verification_status]||item.verification_status}</td><td>{formatTime(item.valid_until)}</td></tr>)}</tbody></table></div>}</section>
    <section className="ops-section" aria-labelledby="circuit-title"><header><div><h2 id="circuit-title">熔断器状态</h2><p>OPEN 阻止流量；HALF_OPEN 仅允许已授权探针。</p></div><small>{cb.policy_version}</small></header>{!cb.items.length?<div className="state empty"><b>尚无熔断器记录</b><span>未知渠道状态保持阻止。</span></div>:<div className="table-wrap"><table><caption className="sr-only">熔断器状态</caption><thead><tr><th>熔断器</th><th>状态</th><th>冷却至</th><th>版本</th></tr></thead><tbody>{cb.items.map(item=><tr key={item.circuit_id}><td>{item.circuit_id}</td><td>{stateLabel[item.state]}</td><td>{formatTime(item.cooldown_until)}</td><td>{item.revision}</td></tr>)}</tbody></table></div>}</section>
    <section className="ops-section" aria-labelledby="attr-title"><header><div><h2 id="attr-title">权威执行归因</h2><p>只有可信执行事件可以确认实际渠道；缺失时明确保持未知。</p></div><small>{attrs.total} 条链</small></header>{!attrs.items.length?<div className="state empty"><b>尚无执行归因链</b><span>不根据推荐渠道推断实际渠道。</span></div>:<div className="table-wrap"><table><caption className="sr-only">调度决策执行归因</caption><thead><tr><th>决策</th><th>状态</th><th>推荐渠道</th><th>权威实际渠道</th><th>执行事件</th></tr></thead><tbody>{attrs.items.map(item=><tr key={item.decision_id}><td><code>{item.decision_id}</code></td><td>{stateLabel[item.status]||item.status}</td><td>{item.scheduler_decision?.selected_channel_id||"待确认"}</td><td>{item.authoritative_actual_channel||"未知（阻止推断）"}</td><td>{item.event_count}</td></tr>)}</tbody></table></div>}</section>
  </main>;
}
