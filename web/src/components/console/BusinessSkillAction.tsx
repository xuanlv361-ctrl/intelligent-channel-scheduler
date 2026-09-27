import {useMutation,useQuery} from "@tanstack/react-query";
import {Link} from "react-router-dom";
import {api,ApiError} from "../../services/api";
import {StatusBadge} from "./ConsoleUI";

const labels:Record<string,string>={request_count:"请求数",call_count:"调用数",success_rate:"成功率",
  p50_latency_ms:"P50 延迟",p95_latency_ms:"P95 延迟",p99_latency_ms:"P99 延迟",
  total_tokens:"总 Token",total_cost:"总费用",average_cost:"平均费用",currency:"币种",
  error_category:"错误分类",retryable:"可重试",decision_reconstruction_rate:"决策可还原率",
  sample_count:"样本数",confidence:"置信度",proposal_id:"提案 ID",data_source:"数据来源"};
const message=(error:unknown)=>error instanceof ApiError?error.message:error instanceof Error?error.message:"Skill 调用未完成";
const display=(value:unknown)=>value===null||value===undefined||value===""?"未提供":typeof value==="boolean"?(value?"是":"否"):String(value);

export function BusinessSkillAction({skillId,label,argumentsValue,disabledReason,confirmation=false}:{
  skillId:string;label:string;argumentsValue:Record<string,unknown>;disabledReason?:string;confirmation?:boolean;
}){
  const catalog=useQuery({queryKey:["managed-skills"],queryFn:({signal})=>api.managedSkills(signal)});
  const item=catalog.data?.items.find(value=>value.skill_id===skillId);
  const invoke=useMutation({mutationFn:()=>api.invokeManagedSkill(skillId,{...argumentsValue,...(confirmation?{confirmed:true}:{})})});
  const execute=()=>{
    if(confirmation&&!window.confirm(`确认通过 ${item?.display_name??skillId} 执行本次受控操作？`))return;
    invoke.mutate();
  };
  const data=invoke.data?.data??{};
  const facts=Object.entries(data).filter(([key,value])=>labels[key]&&!(typeof value==="object"&&value!==null)).slice(0,8);
  return <section className="business-skill-action" data-skill-id={skillId}>
    <div className="business-skill-heading"><span><b>{item?.display_name??label}</b><small>{skillId} · {item?.installed_version??item?.version??"读取版本中"}</small></span>
      <button type="button" disabled={Boolean(disabledReason)||invoke.isPending||item?.installation_status!=="enabled"} onClick={execute}>{invoke.isPending?"正在调用…":label}</button></div>
    {disabledReason&&<small className="skill-disabled-reason">{disabledReason}</small>}
    {invoke.error&&<p className="skill-inline-error" role="alert">{message(invoke.error)}</p>}
    {invoke.data&&<div className="business-skill-result"><p><StatusBadge tone={invoke.data.execution_result==="success"?"success":"warning"}>{invoke.data.execution_result==="success"?"调用成功":"证据不足或被拒绝"}</StatusBadge> 数据来源：{display(data.data_source)}</p>
      {facts.length>0&&<dl>{facts.map(([key,value])=><div key={key}><dt>{labels[key]}</dt><dd>{display(value)}</dd></div>)}</dl>}
      <small>Invocation ID：{invoke.data.invocation_id} · 审计 ID：{invoke.data.audit_id} · 权限：{invoke.data.permission_decision}</small>
      <Link to={`/integrations/agent-skill?tab=audit&audit_id=${encodeURIComponent(invoke.data.audit_id)}`}>查看审计</Link></div>}
  </section>;
}
