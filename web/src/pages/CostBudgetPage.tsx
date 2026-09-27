import {useMemo,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api,ApiError,DataMode} from "../services/api";
import {BusinessSkillAction} from "../components/console/BusinessSkillAction";

const money=(value:string|null|undefined)=>value==null?"待 Provider 日志同步":`CNY ¥${Number(value).toFixed(6)}`;
const time=(value?:string|null)=>value?new Date(value).toLocaleString("zh-CN"):"未提供";

export default function CostBudgetPage({mode,onModeChange}:{mode:DataMode;onModeChange:(mode:DataMode)=>void}){
  void mode; void onModeChange;
  const costs=useQuery({queryKey:["cost-budget","uat"],queryFn:({signal})=>api.costBudget("uat",signal)});
  const prices=useQuery({queryKey:["price-status"],queryFn:({signal})=>api.priceStatus(signal)});
  const [query,setQuery]=useState("");
  const rows=useMemo(()=>costs.data?.highest_cost_requests.filter(row=>
    [row.request_id,row.requested_model,row.actual_model].some(value=>String(value||"").toLowerCase().includes(query.toLowerCase())))||[],[costs.data,query]);
  if(costs.isLoading||prices.isLoading)return <div className="state loading">正在读取真实费用与价格版本…</div>;
  if(costs.error||prices.error){const error=costs.error||prices.error;return <div className="state error" role="alert">{error instanceof ApiError?error.message:"价格数据加载失败"}<button onClick={()=>{void costs.refetch();void prices.refetch()}}>重试</button></div>}
  const data=costs.data!; const catalog=prices.data!.model_catalog;
  return <main className="console-page">
    <header className="ops-page-header"><div><p className="eyebrow">CHINA UAT · VERIFIED PRICING</p><h1>价格版本与调用费用</h1><p>实际费用来自统一 Provider 日志；版本化估算单独标注，不把缺失费用显示为零。</p></div><button onClick={()=>{void costs.refetch();void prices.refetch()}}>刷新</button></header>
    <section className="ops-section" aria-label="价格版本摘要">
      <header><div><h2>当前价格版本</h2><p>模型级价格不伪造渠道；历史版本不可覆盖。</p></div><span className={`status-badge ${catalog?.status==="ready"?"success":"warning"}`}>{catalog?.status==="ready"?"可用于估算":"价格待确认"}</span></header>
      <div className="skill-status-strip">
        <span>版本 <b>{catalog?.price_version||"未建立"}</b></span><span>生效 <b>{time(catalog?.effective_from)}</b></span>
        <span>币种 <b>CNY</b></span><span>覆盖模型 <b>{catalog?.model_count??0}</b></span>
        <span>已确认 <b>{catalog?.confirmed_count??0}</b></span><span>待确认 <b>{catalog?.pending_count??0}</b></span>
        <span>来源 <b>{catalog?.source_type==="historical_log_detail"?"历史 UAT 日志单价证据":catalog?.source_type||"未提供"}</b></span>
        <span>采集时间 <b>{time(catalog?.captured_at)}</b></span>
      </div>
    </section>
    <BusinessSkillAction skillId="analyze-model-cost" label="分析 Token 与费用" argumentsValue={{environment_id:"china_uat",time_range:"all",group_by:"model"}}/>
    <section className="ops-metrics">
      <article className="metric-card"><small>统一日志请求</small><strong>{data.summary.request_count}</strong><span>全部真实来源</span></article>
      <article className="metric-card"><small>已同步实际费用</small><strong>{money(data.summary.total_spend)}</strong><span>Provider 日志字段</span></article>
      <article className="metric-card"><small>价格确认覆盖</small><strong>{catalog?.model_count?`${catalog.confirmed_count}/${catalog.model_count}`:"未提供"}</strong><span>模型级版本证据</span></article>
      <article className="metric-card"><small>价格版本校验和</small><strong>{catalog?.checksum?.slice(0,12)||"未提供"}</strong><span>SHA-256</span></article>
    </section>
    <section className="ops-section"><header><div><h2>调用费用明细</h2><p>实际费用与估算费用分列；无实际账单时等待日志同步。</p></div><input aria-label="筛选费用记录" placeholder="Request ID 或模型" value={query} onChange={event=>setQuery(event.target.value)}/></header>
      <div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>Request ID</th><th>请求 / 实际模型</th><th>Token</th><th>延迟</th><th>实际费用</th><th>版本化估算</th><th>来源</th></tr></thead><tbody>
        {rows.map(row=><tr key={row.request_id||`${row.timestamp}-${row.requested_model}`}><td>{time(row.timestamp)}</td><td>{row.request_id||"历史数据未提供"}</td><td>{row.requested_model||"未提供"}<small>{row.actual_model&&row.actual_model!==row.requested_model?`实际：${row.actual_model}`:""}</small></td><td>{row.total_tokens??"未提供"}</td><td>{row.latency_ms==null?"未提供":`${Math.round(row.latency_ms)} ms`}</td><td>{money(row.actual_cost)}</td><td>{row.estimated_cost==null?"无法计算":`CNY ¥${Number(row.estimated_cost).toFixed(6)}`}</td><td>{row.source_type}</td></tr>)}
        {!rows.length&&<tr><td colSpan={8}>当前筛选范围没有真实调用记录。</td></tr>}
      </tbody></table></div>
    </section>
  </main>;
}
