import {useMemo,useState} from "react";
import {useMutation,useQuery} from "@tanstack/react-query";
import {Link} from "react-router-dom";
import {api,HistoricalReplayInput,HistoricalReplayItem} from "../services/api";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

const STRATEGIES=[
  ["actual_observed","历史实际选择"],
  ["latency_first","延迟优先"],
  ["cost_first","成本优先"],
] as const;
const strategyName=(value:string)=>STRATEGIES.find(([key])=>key===value)?.[1]||value;
const localTime=(value:string|null|undefined)=>value?new Date(value).toLocaleString("zh-CN",{hour12:false}):"未提供";
const value=(input:unknown,suffix="")=>input===null||input===undefined||input===""?"不可计算":`${input}${suffix}`;
const money=(input:string|null,currency:string|null)=>input===null?"不可计算":`${currency||"CNY"} ¥${input}`;
const percent=(input:number|null)=>input===null?"不可计算":`${input.toFixed(1)}%`;
const integer=(input:number|null|undefined)=>input===null||input===undefined?"未提供":input.toLocaleString("zh-CN");
const reasonLabel:Record<string,string>={
  insufficient_candidate_latency_samples:"候选渠道延迟样本不足",
  candidate_cost_evidence_missing:"候选成本证据缺失",
  authoritative_channel_missing:"权威渠道证据缺失",
  price_version_missing:"价格版本缺失",
  token_evidence_missing:"Token证据缺失",
  unsupported_billing_unit:"计费单位暂不支持",
  historical_request_content_missing:"缺少历史请求内容，不能进行请求级模型重新选择",
  candidate_model_or_channel_price_missing:"候选模型或渠道价格证据缺失",
};
const associationLabel=(association:HistoricalReplayItem["association"])=>association==="exact"?"精确关联":association==="historical_statistics"?"仅历史统计":association==="execution_only"?"仅执行记录":"无法使用";

function ReplayDrawer({item,onClose}:{item:HistoricalReplayItem;onClose:()=>void}){
  return <div className="replay-drawer-backdrop" role="presentation" onMouseDown={event=>{if(event.target===event.currentTarget)onClose()}}>
    <aside className="replay-drawer" role="dialog" aria-modal="true" aria-label="请求回放详情">
      <header><div><span className="replay-kicker">TRACE DETAIL</span><h2>请求回放详情</h2><p>{localTime(item.occurred_at)}</p></div><button className="icon-button" onClick={onClose} aria-label="关闭详情">×</button></header>
      <section className="trace-identifiers"><div><small>Request ID</small><code>{item.request_id||"历史数据未提供"}</code></div><div><small>Response ID</small><code>{item.response_id||"未提供"}</code></div><div><small>decision_id</small>{item.decision_id?<Link to={`/routing/decisions/${encodeURIComponent(item.decision_id)}`}><code>{item.decision_id}</code></Link>:<code>历史数据未提供</code>}</div></section>
      <ol className="replay-timeline">{item.timeline.map(step=><li key={step.step} className={`trace-${step.status}`}><span>{step.step}</span><div><h3>{step.title}</h3><dl>{Object.entries(step.evidence).map(([key,itemValue])=><div key={key}><dt>{key}</dt><dd>{itemValue===null||itemValue===undefined?"未提供":typeof itemValue==="object"?"结构化证据（见技术详情）":String(itemValue)}</dd></div>)}</dl></div></li>)}</ol>
      <section className="replay-basis"><h3>证据能力</h3><dl><div><dt>配置版本</dt><dd>{item.configuration_version||"未提供"}</dd></div><div><dt>指标快照</dt><dd>{item.metric_snapshot_id||"未提供"}</dd></div><div><dt>历史价格说明</dt><dd>{item.actual.price_version||"未提供"}</dd></div><div><dt>关联等级</dt><dd>{associationLabel(item.association)}</dd></div></dl></section>
      <details className="replay-technical"><summary>技术详情（默认折叠）</summary><p>这里只展示脱敏后的结构字段，不包含凭据、Prompt 或完整响应正文。</p><dl><div><dt>记录ID</dt><dd>{item.record_id}</dd></div><div><dt>来源</dt><dd>{item.source_type||"真实UAT调用日志"}</dd></div><div><dt>成本依据</dt><dd>{item.counterfactual.cost_basis||reasonLabel[item.counterfactual.cost_unavailable_reason||""]||"不可计算"}</dd></div><div><dt>延迟依据</dt><dd>{item.counterfactual.latency_basis||reasonLabel[item.counterfactual.reason||""]||"不可计算"}</dd></div></dl></details>
    </aside>
  </div>;
}

export default function HistoricalReplayPage(){
  const [form,setForm]=useState<HistoricalReplayInput>({environment_id:"china_uat",occurred_from:null,occurred_to:null,model:null,channel:null,request_type:null,baseline_strategy:"actual_observed",candidate_strategy:"latency_first",exact_only:false,limit:1000});
  const [search,setSearch]=useState("");
  const [modelFilter,setModelFilter]=useState("");
  const [channelFilter,setChannelFilter]=useState("");
  const [changedOnly,setChangedOnly]=useState(false);
  const [lowOnly,setLowOnly]=useState(false);
  const [unestimableOnly,setUnestimableOnly]=useState(false);
  const [page,setPage]=useState(1);
  const [selected,setSelected]=useState<HistoricalReplayItem|null>(null);
  const source=useQuery({queryKey:["historical-replay-source",form.environment_id],queryFn:({signal})=>api.historicalReplaySource({environment_id:form.environment_id},signal)});
  const replay=useMutation({mutationFn:()=>api.runHistoricalReplay(form),onSuccess:()=>setPage(1)});
  const result=replay.data;
  const filtered=useMemo(()=>{
    const needle=search.trim().toLowerCase();
    return (result?.items||[]).filter(item=>{
      if(needle&&!`${item.request_id||""} ${item.actual.model||""} ${item.actual.channel_id||""} ${item.counterfactual.model||""} ${item.counterfactual.channel_id||""}`.toLowerCase().includes(needle))return false;
      if(modelFilter&&item.actual.model!==modelFilter&&item.counterfactual.model!==modelFilter)return false;
      if(channelFilter&&item.actual.channel_id!==channelFilter&&item.counterfactual.channel_id!==channelFilter)return false;
      if(changedOnly&&item.counterfactual.choice_changed!==true)return false;
      if(lowOnly&&item.counterfactual.confidence!=="low")return false;
      if(unestimableOnly&&item.counterfactual.channel_id!==null&&item.counterfactual.estimated_cost!==null&&item.counterfactual.estimated_latency_ms!==null)return false;
      return true;
    });
  },[result,search,modelFilter,channelFilter,changedOnly,lowOnly,unestimableOnly]);
  const replayModels=useMemo(()=>Array.from(new Set((result?.items||[]).flatMap(item=>[item.actual.model,item.counterfactual.model]).filter((item):item is string=>Boolean(item)))).sort(),[result]);
  const replayChannels=useMemo(()=>Array.from(new Set((result?.items||[]).flatMap(item=>[item.actual.channel_id,item.counterfactual.channel_id]).filter((item):item is string=>Boolean(item)))).sort(),[result]);
  const pageSize=25,totalPages=Math.max(1,Math.ceil(filtered.length/pageSize));
  const rows=filtered.slice((Math.min(page,totalPages)-1)*pageSize,Math.min(page,totalPages)*pageSize);
  const explainableDecision=(result?.items||[]).find(item=>Boolean(item.decision_id))?.decision_id;
  const set=(key:keyof HistoricalReplayInput,next:unknown)=>setForm(current=>({...current,[key]:next}));
  return <main className="historical-replay-page">
    <header className="replay-page-header"><div><span className="replay-kicker">REAL UAT · OFFLINE COUNTERFACTUAL</span><h1>历史回放</h1><p>用同一批真实 UAT 请求比较历史实际选择与候选策略的离线决策。</p></div><Link className="secondary button-link" to="/routing/runs">查看真实日志</Link></header>
    <BusinessSkillAction skillId="explain-routing-decision" label="解释回放中的真实决策" argumentsValue={{decision_id:explainableDecision??"",include_metrics:true,include_execution:true}} disabledReason={explainableDecision?undefined:"当前回放结果没有可精确关联的 decision_id"}/>

    <section className="replay-filter-panel" aria-label="回放条件"><div className="replay-filter-grid">
      <label>环境<select value={form.environment_id} onChange={event=>set("environment_id",event.target.value)}><option value="china_uat">国内 UAT</option></select></label>
      <label>开始时间<input type="datetime-local" value={form.occurred_from?.slice(0,16)||""} onChange={event=>set("occurred_from",event.target.value?new Date(event.target.value).toISOString():null)}/></label>
      <label>结束时间<input type="datetime-local" value={form.occurred_to?.slice(0,16)||""} onChange={event=>set("occurred_to",event.target.value?new Date(event.target.value).toISOString():null)}/></label>
      <label>模型<input value={form.model||""} onChange={event=>set("model",event.target.value||null)} placeholder="全部模型"/></label>
      <label>渠道<input value={form.channel||""} onChange={event=>set("channel",event.target.value||null)} placeholder="全部渠道"/></label>
      <label>请求类型<input value={form.request_type||""} onChange={event=>set("request_type",event.target.value||null)} placeholder="全部类型"/></label>
      <label>基线策略<select value={form.baseline_strategy} onChange={event=>set("baseline_strategy",event.target.value)}>{STRATEGIES.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
      <label>候选策略<select value={form.candidate_strategy} onChange={event=>set("candidate_strategy",event.target.value)}>{STRATEGIES.filter(([key])=>key!==form.baseline_strategy).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
      <label className="inline-check"><input type="checkbox" checked={form.exact_only} onChange={event=>set("exact_only",event.target.checked)}/>只使用精确关联</label>
      <button onClick={()=>replay.mutate()} disabled={replay.isPending||form.baseline_strategy===form.candidate_strategy||!source.data?.sample_count}>{replay.isPending?"正在回放…":"运行历史回放"}</button>
    </div></section>

    {source.isLoading&&<div className="replay-empty">正在读取真实 UAT 日志来源…</div>}
    {source.error&&<div className="replay-error" role="alert">真实日志来源读取失败，请检查本地后端后重试。</div>}
    {source.data&&<section className="replay-source-strip" aria-label="数据来源"><div><b>真实 UAT 日志 · 国内 UAT</b><span>{localTime(source.data.occurred_from)} 至 {localTime(source.data.occurred_to)}</span><small>{source.data.sources.map(item=>`${item.source_type} ${item.count}`).join(" · ")||"没有真实日志来源"}</small></div><dl><div><dt>样本</dt><dd>{source.data.sample_count}</dd></div><div><dt>精确关联</dt><dd>{source.data.exact_association_count}</dd></div><div><dt>无法关联</dt><dd>{source.data.unlinked_count}</dd></div><div><dt>最后同步</dt><dd>{localTime(source.data.last_synced_at)}</dd></div><div><dt>同步水位</dt><dd>{source.data.source_watermark||"未生成"}</dd></div><div><dt>配置版本</dt><dd>{source.data.configuration_versions.join("、")||"未提供"}</dd></div><div><dt>价格版本</dt><dd>{source.data.price_versions.join("、")||"未关联"}</dd></div></dl><Link to="/routing/runs">查看来源</Link></section>}
    {source.data&&source.data.sample_count>0&&<>
      <section className="replay-observed" aria-label="历史实际数据"><header><div><span className="replay-kicker">OBSERVED USAGE</span><h2>历史实际数据</h2></div><span>按统一调用日志中的已发生结果统计，不依赖 Request ID 或候选价格版本</span></header><div className="replay-metric-row observed-metrics">
        <article><small>实际调用</small><strong>{integer(source.data.actual.record_count)} 条</strong><span>{source.data.sources.map(item=>`${item.source_type} ${item.count}`).join(" · ")}</span></article>
        <article><small>输入 Token</small><strong>{integer(source.data.actual.input_tokens)}</strong><span>缓存输入 {integer(source.data.actual.cached_input_tokens)}</span></article>
        <article><small>输出 Token</small><strong>{integer(source.data.actual.output_tokens)}</strong><span>来自标准化日志字段</span></article>
        <article><small>历史实际费用</small><strong>{money(source.data.actual.total_cost,source.data.actual.currency)}</strong><span>覆盖 {source.data.actual.cost_coverage_count}/{source.data.actual.record_count} · 花费字段</span></article>
        <article><small>实际 P95 / P99</small><strong>{value(source.data.actual.p95_latency_ms," ms")} / {value(source.data.actual.p99_latency_ms," ms")}</strong><span>P50 {value(source.data.actual.p50_latency_ms," ms")}</span></article>
        <article><small>失败与流式</small><strong>{source.data.actual.failure_count} 次失败</strong><span>流式 {source.data.actual.stream_count} · 非流式 {source.data.actual.nonstream_count}</span></article>
      </div></section>
      <section className="replay-capability-strip" aria-label="数据能力说明">
        <span><b>历史聚合统计</b> 可用 · {source.data.evidence_capabilities.historical_aggregation} 条</span>
        <span><b>请求级精确关联</b> {source.data.evidence_capabilities.exact_decision_reconstruction}/{source.data.sample_count}</span>
        <span><b>仅历史统计</b> {source.data.evidence_capabilities.historical_statistics_only} 条</span>
        <span><b>渠道级比较</b> {source.data.evidence_capabilities.authoritative_channel?`${source.data.evidence_capabilities.authoritative_channel} 条有权威渠道证据`:"证据不足"}</span>
        <span><b>无法使用</b> {source.data.evidence_capabilities.unusable} 条</span>
      </section>
    </>}
    {source.data?.sample_count===0&&<div className="replay-empty"><b>当前没有可用于回放的真实UAT日志。</b><span>系统不会自动切换到 Demo 数据，也不会生成成本、延迟或变化率。</span></div>}
    {replay.error&&<div className="replay-error" role="alert">历史回放运行失败：请确认基线与候选策略不同，并检查筛选条件。</div>}

    {result&&<>
      <section className="replay-conclusion"><span>离线结论</span><h2>{result.conclusion}</h2><p>{result.disclaimer}</p></section>
      <section className="replay-metric-row" aria-label="核心比较结果">
        <article><small>有效样本</small><strong>{result.sample_count} 条</strong><span>精确关联 {result.exact_association_count} · 覆盖率 {result.sample_count?Math.round(result.exact_association_count/result.sample_count*100):0}%</span></article>
        <article><small>选择发生变化</small><strong>{result.choice_changed_count} / {result.choice_comparable_count}</strong><span>{result.choice_changed_rate===null?"缺少渠道关联":`${(result.choice_changed_rate*100).toFixed(1)}%`} · 仅比较可关联选择</span></article>
        <article><small>候选策略费用</small><strong>{money(result.cost.candidate,result.cost.currency)}</strong><span>{result.cost.candidate===null?(reasonLabel[result.cost.candidate_unavailable_reason||""]||"候选价格证据不足"):`相对实际 ${percent(result.cost.delta_percent)}`} · 覆盖 {result.cost.coverage_count}/{result.sample_count}</span></article>
        <article><small>可估算延迟 P95</small><strong>{value(result.latency.baseline_p95_ms," ms")} → {value(result.latency.candidate_p95_ms," ms")}</strong><span>覆盖 {result.latency.coverage_count}/{result.sample_count} · 最少3条同模型渠道样本</span></article>
        <article><small>Fallback</small><strong>{result.fallback.baseline} → {result.fallback.candidate===null?"不可推断":result.fallback.candidate}</strong><span>反事实执行未发生</span></article>
        <article><small>成功状态变化</small><strong>{result.success_changes.success_to_failure===null?"不可推断":`${result.success_changes.success_to_failure} / ${result.success_changes.failure_to_success}`}</strong><span>成功→失败 / 失败→成功 · 覆盖 0/{result.sample_count}</span></article>
        <article><small>SLA 风险</small><strong>{result.sla.candidate_risk===null?"无法判断":result.sla.candidate_risk}</strong><span>实际失败 {result.sla.baseline_failure_count} · 超时 {result.sla.baseline_timeout_count} · 限流 {result.sla.baseline_rate_limit_count}</span></article>
        <article><small>低置信度</small><strong>{result.low_confidence_count} 条</strong><span>风险 {result.risk_count} · 不可估算 {result.unestimable_count}</span></article>
      </section>

      {result.choice_comparable_count===0&&<div className="replay-comparison-empty"><b>候选策略尚未产生可比较决策。</b><span>历史实际 Token、费用和延迟统计仍然有效。</span><small>{reasonLabel[result.cost.candidate_unavailable_reason||""]||"缺少可追溯的候选模型或渠道证据。"}</small></div>}

      <section className="replay-comparison"><header><div><span className="replay-kicker">POLICY DIFF</span><h2>基线策略 vs 候选策略</h2></div></header><div className="replay-table-scroll"><table><thead><tr><th>项目</th><th><span className="actual-tag">历史实际</span> {strategyName(result.baseline_strategy)}</th><th><span className="estimate-tag">离线估算</span> {strategyName(result.candidate_strategy)}</th><th>差异 / 口径</th></tr></thead><tbody>
        <tr><td>模型与渠道选择</td><td>当时真实执行结果</td><td>{result.choice_comparable_count?`${result.choice_changed_count} 条发生变化`:"渠道证据不足"}</td><td>仅比较存在权威渠道关联的记录</td></tr>
        <tr><td>成本</td><td>{money(result.actual.total_cost,result.actual.currency)}<small>{result.cost.actual_formula} · 覆盖 {result.actual.cost_coverage_count}/{result.actual.record_count}</small></td><td>{money(result.cost.candidate,result.cost.currency)}<small>{result.cost.candidate===null?(reasonLabel[result.cost.candidate_unavailable_reason||""]||"不可计算"):result.cost.formula}</small></td><td>{percent(result.cost.delta_percent)} · 候选覆盖 {result.cost.coverage_count}/{result.sample_count}</td></tr>
        <tr><td>P95 延迟</td><td>{value(result.latency.baseline_p95_ms," ms")}</td><td>{value(result.latency.candidate_p95_ms," ms")}</td><td>{result.latency.formula}</td></tr>
        <tr><td>成功状态</td><td>真实状态</td><td>不可推断</td><td>{result.success_changes.reason}</td></tr>
        <tr><td>SLA 风险</td><td>失败 {result.sla.baseline_failure_count} · 超时 {result.sla.baseline_timeout_count} · P99 {value(result.sla.baseline_p99_ms," ms")}</td><td>无法判断</td><td>{result.sla.reason}</td></tr>
        <tr><td>Fallback</td><td>{result.fallback.baseline} 次</td><td>不可推断</td><td>{result.fallback.reason}</td></tr>
        <tr><td>渠道集中度 HHI</td><td>{value(result.concentration.baseline_hhi)}</td><td>{value(result.concentration.candidate_hhi)}</td><td>{result.concentration.reason||result.concentration.formula}</td></tr>
      </tbody></table></div></section>

      <section className="replay-requests"><header><div><span className="replay-kicker">REQUEST LEDGER</span><h2>逐请求回放</h2><p>点击任一记录，查看真实执行与候选策略估算的证据时间线。</p></div><a className="secondary button-link" href={api.historicalReplayExportUrl(result.replay_id)}>导出 CSV</a></header>
        <div className="replay-request-filters"><label>搜索记录<input value={search} onChange={event=>{setSearch(event.target.value);setPage(1)}} placeholder="Request ID 或模型"/></label><label>模型<select value={modelFilter} onChange={event=>{setModelFilter(event.target.value);setPage(1)}}><option value="">全部模型</option>{replayModels.map(model=><option key={model}>{model}</option>)}</select></label><label>渠道<select value={channelFilter} onChange={event=>{setChannelFilter(event.target.value);setPage(1)}}><option value="">全部渠道</option>{replayChannels.map(channel=><option key={channel}>{channel}</option>)}</select></label><label><input type="checkbox" checked={changedOnly} onChange={event=>{setChangedOnly(event.target.checked);setPage(1)}}/>只看发生变化</label><label><input type="checkbox" checked={lowOnly} onChange={event=>{setLowOnly(event.target.checked);setPage(1)}}/>只看低置信度</label><label><input type="checkbox" checked={unestimableOnly} onChange={event=>{setUnestimableOnly(event.target.checked);setPage(1)}}/>只看不可估算</label></div>
        <div className="replay-table-scroll"><table><thead><tr><th>时间</th><th>模型</th><th>调用结果</th><th>流式状态</th><th>输入 Token</th><th>输出 Token</th><th>实际费用</th><th>总耗时 / 首Token</th><th>关联等级</th><th>候选策略</th></tr></thead><tbody>{rows.map(item=><tr key={item.record_id} onClick={()=>setSelected(item)} tabIndex={0} onKeyDown={event=>{if(event.key==="Enter")setSelected(item)}}><td>{localTime(item.occurred_at)}</td><td>{item.actual.actual_model||item.actual.model||"未提供"}<small>{item.actual.billed_model&&item.actual.billed_model!==(item.actual.actual_model||item.actual.model)?`计费模型 ${item.actual.billed_model}`:item.source_type==="historical_uat_csv"?"历史 UAT":"实时执行"}</small></td><td>{item.actual.status||"未提供"}<small>{item.actual.http_status?`HTTP ${item.actual.http_status}`:"历史状态"}</small></td><td>{item.actual.stream===true?"流式":item.actual.stream===false?"非流式":"未提供"}</td><td>{integer(item.actual.input_tokens)}<small>缓存 {integer(item.actual.cached_input_tokens)}</small></td><td>{integer(item.actual.output_tokens)}</td><td>{money(item.actual.cost_amount,item.actual.currency)}<small>{item.actual.price_version||"价格说明未版本化"}</small></td><td>{value(item.actual.latency_ms," ms")}<small>首Token {value(item.actual.first_token_latency_ms," ms")}</small></td><td><span className={item.association==="exact"?"confidence-high":item.association==="historical_statistics"?"confidence-medium":"confidence-low"}>{associationLabel(item.association)}</span><small>{item.request_id||"Request ID 未提供"}</small></td><td>{item.counterfactual.channel_id?`${item.counterfactual.model} / ${item.counterfactual.channel_id}`:"不可比较"}<small>{reasonLabel[item.counterfactual.reason||item.counterfactual.cost_unavailable_reason||""]||"证据充分"}</small></td></tr>)}</tbody></table></div>
        {!rows.length&&<div className="replay-empty">当前筛选条件下没有逐请求记录。</div>}
        <footer className="replay-pagination"><span>共 {filtered.length} 条 · 第 {Math.min(page,totalPages)} / {totalPages} 页</span><div><button className="secondary" disabled={page<=1} onClick={()=>setPage(current=>current-1)}>上一页</button><button className="secondary" disabled={page>=totalPages} onClick={()=>setPage(current=>current+1)}>下一页</button></div></footer>
      </section>
    </>}
    {selected&&<ReplayDrawer item={selected} onClose={()=>setSelected(null)}/>} 
  </main>;
}
