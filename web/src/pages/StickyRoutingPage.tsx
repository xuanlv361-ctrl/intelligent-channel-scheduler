import {useEffect,useRef,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {
  api,ApiError,EnvironmentFilter,StickyBinding,
} from "../services/api";

const stateLabel:Record<string,string>={
  ACTIVE:"生效中",STALE:"已到期，等待安全回收",EXPIRED:"已过期",
  INVALIDATED:"已失效",INTERRUPTED:"已中断",
};
const visibleState=(item:StickyBinding)=>
  item.state==="ACTIVE"&&item.remaining_seconds<=0?"STALE":item.state;
const duration=(seconds:number)=>{
  if(seconds<=0)return "0 秒";
  if(seconds>=3600)return `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
  if(seconds>=60)return `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
  return `${seconds} 秒`;
};
const newIdempotencyKey=()=>globalThis.crypto?.randomUUID?.()||
  `sticky-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const blockedReason=(reason:string|null)=>{
  if(reason==="sticky_policy_disabled")return "策略默认关闭，尚未由运维显式启用。";
  if(reason==="sticky_eligibility_provider_unavailable")
    return "权威资格门控提供器尚未配置，粘性复用保持关闭。";
  return "服务端 HMAC 密钥材料尚未配置。";
};

export default function StickyRoutingPage({environment}:{environment:EnvironmentFilter}){
  const client=useQueryClient();
  const [state,setState]=useState("");
  const [model,setModel]=useState("");
  const [channel,setChannel]=useState("");
  const [offset,setOffset]=useState(0);
  const [selected,setSelected]=useState<StickyBinding|null>(null);
  const [confirming,setConfirming]=useState(false);
  const [confirmation,setConfirmation]=useState("");
  const invalidationKey=useRef("");
  const limit=25;
  const environmentId=environment==="all"?"":environment;
  const status=useQuery({queryKey:["sticky-status"],queryFn:({signal})=>api.stickyStatus(signal)});
  const bindings=useQuery({
    queryKey:["sticky-bindings",environmentId,state,model,channel,offset],
    queryFn:({signal})=>api.stickyBindings({
      environment_id:environmentId,state,model,channel,limit,offset,
    },signal),
    placeholderData:previous=>previous,
  });
  const metrics=useQuery({
    queryKey:["sticky-metrics",environmentId],
    queryFn:({signal})=>api.stickyMetrics(environmentId||undefined,signal),
  });
  const invalidate=useMutation({
    mutationFn:(item:StickyBinding)=>api.invalidateStickyBinding(
      item.sticky_binding_id,item.environment_id,"operator_confirmed_invalidation",
      invalidationKey.current),
    onSuccess:result=>{
      setSelected(result.binding);setConfirming(false);setConfirmation("");
      invalidationKey.current="";
      client.invalidateQueries({queryKey:["sticky-bindings"]});
      client.invalidateQueries({queryKey:["sticky-status"]});
      client.invalidateQueries({queryKey:["sticky-metrics"]});
    },
  });
  useEffect(()=>{
    setSelected(null);
    setConfirming(false);
    setConfirmation("");
    invalidationKey.current="";
  },[environment]);
  if(status.isLoading||bindings.isLoading||metrics.isLoading)
    return <div className="state loading" role="status"><b>正在读取持久化粘性路由</b><span>仅查询本地 SQLite，不访问 UAT 或模型 API。</span></div>;
  const error=status.error||bindings.error||metrics.error;
  if(error)return <div className="state error" role="alert">
    <b>{error instanceof ApiError?error.message:"粘性路由读取失败"}</b>
    <span>请检查最新本地 FastAPI 进程与数据库迁移状态。</span>
    <button onClick={()=>{status.refetch();bindings.refetch();metrics.refetch();}}>重新读取</button>
  </div>;
  const statusData=status.data!;
  const data=bindings.data!;
  const metricData=metrics.data!;
  return <div className="sticky-workspace">
    <header className="ops-page-header">
      <div><p className="eyebrow">POLICY-AWARE AFFINITY</p><h1>粘性路由绑定</h1>
        <p>在安全、能力、环境、预算、健康、置信度、新鲜度和熔断门控全部通过后，复用本地持久化绑定；任何上游阻止条件都优先于粘性命中。</p></div>
    </header>
    {statusData.status==="blocked"&&<section className="blocked" role="status">
      当前粘性路由处于阻止状态：{blockedReason(statusData.blocked_reason)}
      现有 Scheduler 主线继续按原策略运行，不会静默启用粘性复用。
    </section>}
    <section className="ops-metrics">
      <article className="ops-metric"><header>绑定总数</header><div><strong>{data.total}</strong></div><small>本地持久化记录</small></article>
      <article className="ops-metric"><header>ACTIVE</header><div><strong>{statusData.state_counts.ACTIVE||0}</strong></div><small>当前可命中的绑定</small></article>
      <article className="ops-metric"><header>命中率</header><div><strong>{metricData.hit_rate===null?"待观察":`${(metricData.hit_rate*100).toFixed(1)}%`}</strong></div><small>HIT / (HIT + MISS)</small></article>
      <article className="ops-metric"><header>中断</header><div><strong>{metricData.outcomes.INTERRUPTED||0}</strong></div><small>安全或资格变化</small></article>
      <article className="ops-metric"><header>TTL</header><div><strong>{duration(statusData.ttl_seconds)}</strong></div><small>滑动续期</small></article>
      <article className="ops-metric"><header>最长持续</header><div><strong>{duration(statusData.maximum_total_duration_seconds)}</strong></div><small>不可越过的硬上限</small></article>
    </section>
    <section className="panel">
      <h2>筛选绑定</h2>
      <div className="form-grid">
        <label>环境<input aria-label="绑定环境筛选" value={environmentId||"全部环境"} disabled/></label>
        <label>状态<select aria-label="绑定状态筛选" value={state} onChange={event=>{setState(event.target.value);setOffset(0);}}>
          <option value="">全部状态</option>{Object.entries(stateLabel).map(([value,label])=><option key={value} value={value}>{label}</option>)}
        </select></label>
        <label>模型<input aria-label="绑定模型筛选" value={model} onChange={event=>{setModel(event.target.value);setOffset(0);}} placeholder="精确模型 ID"/></label>
        <label>渠道<input aria-label="绑定渠道筛选" value={channel} onChange={event=>{setChannel(event.target.value);setOffset(0);}} placeholder="精确渠道 ID"/></label>
      </div>
    </section>
    {data.items.length===0?<div className="state empty">
      <b>{statusData.status==="blocked"?"当前没有可展示的粘性绑定":"筛选范围内暂无绑定"}</b>
      <span>空状态不会生成演示绑定，也不会用默认渠道伪装真实数据。</span>
    </div>:<section className="table-wrap">
      <table><thead><tr><th>状态 / 环境</th><th>模型 / 渠道</th><th>剩余时间</th><th>命中</th><th>决策与证据</th><th>来源</th><th>操作</th></tr></thead>
      <tbody>{data.items.map(item=><tr key={item.sticky_binding_id}>
        <td><b className={`health-${visibleState(item)==="ACTIVE"?"healthy":"stale"}`}>{stateLabel[visibleState(item)]}</b><small>{item.environment_id}</small></td>
        <td><b>{item.requested_model}</b><small>渠道 {item.selected_channel}</small></td>
        <td>{duration(item.remaining_seconds)}<small>至 {item.expires_at}</small></td>
        <td>{item.hit_count}</td>
        <td><code>{item.latest_decision_id}</code><small>{item.metric_snapshot_id||"指标快照待确认"} · {item.confidence_snapshot_id||"置信度快照待确认"}</small></td>
        <td>{item.is_mock?"Mock / 本地":"非 Mock 结构化证据"}<small>{item.evidence_source} · 指纹 {item.safe_route_key_fingerprint}</small></td>
        <td><button className="secondary" onClick={()=>{setSelected(item);setConfirming(false);invalidationKey.current="";}}>查看</button></td>
      </tr>)}</tbody></table>
      <div className="pagination"><span>第 {offset+1}–{offset+data.items.length} 条，共 {data.total} 条</span>
        <button className="secondary" disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-limit))}>上一页</button>
        <button className="secondary" disabled={!data.has_more} onClick={()=>setOffset(offset+limit)}>下一页</button>
      </div>
    </section>}
    <section className="panel">
      <h2>中断原因</h2>
      {metricData.interruption_reasons.length===0?<p>尚无中断事件。</p>:<ul>{metricData.interruption_reasons.map(item=>
        <li key={item.reason}>{item.reason||"未分类"}：{item.count}</li>)}</ul>}
      <details className="policy-drawer"><summary>策略与门控技术详情</summary>
        <dl><dt>策略版本</dt><dd>{statusData.policy_version}</dd>
          <dt>配置版本</dt><dd>{statusData.configuration_version}</dd>
          <dt>能力范围版本</dt><dd>{statusData.capability_scope_version}</dd>
          <dt>HMAC 派生</dt><dd>{statusData.route_key_derivation_version}</dd>
          <dt>门控顺序</dt><dd>{statusData.required_gate_order.join(" → ")}</dd>
          <dt>网络调用</dt><dd>否</dd></dl>
      </details>
    </section>
    {selected&&<aside className="drawer" aria-label="粘性绑定详情">
      <button aria-label="关闭详情" onClick={()=>{setSelected(null);setConfirming(false);invalidationKey.current="";}}>×</button>
      <h2>绑定详情</h2><p><b>{stateLabel[visibleState(selected)]}</b> · {selected.environment_id}</p>
      <dl className="result-grid">
        <dt>Binding ID</dt><dd>{selected.sticky_binding_id}</dd>
        <dt>安全指纹</dt><dd>{selected.safe_route_key_fingerprint}</dd>
        <dt>模型 / 渠道</dt><dd>{selected.requested_model} / {selected.selected_channel}</dd>
        <dt>创建 / 到期</dt><dd>{selected.created_at}<br/>{selected.expires_at}</dd>
        <dt>最长到期</dt><dd>{selected.maximum_expires_at}</dd>
        <dt>Decision ID</dt><dd>{selected.originating_decision_id}<br/>{selected.latest_decision_id}</dd>
        <dt>Snapshot IDs</dt><dd>{selected.metric_snapshot_id||"待确认"}<br/>{selected.confidence_snapshot_id||"待确认"}</dd>
        <dt>Evidence IDs</dt><dd>{selected.evidence_ids.length?selected.evidence_ids.join(", "):"无持久化证据引用"}</dd>
        <dt>证据来源</dt><dd>{selected.evidence_source}</dd>
        <dt>中断 / 失效原因</dt><dd>{selected.interruption_reason||selected.invalidation_reason||"无"}</dd>
        <dt>Mock 状态</dt><dd>{selected.is_mock?"Mock":"非 Mock"}</dd>
      </dl>
      {selected.state==="ACTIVE"&&!confirming&&<button onClick={()=>{invalidationKey.current=newIdempotencyKey();setConfirming(true);}}>使绑定失效</button>}
      {confirming&&<div className="danger-notice notice">
        <b>确认使绑定失效</b><p>此操作只修改本地绑定，不更改 UAT 平台渠道、模型或配置。请输入：我确认使此粘性路由绑定失效</p>
        <input aria-label="粘性绑定失效确认文本" value={confirmation} onChange={event=>setConfirmation(event.target.value)}/>
        <div className="uat-actions"><button disabled={confirmation!=="我确认使此粘性路由绑定失效"||invalidate.isPending} onClick={()=>invalidate.mutate(selected)}>确认失效</button>
          <button className="secondary" onClick={()=>{setConfirming(false);setConfirmation("");invalidationKey.current="";invalidate.reset();}}>取消</button></div>
        {invalidate.error&&<p role="alert">{invalidate.error instanceof ApiError?invalidate.error.message:"失效操作失败"}</p>}
      </div>}
    </aside>}
  </div>;
}
