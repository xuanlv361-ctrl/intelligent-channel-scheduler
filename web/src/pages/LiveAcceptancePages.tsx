import {useQuery} from "@tanstack/react-query";
import {api} from "../services/api";

const value=(input:unknown,empty="未记录")=>input===null||input===undefined||input===""?empty:String(input);
const percent=(input:unknown)=>typeof input==="number"?`${(input*100).toFixed(1)}%`:"无法计算";
const money=(input:unknown)=>input===null||input===undefined?"待 Provider 日志同步":`¥${input}`;
const ms=(input:unknown)=>typeof input==="number"?`${Math.round(input)} ms`:"未记录";
const seconds=(input:unknown)=>typeof input==="number"?input.toFixed(1):value(input);

type StrategySummary={
  request_count:number; success_rate:number|null; p50_ms:number|null; p95_ms:number|null;
  p99_ms:number|null; actual_provider_cost:string|null; actual_cost_coverage:number;
  estimated_versioned_price:string|null; estimated_cost_coverage:number;
  fallback_rate:number|null; assertion_pass_rate:number|null;
  selection_changes_from_baseline?:number; metric_coverage?:number; confidence?:string;
  model_distribution:Record<string,number>;
};

const strategyNames:Record<string,string>={current_actual:"当前实际",latency_first:"延迟优先",cost_first:"成本优先"};

export function StrategyEffectPage(){
  const query=useQuery({queryKey:["latest-strategy-effect"],queryFn:({signal})=>api.latestStrategyEffect(signal),refetchInterval:5000});
  const item=query.data?.item as Record<string,unknown>|undefined;
  const summary=(item?.summary??{}) as {strategies?:Record<string,StrategySummary>};
  const strategies=summary.strategies??{};
  const rows:Array<[string,(row:StrategySummary)=>string|number]>=[
    ["请求数",row=>row.request_count], ["成功率",row=>percent(row.success_rate)],
    ["P50",row=>ms(row.p50_ms)], ["P95",row=>ms(row.p95_ms)], ["P99",row=>ms(row.p99_ms)],
    ["Provider 实际费用",row=>money(row.actual_provider_cost)],
    ["版本化估算费用",row=>money(row.estimated_versioned_price)],
    ["实际费用覆盖率",row=>percent(row.actual_cost_coverage)],
    ["估算费用覆盖率",row=>percent(row.estimated_cost_coverage)],
    ["Fallback 率",row=>percent(row.fallback_rate)],
    ["断言通过率",row=>percent(row.assertion_pass_rate)],
    ["相对基线选择变化",row=>row.selection_changes_from_baseline??0],
    ["指标覆盖率",row=>percent(row.metric_coverage)],
  ];
  return <main className="page-content live-acceptance-page">
    <header className="page-header"><div><p className="eyebrow">REAL UAT · STRATEGY EFFECT</p><h1>真实策略效果</h1><p>使用同一组固定请求，交错比较当前策略、延迟优先和成本优先。</p></div></header>
    {!item?<section className="state empty"><b>暂无真实策略效果运行</b><span>完成真实 UAT 策略验收后在此展示。</span></section>:<>
      <section className="compact-source"><b>运行 ID：{value(item.strategy_effect_run_id)}</b><span>状态：{value(item.status)}</span><span>进度：{value(item.completed_requests)} / {value(item.planned_requests)}</span><span>价格版本：{value(item.price_version)}</span></section>
      <section className="section"><h2>三策略对比</h2><div className="table-scroll"><table className="console-table"><thead><tr><th>指标</th>{Object.values(strategyNames).map(name=><th key={name}>{name}</th>)}</tr></thead><tbody>
        {rows.map(([label,format])=><tr key={label}><th>{label}</th>{Object.keys(strategyNames).map(name=><td key={name}>{strategies[name]?format(strategies[name]):"未完成"}</td>)}</tr>)}
      </tbody></table></div></section>
      <section className="section"><h2>模型选择分布</h2><div className="metric-grid three">{Object.entries(strategies).map(([name,data])=><article className="metric" key={name}><span>{strategyNames[name]??name}</span><strong>{Object.entries(data.model_distribution).map(([model,count])=>`${model} ${count}`).join(" · ")}</strong><small>置信度：{value(data.confidence)}；来自真实决策日志</small></article>)}</div></section>
      <details className="technical-details"><summary>技术详情</summary><p>配置版本：{value(item.configuration_version)}</p><p>数据库起始水位：{value(item.database_watermark)}</p><p>证据目录：{value(item.evidence_path)}</p></details>
    </>}
  </main>;
}

export function LiveProbePage(){
  const query=useQuery({queryKey:["latest-continuous-probe"],queryFn:({signal})=>api.latestContinuousProbe(signal),refetchInterval:5000});
  const item=query.data?.item as Record<string,unknown>|undefined;
  const summary=(item?.summary??{}) as Record<string,unknown>;
  const events=(item?.events??[]) as Array<Record<string,unknown>>;
  return <main className="page-content live-acceptance-page">
    <header className="page-header"><div><p className="eyebrow">REAL UAT · CONTINUOUS PROBE</p><h1>持续探测</h1><p>受控频率、可停止、可审计的国内 UAT 真实模型探测。</p></div></header>
    {!item?<section className="state empty"><b>暂无真实持续探测</b><span>本地 Mock 任务不计入此处。</span></section>:<>
      <section className="compact-source"><b>运行 ID：{value(item.probe_run_id)}</b><span>状态：{value(item.status)}</span><span>阶段：{value(item.current_phase)}</span><span>当前间隔：{seconds(item.current_interval_seconds)} 秒</span><span>流量类型：探测</span></section>
      <section className="metric-grid four"><article className="metric"><span>实际时长</span><strong>{value(summary.duration_seconds)} 秒</strong></article><article className="metric"><span>请求</span><strong>{value(summary.request_count??item.completed_count)}</strong><small>成功 {value(summary.success_count??item.success_count)} · 失败 {value(summary.failure_count??item.failure_count)}</small></article><article className="metric"><span>成功率</span><strong>{percent(summary.success_rate)}</strong></article><article className="metric"><span>P95 / P99</span><strong>{ms(summary.p95_ms)} / {ms(summary.p99_ms)}</strong></article></section>
      <section className="section"><h2>费用与控制</h2><div className="table-scroll"><table className="console-table"><tbody><tr><th>Provider 实际费用</th><td>{money(summary.actual_provider_cost)}</td><th>版本化估算</th><td>{money(summary.estimated_versioned_price)}</td></tr><tr><th>限速</th><td>{value(summary.throttle_count??item.throttle_count)} 次</td><th>暂停 / 熔断 / 恢复</th><td>{value(item.pause_count)} / {value(item.circuit_open_count)} / {value(item.recovery_count)}</td></tr><tr><th>真实 Provider 失败</th><td>{value(summary.provider_live_failures)}</td><th>UAT 注入失败</th><td>{value(summary.uat_injected_failures)}</td></tr></tbody></table></div></section>
      <section className="section"><h2>最近控制事件</h2><div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>事件</th><th>来源</th></tr></thead><tbody>{events.slice(-12).reverse().map((event,index)=><tr key={String(event.event_id??index)}><td>{value(event.created_at)}</td><td>{value(event.event_type)}</td><td>{event.source_type==="uat_fault_injection"?"UAT 受控注入":event.source_type==="provider_live"?"真实 Provider":"本地控制事件"}</td></tr>)}</tbody></table></div></section>
      <details className="technical-details"><summary>技术详情</summary><p>数据库起始水位：{value(item.database_watermark)}</p><p>证据目录：{value(item.evidence_path)}</p><p>残留后台任务：{value(summary.residual_background_tasks)}</p></details>
    </>}
  </main>;
}
