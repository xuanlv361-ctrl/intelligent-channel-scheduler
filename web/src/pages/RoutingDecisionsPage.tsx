import {useQuery} from "@tanstack/react-query";
import {Link,useParams} from "react-router-dom";
import {api,RoutingDecisionCandidate} from "../services/api";

const text=(value:unknown,fallback="未记录")=>value===null||value===undefined||value===""?fallback:String(value);
const time=(value:unknown)=>value?new Date(String(value)).toLocaleString("zh-CN",{hour12:false}):"未记录";
const reason:Record<string,string>={user_specified_model:"用户明确指定",recent_live_success_evidence:"近期真实调用成功且延迟更优",real_catalog_pending_validation:"真实目录候选，能力待真实验证",catalog_execution_disabled:"模型目录标记为不可执行",capability_unsupported:"明确能力不支持",circuit_open:"渠道处于熔断状态",rate_limited:"渠道限流",price_missing:"价格缺失"};
const labelReason=(value:unknown)=>reason[String(value||"")]||text(value);

function CandidateTable({items,selected}:{items:RoutingDecisionCandidate[];selected?:string|null}){
  return <div className="table-wrap decision-candidates"><table><thead><tr><th>模型</th><th>渠道</th><th>能力状态</th><th>健康状态</th><th>延迟</th><th>成本</th><th>可靠性</th><th>总分</th><th>结果</th></tr></thead><tbody>{items.map((item,index)=><tr key={`${item.model_id}-${index}`}><td>{text(item.model_id)}</td><td>{text(item.channel_id,"待平台日志确认")}</td><td>{item.capability_status?.includes("pending")?"待真实验证":text(item.capability_status)}</td><td>{text(item.health_status,"未记录")}</td><td>{item.average_latency_ms==null?"未记录":`${Math.round(item.average_latency_ms)} ms`}</td><td>未记录</td><td>{(item.successful_live_samples??0)+(item.failed_live_samples??0)?`${item.successful_live_samples??0} 成功 / ${item.failed_live_samples??0} 失败`:"未记录"}</td><td>{item.score==null?"未评分":item.score}</td><td>{item.model_id===selected?"入选":item.score==null?"未评分":"未选择"}<small>{labelReason(item.reason)}</small></td></tr>)}</tbody></table></div>;
}

export function RoutingDecisionDetailPage(){
  const {decisionId=""}=useParams();
  const query=useQuery({queryKey:["routing-decision",decisionId],queryFn:({signal})=>api.routingDecision(decisionId,signal),enabled:Boolean(decisionId)});
  if(query.isLoading)return <main className="decision-detail-page"><p role="status">正在读取真实决策证据…</p></main>;
  if(query.error||!query.data)return <main className="decision-detail-page"><header><p className="eyebrow">DECISION TRACE</p><h1>决策详情</h1></header><div className="state error"><b>未找到对应的真实调度决策</b><span>请确认 decision_id 是否来自统一调用日志。</span><button onClick={()=>query.refetch()}>重新读取</button></div></main>;
  const d=query.data,e=d.execution_result;
  const stages=[
    ["请求进入",`${text(d.method)} ${text(d.path,"路径未记录")}`],
    ["识别请求类型",d.stream?"流式请求":"非流式请求"],
    ["生成候选模型",`${d.catalog_candidate_count??d.candidates.length} 个候选`],
    ["检查能力",d.candidates.some(item=>item.capability_status?.includes("pending"))?"存在待真实验证候选":"已按留存证据检查"],
    ["检查渠道健康",d.candidates.some(item=>item.health_status)?"已记录健康证据":"未记录"],
    ["排除不合格候选",`${d.exclusions.length} 项排除`],
    ["计算评分",d.candidates.some(item=>item.score!==null)?"已记录评分":"用户指定模型，不适用"],
    ["选择模型和渠道",`${text(d.selected_model)} / ${text(d.selected_channel,"待平台日志确认")}`],
    ["执行请求",`${text(e.http_status,"未收到状态")} · ${text(e.status)}`],
    ["重试 / Fallback",`${Math.max(0,(e.total_attempts??1)-1)} 次重试 · ${e.fallback?"发生 Fallback":"未发生 Fallback"}`],
    ["返回结果",e.response_id?`Response ID ${e.response_id}`:"Response ID 未记录"],
  ];
  return <main className="decision-detail-page">
    <header className="decision-hero"><div><p className="eyebrow">DECISION TRACE</p><h1>决策详情</h1><p>查看一次真实在线调度从请求进入到返回结果的完整证据链。</p></div><Link className="secondary button-link" to="/observability/decisions">返回决策日志</Link></header>
    <section className="decision-identity"><div><small>decision_id</small><code>{d.decision_id}</code></div><div><small>Request ID</small><code>{d.request_id}</code></div><div><small>环境</small><b>{d.environment_id==="china_uat"?"国内 UAT":d.environment_id}</b></div><div><small>时间</small><b>{time(d.created_at)}</b></div><div><small>调度状态</small><b>{text(e.status)}</b></div><div><small>决策类型</small><b>{d.decision_type==="automatic_routing"?"自动调度":"用户指定模型"}</b></div><div><small>策略版本</small><b>{text(d.policy_version)}</b></div><div><small>配置版本</small><b>{text(d.configuration_version)}</b></div></section>
    <section className="decision-section"><h2>执行时间线</h2><ol className="decision-timeline">{stages.map(([title,detail],index)=><li key={title}><span>{index+1}</span><div><b>{title}</b><p>{detail}</p></div></li>)}</ol></section>
    <section className="decision-section"><h2>请求摘要</h2><dl className="decision-facts"><div><dt>请求时间</dt><dd>{time(d.created_at)}</dd></div><div><dt>请求类型</dt><dd>{d.path?.includes("chat/completions")?"文本对话":"UAT HTTP 请求"}</dd></div><div><dt>响应方式</dt><dd>{d.stream?"流式":"非流式"}</dd></div><div><dt>HTTP Method</dt><dd>{text(d.method)}</dd></div><div><dt>URL（脱敏）</dt><dd>{text(d.path)}</dd></div><div><dt>预计输入规模</dt><dd>{e.input_tokens==null?"未记录":`${e.input_tokens} Token`}</dd></div></dl></section>
    <section className="decision-section"><h2>候选、评分与结果</h2><CandidateTable items={d.candidates} selected={d.selected_model}/>{d.exclusions.length>0&&<div className="decision-exclusions"><h3>排除原因</h3><ul>{d.exclusions.map((item,index)=><li key={index}><b>{text(item.model_id)}</b><span>{labelReason(item.reason)}</span></li>)}</ul></div>}</section>
    <section className="decision-section decision-choice"><h2>最终选择</h2><dl className="decision-facts"><div><dt>最终模型</dt><dd>{text(d.selected_model)}</dd></div><div><dt>最终渠道</dt><dd>{text(d.selected_channel,"待平台日志确认")}</dd></div><div><dt>选择原因</dt><dd>{labelReason(d.selection_reason)}</dd></div><div><dt>置信度</dt><dd>{text(d.confidence)}</dd></div><div><dt>选择方式</dt><dd>{d.decision_type==="automatic_routing"?"自动调度":"用户明确指定"}</dd></div><div><dt>指标快照时间</dt><dd>{text(d.metric_snapshot_id)}</dd></div></dl></section>
    <section className="decision-section"><h2>执行结果</h2><dl className="decision-facts"><div><dt>HTTP 状态</dt><dd>{text(e.http_status)}</dd></div><div><dt>Response ID</dt><dd>{text(e.response_id)}</dd></div><div><dt>实际模型</dt><dd>{text(e.model)}</dd></div><div><dt>实际渠道</dt><dd>{text(e.channel_name||e.channel_id,"待平台日志确认")}</dd></div><div><dt>总耗时</dt><dd>{e.total_latency_ms==null?"未记录":`${Math.round(e.total_latency_ms)} ms`}</dd></div><div><dt>首 Token 耗时</dt><dd>{e.first_token_latency_ms==null?"未记录":`${Math.round(e.first_token_latency_ms)} ms`}</dd></div><div><dt>输入 / 输出 Token</dt><dd>{`${text(e.input_tokens)} / ${text(e.output_tokens)}`}</dd></div><div><dt>费用</dt><dd>{e.cost_amount==null?"待 Provider 日志同步":`${e.currency||"CNY"} ${e.cost_amount}`}</dd></div><div><dt>重试</dt><dd>{Math.max(0,(e.total_attempts??1)-1)}</dd></div><div><dt>Fallback / 渠道切换</dt><dd>{e.fallback?"已发生":"未发生"}</dd></div><div><dt>最终错误</dt><dd>{text(e.error_category||e.error_code,"无")}</dd></div></dl></section>
    <details className="decision-technical"><summary>技术详情（默认折叠）</summary><pre>{JSON.stringify({decision_id:d.decision_id,request_id:d.request_id,response_id:d.response_id,retry_history:d.retry_history},null,2)}</pre></details>
  </main>;
}

export function RoutingDecisionsPage(){
  const query=useQuery({queryKey:["routing-decisions"],queryFn:({signal})=>api.routingDecisions(undefined,signal)});
  return <main className="decision-list-page"><header className="decision-hero"><div><p className="eyebrow">ONLINE DECISIONS</p><h1>决策日志</h1><p>按 decision_id 查看真实在线调度；历史回放属于独立的离线反事实分析。</p></div><Link className="secondary button-link" to="/observability/replay">前往历史回放</Link></header>{query.isLoading?<p role="status">正在读取决策日志…</p>:query.data?.items.length?<div className="table-wrap"><table><thead><tr><th>时间</th><th>decision_id</th><th>Request ID</th><th>类型</th><th>模型 / 渠道</th><th>状态</th><th>操作</th></tr></thead><tbody>{query.data.items.map(item=><tr key={item.decision_id}><td>{time(item.created_at)}</td><td><code>{item.decision_id}</code></td><td><code>{item.request_id}</code></td><td>{item.decision_type==="automatic_routing"?"自动调度":"用户指定模型"}</td><td>{text(item.selected_model)}<small>{text(item.selected_channel,"渠道待确认")}</small></td><td>{text(item.execution_result.status)}</td><td><Link to={`/routing/decisions/${encodeURIComponent(item.decision_id)}`}>查看详情</Link></td></tr>)}</tbody></table></div>:<div className="state empty"><b>暂无真实决策日志</b><span>完成一次真实模型请求后会在这里生成 decision_id。</span></div>}</main>;
}
