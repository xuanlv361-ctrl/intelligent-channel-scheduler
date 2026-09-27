import {useEffect,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {api,ApiError,type TrafficControlProposal} from "../services/api";
import {EmptyState,ErrorState,PageHeader,Section,StatusBadge,formatLocalTime,formatValue} from "../components/console/ConsoleUI";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

const errorMessage=(error:unknown)=>error instanceof ApiError?error.message:error instanceof Error?error.message:"请求失败";
const stateTone=(state:string):"success"|"warning"|"danger"|"neutral"|"info"=>{
  if(["VALIDATED","APPROVED","COMPLETED"].includes(state))return "success";
  if(["REJECTED","AUTO_STOPPED","ROLLED_BACK"].includes(state))return "danger";
  if(["PENDING_APPROVAL","PAUSED"].includes(state))return "warning";
  if(state==="CANARY_RUNNING")return "info";
  return "neutral";
};
const stateLabel:Record<string,string>={DRAFT:"草稿",VALIDATED:"已校验",PENDING_APPROVAL:"待审批",
  APPROVED:"已批准",CANARY_RUNNING:"灰度运行",PAUSED:"已暂停",COMPLETED:"已完成",
  AUTO_STOPPED:"自动停止",ROLLED_BACK:"已回滚",REJECTED:"已拒绝"};
const percent=(value:number|null|undefined)=>value===null||value===undefined?"无法估算":`${(value*100).toFixed(1)}%`;
const latency=(value:number|null|undefined)=>value===null||value===undefined?"无法估算":`${Math.round(value)} ms`;
const currency=(value:number|null|undefined)=>value===null||value===undefined?"无法估算":`CNY ¥${value.toFixed(6)}`;

export function TrafficGovernanceControlPage(){
  const qc=useQueryClient();
  const current=useQuery({queryKey:["traffic-control-current"],queryFn:({signal})=>api.trafficControlCurrent(signal),refetchInterval:5000});
  const proposals=useQuery({queryKey:["traffic-control-proposals"],queryFn:({signal})=>api.trafficControlProposals(signal),refetchInterval:5000});
  const models=useQuery({queryKey:["traffic-control-models"],queryFn:({signal})=>api.uatModels(false,signal)});
  const channels=useQuery({queryKey:["traffic-control-channels"],queryFn:({signal})=>api.trafficControlChannels(signal)});
  const versions=useQuery({queryKey:["traffic-control-versions"],queryFn:({signal})=>api.configReview(signal)});
  const [environmentMode,setEnvironmentMode]=useState<"sandbox"|"china_uat"|"production">("sandbox");
  const [modelId,setModelId]=useState("");const [channelId,setChannelId]=useState("");
  const [targetPolicy,setTargetPolicy]=useState("");const [rollout,setRollout]=useState(5);
  const [reason,setReason]=useState("");const [rollbackCondition,setRollbackCondition]=useState("");
  const [approvalReference,setApprovalReference]=useState("");const [observationMinutes,setObservationMinutes]=useState(15);
  const [minimumSamples,setMinimumSamples]=useState(10);const [selectedId,setSelectedId]=useState("");
  const [errorRate,setErrorRate]=useState(10);const [successRate,setSuccessRate]=useState(90);
  const [p95Limit,setP95Limit]=useState(10000);const [fallbackLimit,setFallbackLimit]=useState(20);
  const [consecutiveFailures,setConsecutiveFailures]=useState(3);
  useEffect(()=>{
    if(!targetPolicy){
      const active=versions.data?.versions.find(item=>item.is_active);
      setTargetPolicy(active?.configuration_version??current.data?.configuration_version??"");
    }
  },[targetPolicy,versions.data,current.data]);
  const eligibleChannels=(channels.data?.items??[]).filter(item=>!modelId||item.models.includes(modelId));
  useEffect(()=>{
    if(channelId&&!eligibleChannels.some(item=>item.channel_id===channelId))setChannelId("");
  },[modelId,channelId,eligibleChannels]);
  const impact=useQuery({queryKey:["traffic-control-impact",modelId,channelId],
    queryFn:({signal})=>api.trafficControlImpact(modelId,channelId,signal),enabled:Boolean(modelId&&channelId)});
  const selected=(proposals.data?.items??[]).find(item=>item.proposal_id===selectedId)??proposals.data?.items[0];
  useEffect(()=>{
    if(!selectedId&&proposals.data?.items[0])setSelectedId(proposals.data.items[0].proposal_id);
  },[selectedId,proposals.data]);
  const live=useQuery({queryKey:["traffic-control-live",selected?.proposal_id],
    queryFn:({signal})=>api.trafficControlMetrics(selected!.proposal_id,signal),
    enabled:Boolean(selected&&["CANARY_RUNNING","PAUSED","AUTO_STOPPED"].includes(selected.state)),refetchInterval:5000});
  const audit=useQuery({queryKey:["traffic-control-audit",selected?.proposal_id],
    queryFn:({signal})=>api.trafficControlAudit(selected!.proposal_id,signal),enabled:Boolean(selected)});
  const refresh=async()=>Promise.all([
    qc.invalidateQueries({queryKey:["traffic-control-current"]}),
    qc.invalidateQueries({queryKey:["traffic-control-proposals"]}),
    qc.invalidateQueries({queryKey:["traffic-control-live"]}),
    qc.invalidateQueries({queryKey:["traffic-control-audit"]}),
  ]);
  const create=useMutation({mutationFn:()=>api.createTrafficControlProposal({
    environment_mode:environmentMode,source_model_id:current.data?.current_model_id,
    source_channel_id:current.data?.current_channel_id,target_model_id:modelId,
    target_channel_id:channelId,source_policy_version:current.data?.configuration_version,
    target_policy_version:targetPolicy,rollout_percent:rollout,reason,
    approval_reference:approvalReference||null,observation_seconds:observationMinutes*60,
    minimum_sample_count:minimumSamples,rollback_condition:rollbackCondition,
    stop_conditions:{error_rate_above:errorRate/100,success_rate_below:successRate/100,
      p95_latency_ms_above:p95Limit,fallback_rate_above:fallbackLimit/100,
      consecutive_failures:consecutiveFailures},
  }),onSuccess:async value=>{setSelectedId(value.proposal_id);await refresh()}});
  type Action="validate"|"submit"|"approve"|"reject"|"activate"|"pause"|"resume"|"increase"|"decrease"|"rollback"|"complete"|"delete";
  const action=useMutation({mutationFn:async({kind,row}:{kind:Action;row:TrafficControlProposal})=>{
    if(kind==="approve")return api.approveTrafficControlProposal(row.proposal_id,approvalReference);
    if(kind==="reject")return api.rejectTrafficControlProposal(row.proposal_id,"审批未通过风险检查");
    if(kind==="activate")return api.activateTrafficControlProposal(row.proposal_id);
    if(kind==="increase"||kind==="decrease")return api.adjustTrafficControlProposal(row.proposal_id,
      Math.max(1,Math.min(100,row.rollout_percent+(kind==="increase"?5:-5))));
    if(kind==="rollback")return api.rollbackTrafficControlProposal(row.proposal_id,"操作员执行回滚");
    if(kind==="delete")return api.deleteTrafficControlProposal(row.proposal_id);
    return api.trafficControlAction(row.proposal_id,kind);
  },onSuccess:refresh});
  const formReady=Boolean(modelId&&channelId&&targetPolicy&&reason.trim()&&rollbackCondition.trim()&&rollout>0&&minimumSamples>0);
  const operationError=create.error||action.error;
  const renderActions=(row:TrafficControlProposal)=><div className="traffic-row-actions">
    {row.state==="DRAFT"&&<><button onClick={()=>action.mutate({kind:"validate",row})}>校验变更</button><button className="secondary" onClick={()=>action.mutate({kind:"delete",row})}>删除草稿</button></>}
    {row.state==="VALIDATED"&&<button onClick={()=>action.mutate({kind:"submit",row})}>提交审批</button>}
    {row.state==="PENDING_APPROVAL"&&<><button onClick={()=>action.mutate({kind:"approve",row})}>批准</button><button className="secondary" onClick={()=>action.mutate({kind:"reject",row})}>拒绝</button></>}
    {row.state==="APPROVED"&&<button onClick={()=>action.mutate({kind:"activate",row})}>开始灰度</button>}
    {row.state==="CANARY_RUNNING"&&<><button onClick={()=>action.mutate({kind:"pause",row})}>暂停</button><button className="secondary" onClick={()=>action.mutate({kind:"increase",row})}>增加 5%</button><button className="secondary" onClick={()=>action.mutate({kind:"decrease",row})}>减少 5%</button><button className="danger" onClick={()=>action.mutate({kind:"rollback",row})}>立即回滚</button><button className="secondary" onClick={()=>action.mutate({kind:"complete",row})}>完成发布</button></>}
    {row.state==="PAUSED"&&<><button onClick={()=>action.mutate({kind:"resume",row})}>继续灰度</button><button className="danger" onClick={()=>action.mutate({kind:"rollback",row})}>回滚</button></>}
    {row.state==="AUTO_STOPPED"&&<button className="danger" onClick={()=>action.mutate({kind:"rollback",row})}>确认回滚</button>}
  </div>;
  return <div className="traffic-control-page">
    <PageHeader title="流量切换" description="创建、验证、审批、灰度、监控和回滚模型与渠道流量变更。"/>
    <div className="traffic-mode-strip"><span><b>当前模式</b> 沙箱验证</span><span><b>国内 UAT</b> 可选</span><span><b>生产执行</b> 未配置</span><span><b>数据更新</b> {formatLocalTime(current.data?.data_updated_at)}</span></div>
    <Section title="当前流量" description="来自当前持久化策略与去重后的统一真实调用日志。">
      {current.error?<ErrorState title="当前流量读取失败" description={errorMessage(current.error)} onRetry={()=>current.refetch()}/>:current.data?.metrics_24h.request_count?<>
        <div className="traffic-current-grid"><div><small>策略版本</small><b>{formatValue(current.data.configuration_version)}</b></div><div><small>当前模型</small><b>{formatValue(current.data.current_model_id)}</b></div><div><small>权威渠道</small><b>{formatValue(current.data.current_channel_id)}</b></div><div><small>24 小时请求</small><b>{current.data.metrics_24h.request_count}</b></div><div><small>成功率</small><b>{percent(current.data.metrics_24h.success_rate)}</b></div><div><small>P95 延迟</small><b>{latency(current.data.metrics_24h.p95_latency_ms)}</b></div><div><small>Token</small><b>{formatValue(current.data.metrics_24h.token_count)}</b></div><div><small>费用</small><b>{currency(current.data.metrics_24h.total_cost)}</b></div><div><small>Fallback</small><b>{percent(current.data.metrics_24h.fallback_rate)}</b></div></div>
        {current.data.traffic_distribution.length>0&&<div className="traffic-distribution">{current.data.traffic_distribution.slice(0,5).map(item=><div key={`${item.model_id}-${item.channel_id}`}><span>{item.model_id}<small>{item.channel_id}</small></span><i><em style={{width:`${item.percentage}%`}}/></i><b>{item.percentage}%</b></div>)}</div>}
      </>:<div className="traffic-inline-empty">暂无真实流量数据。完成国内 UAT 调用并获得权威渠道证据后，这里会自动更新。</div>}
    </Section>
    <Section title="创建变更" description="左右对照当前配置与目标配置；目标选项只来自真实模型目录和权威渠道证据。">
      <div className="traffic-environment-tabs"><button className={environmentMode==="sandbox"?"active":""} onClick={()=>setEnvironmentMode("sandbox")}>沙箱验证</button><button className={environmentMode==="china_uat"?"active":""} onClick={()=>setEnvironmentMode("china_uat")}>国内 UAT</button><button disabled title="生产适配器未配置">正式环境</button></div>
      <div className="traffic-compare-editor"><div className="traffic-config-panel current"><span className="traffic-panel-label">当前配置</span><dl><div><dt>模型</dt><dd>{formatValue(current.data?.current_model_id)}</dd></div><div><dt>渠道</dt><dd>{formatValue(current.data?.current_channel_id)}</dd></div><div><dt>策略</dt><dd>{formatValue(current.data?.configuration_version)}</dd></div><div><dt>流量</dt><dd>观测分布</dd></div></dl></div><div className="traffic-direction" aria-hidden="true">→</div><div className="traffic-config-panel target"><span className="traffic-panel-label">目标配置</span><label>真实模型<select value={modelId} onChange={event=>setModelId(event.target.value)}><option value="">选择 UAT 模型</option>{models.data?.models.map(item=><option value={item.id} key={item.id}>{item.id}</option>)}</select></label><label>真实渠道<select value={channelId} onChange={event=>setChannelId(event.target.value)} disabled={!modelId||eligibleChannels.length===0}><option value="">{eligibleChannels.length?"选择权威渠道":"暂无匹配渠道证据"}</option>{eligibleChannels.map(item=><option value={item.channel_id} key={item.channel_id}>{item.channel_name||item.channel_id} · {item.sample_count} 样本</option>)}</select></label><label>目标策略<select value={targetPolicy} onChange={event=>setTargetPolicy(event.target.value)}><option value="">选择真实配置版本</option>{versions.data?.versions.map(item=><option value={item.configuration_version} key={item.configuration_version}>{item.configuration_version}{item.is_active?"（当前）":""}</option>)}</select></label></div></div>
      {models.error&&<p className="traffic-warning">真实模型目录读取失败：{errorMessage(models.error)}</p>}{channels.data?.total===0&&<p className="traffic-warning">当前没有权威 channel_id，不能创建真实渠道灰度；系统不会根据模型名称猜测渠道。</p>}
      <div className="traffic-rollout"><label>灰度比例 <b>{rollout}%</b><input type="range" min="1" max="100" value={rollout} onChange={event=>setRollout(Number(event.target.value))}/></label><div>{[1,5,10,25,50,100].map(value=><button type="button" className={rollout===value?"active":""} onClick={()=>setRollout(value)} key={value}>{value}%</button>)}</div></div>
      <div className="traffic-form-grid"><label>变更原因<textarea rows={3} value={reason} onChange={event=>setReason(event.target.value)} placeholder="说明这次变更解决什么问题"/></label><label>回滚条件<textarea rows={3} value={rollbackCondition} onChange={event=>setRollbackCondition(event.target.value)} placeholder="说明何时必须回滚"/></label><label>观察时长（分钟）<input type="number" min="1" value={observationMinutes} onChange={event=>setObservationMinutes(Number(event.target.value))}/></label><label>最小样本数<input type="number" min="1" value={minimumSamples} onChange={event=>setMinimumSamples(Number(event.target.value))}/></label><label>审批引用（生产必填）<input value={approvalReference} onChange={event=>setApprovalReference(event.target.value)} placeholder="沙箱和 UAT 可留空"/></label></div>
      <details className="traffic-stop-editor" open><summary>自动停止条件</summary><div><label>错误率超过<input type="number" min="0" max="100" value={errorRate} onChange={event=>setErrorRate(Number(event.target.value))}/><span>%</span></label><label>成功率低于<input type="number" min="0" max="100" value={successRate} onChange={event=>setSuccessRate(Number(event.target.value))}/><span>%</span></label><label>P95 延迟超过<input type="number" min="1" value={p95Limit} onChange={event=>setP95Limit(Number(event.target.value))}/><span>ms</span></label><label>Fallback 率超过<input type="number" min="0" max="100" value={fallbackLimit} onChange={event=>setFallbackLimit(Number(event.target.value))}/><span>%</span></label><label>连续失败<input type="number" min="1" value={consecutiveFailures} onChange={event=>setConsecutiveFailures(Number(event.target.value))}/><span>次</span></label></div></details>
      <div className="traffic-action-bar"><span>{!modelId?"尚未选择目标模型":!channelId?"目标渠道未确认":!targetPolicy?"尚未选择目标策略":!reason.trim()||!rollbackCondition.trim()?"请填写变更与回滚原因":impact.data?.status==="insufficient_evidence"&&environmentMode!=="sandbox"?"影响评估证据不足":"可以创建变更草稿"}</span><button disabled={!formReady||create.isPending||(environmentMode!=="sandbox"&&impact.data?.status!=="ready")} onClick={()=>create.mutate()}>保存变更草稿</button></div>
      {operationError&&<ErrorState title="流量变更操作失败" description={errorMessage(operationError)}/>} 
    </Section>
    <Section title="影响预估" description="使用真实历史日志中目标模型与渠道的匹配样本；缺少证据时不显示 0。">
      {!modelId||!channelId?<div className="traffic-inline-empty">选择目标模型和真实渠道后生成影响预估。</div>:impact.isLoading?<p role="status">正在计算影响…</p>:impact.error?<ErrorState title="影响预估失败" description={errorMessage(impact.error)}/>:<div className="table-scroll"><table className="console-table traffic-impact-table"><thead><tr><th>指标</th><th>当前配置</th><th>目标估算</th><th>差异</th><th>数据覆盖率</th></tr></thead><tbody><tr><td>成功率</td><td>{percent(impact.data?.baseline.success_rate)}</td><td>{percent(impact.data?.target.success_rate)}</td><td>{percent(impact.data?.difference.success_rate)}</td><td rowSpan={5}>{percent(impact.data?.coverage_rate)}</td></tr><tr><td>P95 延迟</td><td>{latency(impact.data?.baseline.p95_latency_ms)}</td><td>{latency(impact.data?.target.p95_latency_ms)}</td><td>{latency(impact.data?.difference.p95_latency_ms)}</td></tr><tr><td>单次费用</td><td>{currency(impact.data?.baseline.average_cost)}</td><td>{currency(impact.data?.target.average_cost)}</td><td>{currency(impact.data?.difference.average_cost)}</td></tr><tr><td>Fallback 率</td><td>{percent(impact.data?.baseline.fallback_rate)}</td><td>{percent(impact.data?.target.fallback_rate)}</td><td>{percent(impact.data?.difference.fallback_rate)}</td></tr><tr><td>渠道集中度</td><td>无法估算</td><td>{impact.data?.channel_concentration===null?"无法估算":impact.data?.channel_concentration.toFixed(3)}</td><td>—</td></tr></tbody></table>{impact.data?.limitations.map(item=><p className="traffic-warning" key={item}>{item}</p>)}</div>}
    </Section>
    <Section title="审批与执行" description="状态转换由后端校验并持久化；刷新页面不会丢失。">
      {selected?<><div className="traffic-lifecycle" aria-label="变更生命周期">{["DRAFT","VALIDATED","PENDING_APPROVAL","APPROVED","CANARY_RUNNING","PAUSED","COMPLETED"].map((state,index)=><span className={selected.state===state?"current":""} key={state}><i>{index+1}</i>{stateLabel[state]}</span>)}</div><div className="traffic-selected"><div><small>当前变更</small><b>{selected.proposal_id}</b><StatusBadge tone={stateTone(selected.state)}>{stateLabel[selected.state]}</StatusBadge></div>{renderActions(selected)}</div></>:<EmptyState title="暂无流量变更记录" description="选择真实目标后创建第一份变更草稿。"/>}
    </Section>
    <Section title="实时监控" description="灰度运行期间每 5 秒读取统一调用日志，并评估自动停止条件。">
      {!selected||!["CANARY_RUNNING","PAUSED","AUTO_STOPPED"].includes(selected.state)?<div className="traffic-inline-empty">变更进入灰度运行后显示实时指标。</div>:live.error?<ErrorState title="灰度指标读取失败" description={errorMessage(live.error)} onRetry={()=>live.refetch()}/>:<><div className="traffic-live-grid"><div><small>灰度比例</small><b>{selected.rollout_percent}%</b></div><div><small>请求数</small><b>{live.data?.metrics.request_count??0}</b></div><div><small>成功率</small><b>{percent(live.data?.metrics.success_rate)}</b></div><div><small>错误率</small><b>{percent(live.data?.metrics.error_rate)}</b></div><div><small>P95</small><b>{latency(live.data?.metrics.p95_latency_ms)}</b></div><div><small>Token</small><b>{formatValue(live.data?.metrics.token_count)}</b></div><div><small>费用</small><b>{currency(live.data?.metrics.total_cost)}</b></div><div><small>Fallback</small><b>{percent(live.data?.metrics.fallback_rate)}</b></div></div>{live.data?.triggers.length?<div className="traffic-stop-alert"><b>已触发自动停止条件</b>{live.data.triggers.map(item=><span key={item.condition}>{item.condition}：实际 {item.actual} / 阈值 {item.threshold}</span>)}</div>:<p className="traffic-ok">当前未触发自动停止条件 · 最近采集 {formatLocalTime(live.data?.collected_at)}</p>}</>}
    </Section>
    <Section title="变更记录" description="点击记录查看审批、灰度、停止与回滚审计。">
      {proposals.error?<ErrorState title="变更记录读取失败" description={errorMessage(proposals.error)} onRetry={()=>proposals.refetch()}/>:proposals.data?.items.length?<div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>变更 ID</th><th>环境</th><th>当前 → 目标</th><th>灰度</th><th>状态</th><th>操作</th></tr></thead><tbody>{proposals.data.items.map(row=><tr key={row.proposal_id} className={selected?.proposal_id===row.proposal_id?"selected-row":""}><td>{formatLocalTime(row.created_at)}</td><td><button className="text-button" onClick={()=>setSelectedId(row.proposal_id)}>{row.proposal_id}</button></td><td>{row.environment_mode==="sandbox"?"沙箱验证":row.environment_mode==="china_uat"?"国内 UAT":"正式环境"}</td><td>{formatValue(row.source_model_id)} → {row.target_model_id}<small>{formatValue(row.source_channel_id)} → {row.target_channel_id}</small></td><td>{row.rollout_percent}%</td><td><StatusBadge tone={stateTone(row.state)}>{stateLabel[row.state]}</StatusBadge></td><td><button className="text-button" onClick={()=>setSelectedId(row.proposal_id)}>查看详情</button></td></tr>)}</tbody></table></div>:<div className="traffic-inline-empty">暂无流量变更记录。</div>}
    </Section>
    {selected&&<aside className="traffic-detail-drawer" aria-label="流量变更详情"><header><div><small>变更详情</small><h2>{selected.proposal_id}</h2></div><button aria-label="关闭详情" onClick={()=>setSelectedId("")}>×</button></header><dl><div><dt>操作者</dt><dd>{selected.operator_id??selected.proposer_id}</dd></div><div><dt>当前配置</dt><dd>{formatValue(selected.source_model_id)} / {formatValue(selected.source_channel_id)}</dd></div><div><dt>目标配置</dt><dd>{selected.target_model_id} / {selected.target_channel_id}</dd></div><div><dt>策略变化</dt><dd>{formatValue(selected.source_policy_version)} → {selected.target_policy_version}</dd></div><div><dt>灰度比例</dt><dd>{selected.rollout_percent}%</dd></div><div><dt>Request ID</dt><dd>{selected.request_ids.length?selected.request_ids.join("、"):"未关联"}</dd></div><div><dt>Decision ID</dt><dd>{selected.decision_ids.length?selected.decision_ids.join("、"):"未关联"}</dd></div><div><dt>回滚条件</dt><dd>{selected.rollback_condition}</dd></div></dl><h3>生命周期时间线</h3><ol className="traffic-audit-timeline">{audit.data?.items.map(item=><li key={item.audit_id}><span>{formatLocalTime(item.created_at)}</span><b>{item.event_type}</b><small>{item.actor_id}</small></li>)}</ol><details><summary>技术详情</summary><dl><div><dt>修订</dt><dd>{selected.revision}</dd></div><div><dt>治理策略</dt><dd>{selected.policy_version}</dd></div><div><dt>审批 ID</dt><dd>{formatValue(selected.approval_id)}</dd></div></dl></details></aside>}
    <div className="traffic-skill-bridge"><BusinessSkillAction skillId="propose-traffic-switch" label="通过 Agent Skill 创建治理提案" argumentsValue={{source_policy_version:current.data?.configuration_version??"未记录",target_policy_version:targetPolicy,requested_percentage:rollout,environment:"china_uat",reason,rollback_condition:rollbackCondition}} disabledReason={targetPolicy&&reason&&rollbackCondition?undefined:"请先完成目标策略、原因和回滚条件"} confirmation/></div>
  </div>;
}
