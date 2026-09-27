import {useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api,ConfigReviewResult} from "../services/api";
import {EmptyState,ErrorState,MetricCard,PageHeader,Section,StatusBadge} from "../components/console/ConsoleUI";
import {formatPercentage,labelEnum} from "../lib/presentation";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

const time=(value:string|null)=>value?new Date(value).toLocaleString("zh-CN",{hour12:false}):"未提供";
const number=(value:number|null|undefined,suffix="")=>value==null?"无法计算":`${value.toLocaleString()}${suffix}`;
const money=(value:string|null|undefined)=>value==null?"无法计算":`CNY ${Number(value).toLocaleString("zh-CN",{maximumFractionDigits:6})}`;
const source=(value:string)=>value==="historical_uat_csv"?"历史 CSV":value==="realtime_execution"?"实时执行":value;
const showValue=(value:unknown)=>value===null||value===undefined?"未提供":typeof value==="object"?JSON.stringify(value):String(value);

export default function ConfigReviewPage(){
  const [baseline,setBaseline]=useState<string>();
  const [comparison,setComparison]=useState<string>();
  const [nextTimeout,setNextTimeout]=useState("");
  const [changeReason,setChangeReason]=useState("");
  const query=useQuery({queryKey:["real-config-review",baseline,comparison],
    queryFn:({signal})=>api.configReview(signal,baseline,comparison),refetchInterval:5000});
  if(query.isLoading)return <div className="state loading" role="status">正在读取真实配置与去重日志…</div>;
  if(query.error)return <ErrorState title="配置评审加载失败" description={query.error instanceof Error?query.error.message:"后端未返回评审结果"} onRetry={()=>query.refetch()}/>;
  const data=query.data as ConfigReviewResult;
  const legacyVersion=data.data_source.configuration_version||data.configuration.policy_version||"";
  const selectedBase=baseline||data.selected_versions?.baseline||legacyVersion;
  const selectedCompare=comparison||data.selected_versions?.comparison||legacyVersion;
  const versions=data.versions??[];
  const configurationDiff=data.configuration_diff??{};
  data.configuration_diff=configurationDiff;
  return <div className="config-review-page">
    <PageHeader title="配置评审" description="基于持久化配置版本与去重后的真实国内 UAT 日志评估风险和影响。" actions={<button className="secondary" onClick={()=>query.refetch()} disabled={query.isFetching}>{query.isFetching?"刷新中…":"刷新评审"}</button>}/>

    <section className="config-review-source" aria-label="数据来源与同步状态">
      <div><small>数据来源</small><strong>{data.data_source.sources.map(item=>`${source(item.source)} ${item.count}`).join(" · ")||"暂无真实日志"}</strong></div>
      <div><small>原始 / 去重有效</small><strong>{(data.data_source.raw_count??data.data_source.sample_count??0).toLocaleString()} / {(data.data_source.deduplicated_count??data.data_source.sample_count??0).toLocaleString()}</strong></div>
      <div><small>精确重复 / 疑似重复</small><strong>{data.data_source.exact_duplicate_count??"未记录"} / {data.data_source.duplicate_candidate_count??"未记录"}</strong></div>
      <div><small>精确关联 / 渠道覆盖</small><strong>{data.data_source.exact_link_count??"未记录"} / {data.data_source.channel_coverage_rate==null?"无法计算":formatPercentage(data.data_source.channel_coverage_rate)}</strong></div>
      <div><small>最后同步</small><strong>{time(data.data_source.last_synced_at)}</strong></div>
      <StatusBadge tone={data.status==="ready"?"success":"warning"}>{data.status==="ready"?"真实数据可用":"证据不足"}</StatusBadge>
    </section>

    <Section title="当前配置">
      <div className="config-review-policy"><dl>
        <div><dt>当前策略版本</dt><dd>{data.configuration.policy_version||"未提供"}</dd></div>
        <div><dt>重试版本</dt><dd>{data.configuration.retry_policy_version||"未提供"}</dd></div>
        <div><dt>超时</dt><dd>{number(data.configuration.timeout_ms," ms")}</dd></div>
        <div><dt>最大尝试</dt><dd>{data.configuration.maximum_attempts??"未提供"}</dd></div>
        <div><dt>Fallback</dt><dd>{data.configuration.fallback_configured?"已配置":"未配置"}</dd></div>
      </dl></div>
      <div className="form-grid clean"><label>新超时（ms）<input type="number" min="1" value={nextTimeout} onChange={event=>setNextTimeout(event.target.value)} placeholder="输入受控 UAT 配置变更"/></label><label>修改原因<input value={changeReason} onChange={event=>setChangeReason(event.target.value)} placeholder="说明本次非生产变更原因"/></label></div>
      <BusinessSkillAction skillId="update-routing-policy" label="生成新策略版本" argumentsValue={{policy_id:data.configuration.policy_version,expected_version:selectedCompare,changes:{timeout_ms:Number(nextTimeout)},reason:changeReason}} disabledReason={nextTimeout&&changeReason?undefined:"填写新超时和修改原因后才能提交"} confirmation/>
    </Section>

    {data.status!=="ready"?<EmptyState title="暂无真实日志，无法完成评审" description="不会使用 Mock、Demo 或缺失值 0 生成结论。"/>:<>
      <Section title="真实日志指标" description={`${time(data.data_source.time_from)} 至 ${time(data.data_source.time_to)}，统计已排除精确重复记录。`}>
        <div className="metric-grid config-review-metrics">
          <MetricCard label="有效样本" value={data.metrics.sample_count}/>
          <MetricCard label="成功率" value={data.metrics.success_rate===null?"无法计算":formatPercentage(data.metrics.success_rate)}/>
          <MetricCard label="P95 / P99" value={`${number(data.metrics.p95_latency_ms," ms")} / ${number(data.metrics.p99_latency_ms," ms")}`}/>
          <MetricCard label="Token" value={data.metrics.input_tokens==null||data.metrics.output_tokens==null?"未记录":`${data.metrics.input_tokens.toLocaleString()} 入 / ${data.metrics.output_tokens.toLocaleString()} 出`}/>
          <MetricCard label="实际费用" value={money(data.metrics.total_cost)}/>
          <MetricCard label="渠道覆盖" value={data.data_source.channel_coverage_rate===null?"无法计算":formatPercentage(data.data_source.channel_coverage_rate)}/>
        </div>
        <div className="config-review-concentration"><span>权威渠道 <b>{data.metrics.concentration.channel_count||"无法计算"}</b></span><span>HHI <b>{data.metrics.concentration.hhi??"无法计算"}</b></span><span>最大占比 <b>{data.metrics.concentration.maximum_share===null?"无法计算":formatPercentage(data.metrics.concentration.maximum_share)}</b></span><small>{data.metrics.concentration.basis}</small></div>
      </Section>

      <Section title="风险检查">
        <div className="table-wrap dense"><table><thead><tr><th>检查项</th><th>结论</th><th>真实事件</th><th>口径</th></tr></thead><tbody>{data.risks.map((item,index)=><tr key={`${item.risk}-${index}`}><td>{item.risk}</td><td>{item.status}</td><td>{item.observed_count??"无法计算"}</td><td>{item.basis}</td></tr>)}</tbody></table></div>
      </Section>
    </>}

    <Section title="版本比较" description={versions.length<=1?"当前仅有一个真实配置版本，已建立基线快照；产生下一版本后可进行跨版本比较。":"选择不可变快照；只有两个版本都有真实调用证据时才计算效果差异。"}>
      <div className="filter-bar config-version-selectors">
        <label>基线版本<select value={selectedBase} onChange={event=>setBaseline(event.target.value)}>{versions.length?versions.map(item=><option key={item.configuration_version} value={item.configuration_version}>{item.configuration_version}{item.is_active?"（当前）":""}</option>):<option value={legacyVersion}>{legacyVersion}</option>}</select></label>
        <label>比较版本<select value={selectedCompare} onChange={event=>setComparison(event.target.value)}>{versions.length?versions.map(item=><option key={item.configuration_version} value={item.configuration_version}>{item.configuration_version}{item.is_active?"（当前）":""}</option>):<option value={legacyVersion}>{legacyVersion}</option>}</select></label>
      </div>
      {Object.keys(data.configuration_diff).length?<div className="table-wrap dense"><table><thead><tr><th>字段</th><th>基线</th><th>比较版本</th></tr></thead><tbody>{Object.entries(data.configuration_diff).map(([key,diff])=><tr key={key}><td>{key}</td><td>{showValue(diff.before)}</td><td>{showValue(diff.after)}</td></tr>)}</tbody></table></div>:<div className="compact-empty">所选版本没有可展示的配置差异。</div>}
      <p className="calculation-basis">{data.impact.basis}</p>
    </Section>

    <Section title="关联请求与决策证据">
      {data.evidence.length?<div className="table-wrap dense"><table><thead><tr><th>时间</th><th>Request ID</th><th>decision_id</th><th>模型 / 渠道</th><th>结果</th><th>耗时</th><th>费用</th></tr></thead><tbody>{data.evidence.map((item,index)=><tr key={`${item.request_id||item.decision_id}-${index}`}><td>{time(item.occurred_at)}</td><td>{item.request_id?<a href={`/routing/runs?search=${encodeURIComponent(item.request_id)}`}>{item.request_id}</a>:"未提供"}</td><td>{item.decision_id?<a href={`/routing/decisions/${encodeURIComponent(item.decision_id)}`}>{item.decision_id}</a>:"未提供"}</td><td>{item.model||"未提供"}<small>{item.channel_id||"渠道待确认"} · {item.channel_source}</small></td><td>{labelEnum(item.status)}</td><td>{number(item.latency_ms," ms")}</td><td>{item.cost_amount===null?"未提供":`${item.currency||"CNY"} ${item.cost_amount}`}</td></tr>)}</tbody></table></div>:<div className="compact-empty">暂无可下钻的真实 Request ID 或 decision_id。</div>}
    </Section>
  </div>;
}
