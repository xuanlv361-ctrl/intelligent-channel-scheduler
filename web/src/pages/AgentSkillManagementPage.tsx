import {useEffect,useMemo,useRef,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {api,ApiError,type ManagedSkillHost,type ManagedSkillHostInput,type ManagedSkillInvocation,type ManagedSkillItem} from "../services/api";
import {EmptyState,PageHeader,Section,StatusBadge,formatLocalTime,type StatusTone} from "../components/console/ConsoleUI";

type Tab="catalog"|"playground"|"release"|"hosts"|"audit";
type HostPanel="closed"|"create"|"edit"|"details";

const stateText:Record<string,string>={draft:"草稿",validated:"校验通过",published:"已发布",deprecated:"已弃用",
  not_installed:"未安装",installed:"已安装",enabled:"已启用",disabled:"已停用",uninstalled:"已卸载",
  not_configured:"未配置",pending_test:"待测试",testing:"测试中",connected:"已连接",
  authentication_failed:"鉴权失败",network_failed:"连接失败",protocol_incompatible:"协议不兼容",
  success:"成功",failed:"失败",allowed:"允许",denied:"拒绝"};
const errorText:Record<string,string>={skill_not_enabled:"当前 Skill 尚未启用，请完成安装并启用。",
  skill_not_installed:"当前 Skill 尚未安装，请先安装已发布版本。",skill_not_validated:"当前版本仍为草稿，请先完成校验。",
  host_not_configured:"尚未配置真实外部 Agent Host。",host_not_enabled:"当前外部 Agent Host 未启用。",
  host_authentication_failed:"外部宿主鉴权失败，请轮换凭据后重新测试。",host_connection_failed:"无法连接外部宿主，请确认进程、地址和网络。",
  host_protocol_incompatible:"外部宿主协议版本不兼容。",invalid_host_response:"外部宿主返回的数据结构无效。",
  permission_denied:"该操作超出宿主的读取与本地计算权限。",schema_validation_failed:"调用输入不符合 Skill Schema。",
  invocation_timeout:"外部 Skill 调用超时。",instruction_skill_task_required:"请输入需要 Skill 完成的真实任务。",
  execution_engine_not_configured:"尚未配置可执行该 Skill 的国内 UAT 模型引擎。",
  instruction_skill_empty_result:"执行引擎未返回可展示的业务结果。"};
const permissionText:Record<string,string>={read:"读取",local_compute:"本地计算",controlled_write:"受控写入",
  external_network:"外部网络",secret_use:"使用临时凭据",production_change:"生产变更"};
const tabs:Array<[Tab,string]>=[["catalog","Skill目录"],["playground","测试台"],["release","发布与安装"],["hosts","宿主连接"],["audit","审计记录"]];
const blankHost:ManagedSkillHostInput={host_name:"",host_type:"independent_http_runtime",base_url:"",
  environment:"local_acceptance",protocol:"formal-agent-skill",protocol_version:"1.0",auth_type:"bearer"};

function tone(value:string):StatusTone{return ["success","connected","published","enabled","validated"].includes(value)?"success":
  ["failed","authentication_failed","network_failed","protocol_incompatible"].includes(value)?"danger":
  ["pending_test","draft","installed"].includes(value)?"warning":"neutral";}
function codeOf(error:unknown){return error instanceof ApiError?String(error.detail.code??""):"";}
function ErrorPanel({title,error}:{title:string;error:unknown}){const code=codeOf(error)||"invocation_failed";return <div className="skill-error-panel" role="alert">
  <b>{title}</b><p>{errorText[code]??"操作未完成，请查看审计记录。"}</p><details><summary>技术详情</summary><dl><div><dt>错误代码</dt><dd>{code}</dd></div>{error instanceof ApiError&&<div><dt>HTTP状态</dt><dd>{error.detail.status}</dd></div>}</dl></details></div>}
function lifecycleReason(item:ManagedSkillItem){if(item.version_status==="draft")return "当前版本仍为草稿，请先完成校验。";
  if(item.version_status!=="published")return "当前版本尚未发布。";if(item.installation_status==="not_installed")return "当前 Skill 尚未安装。";
  if(item.installation_status!=="enabled")return "当前 Skill 尚未启用。";return "";}

function MarkdownResult({value}:{value:string}){const lines=value.split(/\r?\n/);let code=false;return <article className="skill-markdown">{lines.map((line,index)=>{
  if(line.trim().startsWith("```")){code=!code;return <span key={index}/>;}if(code)return <pre key={index}><code>{line}</code></pre>;
  if(line.startsWith("### "))return <h4 key={index}>{line.slice(4)}</h4>;if(line.startsWith("## "))return <h3 key={index}>{line.slice(3)}</h3>;
  if(line.startsWith("# "))return <h2 key={index}>{line.slice(2)}</h2>;if(/^[-*] /.test(line))return <li key={index}>{line.slice(2)}</li>;
  return line.trim()?<p key={index}>{line}</p>:<span className="skill-markdown-space" key={index}/>;})}</article>}
const specializedResultTitles:Record<string,string>={
  "explain-routing-decision":"决策时间线",
  "inspect-channel-health":"渠道健康指标",
  "analyze-model-cost":"Token 与费用分析",
  "classify-provider-error":"错误分类与重试依据",
  "collect-acceptance-evidence":"验收证据清单",
  "validate-uat-request":"请求校验结果",
  "execute-uat-request":"真实 UAT 响应",
  "update-routing-policy":"策略配置差异",
  "operate-circuit-breaker":"熔断状态转换",
  "propose-traffic-switch":"流量切换提案",
};
function StructuredBusinessResult({skillId,data}:{skillId:string;data:Record<string,unknown>}){
  const title=specializedResultTitles[skillId]??"执行结果";
  const rows=Object.entries(data).filter(([key,value])=>!["content","execution_steps","input_summary","raw_input","raw_output"].includes(key)&&value!=null&&typeof value!=="object").slice(0,12);
  const timeline=Array.isArray(data.timeline)?data.timeline.map(String):[];
  return <section className={`skill-specialized-result skill-${skillId}`} data-result-kind={skillId}>
    <h4>{title}</h4>
    {timeline.length>0&&<ol className="skill-result-timeline">{timeline.map((item,index)=><li key={`${item}-${index}`}>{item}</li>)}</ol>}
    {rows.length>0?<dl className="skill-structured-facts">{rows.map(([key,value])=><div key={key}><dt>{key.replaceAll("_"," ")}</dt><dd>{String(value)}</dd></div>)}</dl>:<p>{String(data.result_summary??data.message??"未返回可展示结果")}</p>}
  </section>;
}
function InvocationResult({value}:{value:ManagedSkillInvocation}){const data=value.data??{};const validated=value.execution_result==="conditions_validated"||data.business_executed===false;
  const content=String(data.content??"");const steps=Array.isArray(data.execution_steps)?data.execution_steps.map(String):[];
  const statusLabel=validated?"生命周期验证通过":value.execution_result==="success"?"Skill调用成功":"Skill调用未完成";
  return <div className={`skill-execution-result ${validated?"validation-only":"business-result"}`}>
    <header><div><small>{validated?"未执行业务任务":"真实业务结果"}</small><h3>{statusLabel}</h3></div><StatusBadge tone={validated?"warning":value.execution_result==="success"?"success":"danger"}>{validated?"条件通过":stateText[value.execution_result]??value.execution_result}</StatusBadge></header>
    {!validated&&content&&<MarkdownResult value={content}/>} {!validated&&!content&&<StructuredBusinessResult skillId={value.skill_id} data={data}/>} 
    {steps.length>0&&<section className="skill-run-steps"><h4>执行步骤</h4><ol>{steps.map((step,index)=><li key={`${step}-${index}`}>{step}</li>)}</ol></section>}
    <dl className="skill-result-facts"><div><dt>执行位置</dt><dd>{data.host_id==="local-formal-runtime"?"本地 Formal Runtime":String(data.host_id??"本地 Formal Runtime")}</dd></div><div><dt>执行引擎</dt><dd>{String(data.execution_engine??(validated?"未执行":"未记录"))}</dd></div><div><dt>模型</dt><dd>{String(data.model_id??"未使用")}</dd></div><div><dt>数据来源</dt><dd>{String(data.data_source??"未记录")}</dd></div><div><dt>耗时</dt><dd>{data.latency_ms!=null?`${String(data.latency_ms)} ms`:"未记录"}</dd></div><div><dt>Token</dt><dd>{data.input_tokens!=null||data.output_tokens!=null?`${String(data.input_tokens??0)} / ${String(data.output_tokens??0)}`:"未记录"}</dd></div><div><dt>Request ID</dt><dd>{String(data.request_id??"未生成")}</dd></div><div><dt>权限判定</dt><dd>{stateText[value.permission_decision]??value.permission_decision}</dd></div></dl>
    <footer className="skill-trace-footer"><span>Invocation ID <code>{value.invocation_id}</code></span><span>Audit ID <code>{value.audit_id}</code></span></footer>
    <details className="skill-technical-details"><summary>技术详情</summary><pre>{JSON.stringify({input_summary:data.input_summary??null,output:data},null,2)}</pre></details>
  </div>}

export function AgentSkillManagementPage(){
  const qc=useQueryClient();const [tab,setTab]=useState<Tab>("catalog");const [selectedSkillId,setSelectedSkillId]=useState("");
  const [hostPanel,setHostPanel]=useState<HostPanel>("closed");const [selectedHostId,setSelectedHostId]=useState("");
  const [hostDraft,setHostDraft]=useState<ManagedSkillHostInput>(blankHost);const [credential,setCredential]=useState("");
  const [credentialHeader,setCredentialHeader]=useState("X-API-Key");const [hostSkillId,setHostSkillId]=useState("");
  const [invocationMode,setInvocationMode]=useState<"lifecycle_validation"|"functional">("functional");
  const [selectedInvocationId,setSelectedInvocationId]=useState("");
  const [skillTask,setSkillTask]=useState("为智能渠道调度系统设计一个只读查询模型健康状态的MCP Server。");
  const [executionLocation,setExecutionLocation]=useState("local");const [modelId,setModelId]=useState("deepseek-v4-flash");
  const [outputLanguage,setOutputLanguage]=useState("中文");const [maxOutputTokens,setMaxOutputTokens]=useState(1200);const [dryRun,setDryRun]=useState(false);
  const invokeGuard=useRef(false);
  const [schemaValues,setSchemaValues]=useState<Record<string,string>>({});
  const skills=useQuery({queryKey:["managed-skills"],queryFn:({signal})=>api.managedSkills(signal)});
  const hosts=useQuery({queryKey:["managed-skill-hosts"],queryFn:({signal})=>api.managedSkillHosts(signal)});
  const audits=useQuery({queryKey:["managed-skill-audits"],queryFn:({signal})=>api.managedSkillAudits(signal),enabled:tab==="audit"});
  const hostCapabilities=useQuery({queryKey:["managed-skill-host-capabilities",selectedHostId],queryFn:({signal})=>api.managedSkillHostCapabilities(selectedHostId,signal),enabled:hostPanel==="details"&&!!selectedHostId});
  const hostInstallations=useQuery({queryKey:["managed-skill-host-installations",selectedHostId],queryFn:({signal})=>api.managedSkillHostInstallations(selectedHostId,signal),enabled:hostPanel==="details"&&!!selectedHostId});
  const hostInvocations=useQuery({queryKey:["managed-skill-host-invocations",selectedHostId],queryFn:({signal})=>api.managedSkillHostInvocations(selectedHostId,signal),enabled:hostPanel==="details"&&!!selectedHostId});
  const priorInvocations=useQuery({queryKey:["managed-skill-invocations",selectedSkillId],queryFn:({signal})=>api.managedSkillInvocations(selectedSkillId,signal),enabled:tab==="playground"&&!!selectedSkillId});
  const items=useMemo(()=>skills.data?.items??[],[skills.data?.items]);useEffect(()=>{if(!selectedSkillId&&items.length)setSelectedSkillId(items[0].skill_id)},[items,selectedSkillId]);
  const selectedSkill=items.find(item=>item.skill_id===selectedSkillId);const selectedHost=hosts.data?.items.find(item=>item.host_id===selectedHostId);
  const publishedSkills=items.filter(item=>item.version_status==="published");
  const refreshHost=()=>{void qc.invalidateQueries({queryKey:["managed-skill-hosts"]});void qc.invalidateQueries({queryKey:["managed-skill-host-capabilities"]});
    void qc.invalidateQueries({queryKey:["managed-skill-host-installations"]});void qc.invalidateQueries({queryKey:["managed-skill-host-invocations"]});void qc.invalidateQueries({queryKey:["managed-skill-audits"]});};
  const invoke=useMutation({mutationFn:async()=>{const item=items.find(value=>value.skill_id===selectedSkillId);const required=item?.input_schema.required??[];
    const argumentsValue:Record<string,unknown>={invocation_mode:invocationMode,skill_version:item?.version,execution_location:executionLocation,
      output_language:outputLanguage,max_output_tokens:maxOutputTokens,dry_run:dryRun,...schemaValues};
    if(skillTask.trim())argumentsValue.task=skillTask.trim();
    if(invocationMode==="functional"&&item?.binding==="instruction_only")argumentsValue.model_id=modelId;
    if(executionLocation!=="local"){const remote=await api.invokeManagedSkillHost(executionLocation,{skill_id:selectedSkillId,arguments:argumentsValue});return {invocation_id:remote.invocation_id,audit_id:remote.audit_id,operator_id:"remote-host",skill_id:selectedSkillId,skill_version:item?.version??"",started_at:String(remote.result?.started_at??""),finished_at:String(remote.result?.finished_at??""),permission_decision:"allowed",execution_result:String(remote.execution_result??remote.status??"failed"),data:{...remote.result,host_id:executionLocation},uncertainty:"known",network_called:true,write_performed:false} as ManagedSkillInvocation;}
    for(const key of required)if(key!=="task"&&!argumentsValue[key])argumentsValue[key]=schemaValues[key]??"";
    return api.invokeManagedSkill(selectedSkillId,argumentsValue)},
    onSuccess:(data)=>{setSelectedInvocationId(data.invocation_id);void qc.invalidateQueries({queryKey:["managed-skill-invocations",selectedSkillId]})},
    onSettled:()=>{invokeGuard.current=false}});
  const lifecycle=useMutation({mutationFn:({item,action}:{item:ManagedSkillItem;action:string})=>action==="validate"?api.managedSkillAction(item.skill_id,"validate"):
    action==="enable"||action==="disable"?api.managedSkillAction(item.skill_id,action):api.managedSkillLifecycle(item.skill_id,action as "publish"|"install"|"upgrade"|"rollback"|"uninstall",item.version),
    onSuccess:()=>void qc.invalidateQueries({queryKey:["managed-skills"]})});
  const saveHost=useMutation({mutationFn:()=>hostPanel==="edit"&&selectedHostId?api.updateManagedSkillHost(selectedHostId,hostDraft):api.createManagedSkillHost(hostDraft),
    onSuccess:(saved)=>{setSelectedHostId(saved.host_id);setHostPanel("details");refreshHost()}});
  const saveCredential=useMutation({mutationFn:()=>api.setManagedSkillHostCredential(selectedHostId,{auth_type:hostDraft.auth_type||selectedHost?.auth_type||"bearer",credential,
    header_name:(hostDraft.auth_type||selectedHost?.auth_type)==="api_key_header"?credentialHeader:undefined}),onSuccess:()=>{setCredential("");refreshHost()}});
  const revokeCredential=useMutation({mutationFn:()=>api.revokeManagedSkillHostCredential(selectedHostId),onSuccess:refreshHost});
  const testHost=useMutation({mutationFn:()=>api.testManagedSkillHost(selectedHostId),onSuccess:refreshHost});
  const toggleHost=useMutation({mutationFn:(enabled:boolean)=>api.setManagedSkillHostEnabled(selectedHostId,enabled),onSuccess:refreshHost});
  const installHostSkill=useMutation({mutationFn:()=>{const item=items.find(skill=>skill.skill_id===hostSkillId)!;return api.installManagedSkillOnHost(selectedHostId,{skill_id:item.skill_id,version:item.version})},onSuccess:refreshHost});
  const installationAction=useMutation({mutationFn:({installationId,action}:{installationId:string;action:"enable"|"disable"|"uninstall"})=>action==="uninstall"?
    api.uninstallManagedSkillFromHost(selectedHostId,installationId):api.setManagedSkillHostInstallationEnabled(selectedHostId,installationId,action==="enable"),onSuccess:refreshHost});
  const invokeHost=useMutation({mutationFn:()=>api.invokeManagedSkillHost(selectedHostId,{skill_id:hostSkillId,arguments:{task:"检查宿主连接页的信息层级、真实状态和窄屏布局。"}}),onSuccess:refreshHost});
  const hostError=saveHost.error||saveCredential.error||revokeCredential.error||testHost.error||toggleHost.error||installHostSkill.error||installationAction.error||invokeHost.error;
  const hostBusy=[saveHost,saveCredential,revokeCredential,testHost,toggleHost,installHostSkill,installationAction,invokeHost].some(m=>m.isPending);
  const openHost=(host:ManagedSkillHost,panel:HostPanel="details")=>{setSelectedHostId(host.host_id);setHostDraft({host_name:host.host_name,host_type:host.host_type,
    base_url:host.base_url??"",environment:host.environment,protocol:host.protocol,protocol_version:host.protocol_version??"1.0",auth_type:host.auth_type});setHostPanel(panel)};
  const auditItems=useMemo(()=>audits.data?.items??[],[audits.data]);
  const connectedHostCount=hosts.data?.connected_count??(hosts.data?.items.filter(item=>item.status==="connected"&&item.enabled).length??0);
  return <div className="skill-page"><PageHeader title="Agent Skill" description="统一管理Skill来源、版本、安装、权限、外部宿主与审计。"/>
    <div className="skill-status-strip" aria-label="Agent Skill 状态摘要"><span>已注册 <b>{skills.data?.registered_count??0}</b></span><span>已发布 <b>{skills.data?.published_count??0}</b></span>
      <span>已安装 <b>{skills.data?.installed_count??0}</b></span><span>已启用 <b>{skills.data?.enabled_count??0}</b></span><span>已连接宿主 <b>{connectedHostCount}</b></span><span>最近同步 {formatLocalTime(skills.data?.last_synced_at)}</span></div>
    <nav className="skill-tabs" aria-label="Agent Skill 功能">{tabs.map(([id,label])=><button key={id} className={tab===id?"active":""} onClick={()=>setTab(id)}>{label}</button>)}</nav>

    {tab==="catalog"&&<Section title="Skill目录" description="版本状态与安装状态分别管理；未审核外部Skill不能启用。">{skills.isLoading?<p>正在读取Skill目录…</p>:<div className="table-scroll"><table className="console-table"><thead><tr><th>Skill</th><th>来源</th><th>版本</th><th>安装</th><th>权限</th><th>最近调用</th></tr></thead><tbody>{items.map(item=><tr key={item.skill_id}><td><b>{item.display_name}</b><small>{item.skill_id}</small></td><td>{item.source_type}</td><td><StatusBadge tone={tone(item.version_status)}>{stateText[item.version_status]}</StatusBadge><small>{item.version}</small></td><td><StatusBadge tone={tone(item.installation_status)}>{stateText[item.installation_status]}</StatusBadge></td><td>{item.requested_permissions.map(value=><span className="skill-permission" key={value}>{permissionText[value]??value}</span>)}</td><td>{formatLocalTime(item.last_invoked_at)}</td></tr>)}</tbody></table></div>}</Section>}

    {tab==="playground"&&<Section title="Skill测试台" description="生命周期验证只检查调用条件；功能调用会真实执行任务并返回业务结果。"><div className="skill-test-mode" role="group" aria-label="测试类型"><button className={invocationMode==="lifecycle_validation"?"active":""} onClick={()=>{setInvocationMode("lifecycle_validation");invoke.reset()}}>生命周期验证</button><button className={invocationMode==="functional"?"active":""} onClick={()=>{setInvocationMode("functional");invoke.reset()}}>功能调用</button></div><div className="skill-playground-grid"><div className="skill-call-config"><header><small>调用配置</small><h3>{invocationMode==="functional"?"执行真实 Skill 任务":"验证调用前置条件"}</h3></header><label>Skill<select value={selectedSkillId} onChange={e=>{setSelectedSkillId(e.target.value);setSelectedInvocationId("");invoke.reset()}}>{items.map(item=><option key={item.skill_id} value={item.skill_id}>{item.display_name} · {item.version}</option>)}</select></label>
      <div className="skill-config-pair"><label>Skill版本<input value={selectedSkill?.version??""} readOnly/></label><label>执行位置<select value={executionLocation} onChange={e=>setExecutionLocation(e.target.value)}><option value="local">本地 Formal Runtime</option>{(hosts.data?.items??[]).filter(host=>host.enabled&&host.status==="connected").map(host=><option key={host.host_id} value={host.host_id}>外部Host · {host.host_name}</option>)}</select></label></div>
      {invocationMode==="functional"&&<><label>用户任务<textarea rows={6} value={skillTask} onChange={e=>setSkillTask(e.target.value)} placeholder="描述希望 Skill 实际完成的任务"/></label>{selectedSkill?.binding==="instruction_only"&&<div className="skill-config-pair"><label>执行模型<input list="skill-model-suggestions" value={modelId} onChange={e=>setModelId(e.target.value)} placeholder="真实国内UAT模型ID"/><datalist id="skill-model-suggestions"><option value="deepseek-v4-flash"/><option value="kimi-k3"/><option value="glm-5.2"/></datalist></label><label>输出语言<select value={outputLanguage} onChange={e=>setOutputLanguage(e.target.value)}><option>中文</option><option>English</option></select></label></div>}<div className="skill-config-pair"><label>最大输出长度<input type="number" min={64} max={4000} value={maxOutputTokens} onChange={e=>setMaxOutputTokens(Number(e.target.value))}/></label><label className="skill-check"><input type="checkbox" checked={dryRun} onChange={e=>setDryRun(e.target.checked)}/> Dry Run</label></div></>}
      {(selectedSkill?.input_schema.required??[]).filter(key=>key!=="task").map(key=><label key={key}>{selectedSkill?.input_schema.properties?.[key]?.title??key}<input value={schemaValues[key]??""} onChange={e=>setSchemaValues(value=>({...value,[key]:e.target.value}))}/></label>)}
      <div className="skill-execution-pipeline"><span>加载版本</span><i>→</i><span>权限与Schema</span><i>→</i><span>{invocationMode==="functional"?"执行引擎":"条件结论"}</span><i>→</i><span>审计落库</span></div>
      <button className="skill-primary-action" disabled={!selectedSkill||!!lifecycleReason(selectedSkill)||invoke.isPending||(invocationMode==="functional"&&selectedSkill?.binding==="instruction_only"&&(!skillTask.trim()||!modelId.trim()))} onClick={()=>{if(invokeGuard.current)return;invokeGuard.current=true;invoke.mutate()}}>{invoke.isPending?"正在执行…":invocationMode==="functional"?"功能调用":"验证调用条件"}</button></div><div className="skill-result-pane"><header><small>执行结果</small><h3>{invoke.data?"本次调用":"等待执行"}</h3></header>{(priorInvocations.data?.items?.length??0)>1&&<label className="skill-history-picker">历史调用结果<select aria-label="历史调用结果" value={selectedInvocationId} onChange={event=>setSelectedInvocationId(event.target.value)}><option value="">最近一次</option>{priorInvocations.data?.items.map(item=><option key={item.invocation_id} value={item.invocation_id}>{formatLocalTime(item.started_at)} · {stateText[item.execution_result]??item.execution_result} · {item.invocation_id}</option>)}</select></label>}{invoke.data?<InvocationResult value={invoke.data}/>:priorInvocations.data?.items?.length?<><p className="skill-operation-note">已恢复持久化调用结果。</p><InvocationResult value={priorInvocations.data.items.find(item=>item.invocation_id===selectedInvocationId)??priorInvocations.data.items[0]}/></>:<EmptyState title="尚无调用结果" description="选择验证类型并执行后，结果将在此展示。"/>}</div></div>
      {selectedSkill&&lifecycleReason(selectedSkill)&&<div className="skill-call-blocker"><StatusBadge tone="warning">暂不可调用</StatusBadge><span>{lifecycleReason(selectedSkill)}</span><button onClick={()=>setTab("release")}>前往发布与安装</button></div>}
      {invoke.error&&<ErrorPanel title="Skill调用失败" error={invoke.error}/>}</Section>}

    {tab==="release"&&<Section title="发布与安装" description="校验、发布、安装、启停和卸载均由后端状态机持久化。"><div className="table-scroll"><table className="console-table"><thead><tr><th>Skill</th><th>版本状态</th><th>安装状态</th><th>操作</th></tr></thead><tbody>{items.map(item=><tr key={item.skill_id}><td><b>{item.display_name}</b><small>{item.version}</small></td><td>{stateText[item.version_status]}</td><td>{stateText[item.installation_status]}</td><td className="skill-actions">{item.version_status==="draft"&&<button onClick={()=>lifecycle.mutate({item,action:"validate"})}>安全检查与校验</button>}{item.version_status==="validated"&&<button onClick={()=>lifecycle.mutate({item,action:"publish"})}>发布版本</button>}{item.version_status==="published"&&item.installation_status==="not_installed"&&<button onClick={()=>lifecycle.mutate({item,action:"install"})}>安装</button>}{item.installation_status==="installed"&&<button onClick={()=>lifecycle.mutate({item,action:"enable"})}>启用</button>}{item.installation_status==="enabled"&&<button onClick={()=>lifecycle.mutate({item,action:"disable"})}>停用</button>}{item.installation_status==="disabled"&&<><button onClick={()=>lifecycle.mutate({item,action:"enable"})}>重新启用</button><button className="secondary" onClick={()=>lifecycle.mutate({item,action:"uninstall"})}>卸载</button></>}</td></tr>)}</tbody></table></div>{lifecycle.error&&<ErrorPanel title="生命周期操作被拒绝" error={lifecycle.error}/>}</Section>}

    {tab==="hosts"&&<Section title="宿主连接" description="本地独立Host通过真实HTTP运行；第三方Host在提供地址前保持待配置。" actions={<button onClick={()=>{setHostDraft(blankHost);setSelectedHostId("");setHostPanel("create")}}>新增宿主</button>}>
      {!hosts.isLoading&&!hosts.data?.items.length?<EmptyState title="尚未配置真实外部 Agent Host" description="当前仅启用本地 Formal Agent Skill Runtime；可新增宿主并完成真实HTTP连接测试。" action={<button onClick={()=>{setHostDraft(blankHost);setHostPanel("create")}}>新增宿主</button>}/>:<div className="table-scroll"><table className="console-table"><thead><tr><th>宿主</th><th>环境 / 协议</th><th>状态</th><th>凭据</th><th>安装</th><th>最近测试</th><th>操作</th></tr></thead><tbody>{hosts.data?.items.map(host=><tr key={host.host_id}><td><b>{host.host_name}</b><small>{host.base_url??"地址未配置"}</small></td><td>{host.environment}<small>{host.protocol} {host.protocol_version}</small></td><td><StatusBadge tone={tone(host.status)}>{stateText[host.status]??host.status}</StatusBadge><small>{host.enabled?"已启用":"未启用"}</small></td><td>{host.credential_fingerprint??"未配置"}</td><td>{host.installed_skill_count}</td><td>{formatLocalTime(host.last_tested_at)}</td><td className="skill-actions"><button onClick={()=>openHost(host)}>管理</button><button className="secondary" onClick={()=>openHost(host,"edit")}>编辑</button></td></tr>)}</tbody></table></div>}
      {(hostPanel==="create"||hostPanel==="edit")&&<div className="skill-host-editor" role="dialog" aria-label={hostPanel==="create"?"新增宿主":"编辑宿主"}><header><div><h3>{hostPanel==="create"?"新增外部宿主":"编辑外部宿主"}</h3><p>本地验收仅允许127.0.0.1:8010；第三方地址必须经过后端白名单和HTTPS校验。</p></div><button className="secondary" onClick={()=>setHostPanel("closed")}>关闭</button></header><div className="skill-host-form"><label>宿主名称<input value={hostDraft.host_name} onChange={e=>setHostDraft(v=>({...v,host_name:e.target.value}))}/></label><label>宿主类型<select value={hostDraft.host_type} onChange={e=>setHostDraft(v=>({...v,host_type:e.target.value}))}><option value="independent_http_runtime">独立HTTP Runtime</option><option value="remote_agent">远程Agent</option><option value="mcp">MCP Host</option></select></label><label className="wide">Base URL<input value={hostDraft.base_url} onChange={e=>setHostDraft(v=>({...v,base_url:e.target.value}))}/></label><label>环境<select value={hostDraft.environment} onChange={e=>setHostDraft(v=>({...v,environment:e.target.value}))}><option value="local_acceptance">本地验收</option><option value="china_uat">国内UAT</option></select></label><label>协议<select value={hostDraft.protocol} onChange={e=>setHostDraft(v=>({...v,protocol:e.target.value}))}><option value="formal-agent-skill">Formal Agent Skill</option><option value="mcp">MCP</option></select></label><label>协议版本<input value={hostDraft.protocol_version} onChange={e=>setHostDraft(v=>({...v,protocol_version:e.target.value}))}/></label><label>鉴权方式<select value={hostDraft.auth_type} onChange={e=>setHostDraft(v=>({...v,auth_type:e.target.value}))}><option value="bearer">Bearer Token</option><option value="api_key_header">API Key Header</option></select></label></div><footer><button disabled={!hostDraft.host_name||saveHost.isPending} onClick={()=>saveHost.mutate()}>{saveHost.isPending?"正在保存…":"保存宿主"}</button></footer></div>}
      {hostPanel==="details"&&selectedHost&&<div className="skill-host-detail"><header><div><h3>{selectedHost.host_name}</h3><p>执行位置：外部Host · 连接方式：真实HTTP · {selectedHost.base_url}</p></div><button className="secondary" onClick={()=>setHostPanel("closed")}>关闭</button></header><div className="skill-host-facts"><span>连接状态<b>{stateText[selectedHost.status]??selectedHost.status}</b></span><span>凭据指纹<b>{selectedHost.credential_fingerprint??"未配置"}</b></span><span>宿主能力<b>{selectedHost.capabilities.length}</b></span><span>已安装Skill<b>{selectedHost.installed_skill_count}</b></span></div><div className="skill-host-actions"><button disabled={hostBusy} onClick={()=>testHost.mutate()}>{testHost.isPending?"正在测试…":"测试连接"}</button><button className="secondary" disabled={hostBusy||(!selectedHost.enabled&&!['connected','disabled'].includes(selectedHost.status))} onClick={()=>toggleHost.mutate(!selectedHost.enabled)}>{selectedHost.enabled?"停用宿主":"启用宿主"}</button><button className="secondary" onClick={()=>openHost(selectedHost,"edit")}>编辑</button><button className="secondary" onClick={()=>setTab("audit")}>查看审计</button></div>
        <section className="skill-host-subsection"><h4>宿主凭据</h4><p>配置与轮换后必须重新测试；浏览器不保存、不回显Secret。</p><div className="skill-credential-row"><label>鉴权方式<select value={hostDraft.auth_type} onChange={e=>setHostDraft(v=>({...v,auth_type:e.target.value}))}><option value="bearer">Bearer Token</option><option value="api_key_header">API Key Header</option></select></label>{hostDraft.auth_type==="api_key_header"&&<label>Header名称<input value={credentialHeader} onChange={e=>setCredentialHeader(e.target.value)}/></label>}<label>新凭据<input aria-label="宿主凭据" type="password" autoComplete="new-password" value={credential} onChange={e=>setCredential(e.target.value)}/></label><button disabled={!credential||saveCredential.isPending} onClick={()=>saveCredential.mutate()}>配置 / 轮换</button><button className="secondary" disabled={!selectedHost.credential_fingerprint||revokeCredential.isPending} onClick={()=>revokeCredential.mutate()}>撤销</button></div></section>
        <section className="skill-host-subsection"><h4>能力与安装</h4><p>{hostCapabilities.data?.items.length?`已读取 ${hostCapabilities.data.items.length} 项真实能力。`:"尚未读取到宿主能力。"}</p><div className="skill-host-install"><select aria-label="安装到宿主的 Skill" value={hostSkillId} onChange={e=>setHostSkillId(e.target.value)}><option value="">选择已发布Skill</option>{publishedSkills.map(item=><option key={item.skill_id} value={item.skill_id}>{item.display_name} · {item.version}</option>)}</select><button disabled={!hostSkillId||installHostSkill.isPending} onClick={()=>installHostSkill.mutate()}>安装到宿主</button><button className="secondary" disabled={!hostSkillId||selectedHost.status!=="connected"||!selectedHost.enabled||invokeHost.isPending} onClick={()=>invokeHost.mutate()}>调用外部Skill</button></div>{hostInstallations.data?.items.length?<ul className="skill-install-list">{hostInstallations.data.items.map(item=><li key={item.installation_id}><span>{item.skill_id} · {item.skill_version}</span><StatusBadge tone={tone(item.status)}>{stateText[item.status]??item.status}</StatusBadge><span className="skill-actions">{item.status==="disabled"?<button onClick={()=>installationAction.mutate({installationId:item.installation_id,action:"enable"})}>启用</button>:<button onClick={()=>installationAction.mutate({installationId:item.installation_id,action:"disable"})}>停用</button>}<button className="secondary" onClick={()=>installationAction.mutate({installationId:item.installation_id,action:"uninstall"})}>卸载</button></span></li>)}</ul>:<p>该宿主尚未安装Skill。</p>}</section>
        <section className="skill-host-subsection"><h4>最近调用</h4>{hostInvocations.data?.items.length?<ul className="skill-invocation-list">{hostInvocations.data.items.slice(0,6).map(item=><li key={item.invocation_id}><b>{item.skill_id} · {item.skill_version}</b><StatusBadge tone={tone(item.execution_result??item.status??"")}>{stateText[item.execution_result??item.status??""]??item.execution_result??item.status}</StatusBadge><small>主调用 {item.invocation_id} · 主审计 {item.audit_id}</small><small>远端调用 {String(item.result?.remote_invocation_id??"未记录")} · 远端审计 {String(item.result?.remote_audit_id??"未记录")}</small></li>)}</ul>:<p>暂无外部宿主调用记录。</p>}</section>
      </div>}
      {testHost.data&&<div className="skill-host-test-result" role="status"><b>连接测试：{stateText[testHost.data.status]??testHost.data.status}</b><span>HTTP {testHost.data.http_status??"未提供"}</span><span>{testHost.data.latency_ms??"未提供"} ms</span><span>鉴权 {String(testHost.data.authentication_result??testHost.data.authentication??"未提供")}</span><span>协议 {testHost.data.protocol_version??"未提供"}</span></div>}
      {invokeHost.data&&<div className="skill-result"><b>外部Host调用成功</b><div className="skill-result-facts"><div><dt>执行位置</dt><dd>外部Host</dd></div><div><dt>连接方式</dt><dd>真实HTTP</dd></div><div><dt>主Invocation</dt><dd>{invokeHost.data.invocation_id}</dd></div><div><dt>远端Invocation</dt><dd>{String(invokeHost.data.result?.remote_invocation_id??"未记录")}</dd></div></div></div>}
      {hostError&&<ErrorPanel title="宿主操作失败" error={hostError}/>}</Section>}

    {tab==="audit"&&<Section title="审计记录" description="生命周期、宿主、权限和调用结果均保留追踪标识。">{auditItems.length?<div className="table-scroll"><table className="console-table"><thead><tr><th>时间</th><th>Skill / 宿主</th><th>操作</th><th>操作者</th><th>结果</th><th>追踪标识</th></tr></thead><tbody>{auditItems.map((row,index)=><tr key={String(row.audit_id??row.event_id??index)}><td>{formatLocalTime(row.created_at)}</td><td>{String(row.skill_id??row.host_id??"未提供")}</td><td>{String(row.operation??row.record_type??"未提供")}</td><td>{String(row.operator_id??"未提供")}</td><td>{stateText[String(row.result)]??String(row.result??"未提供")}{Boolean(row.error_code)&&<small>{errorText[String(row.error_code)]??String(row.error_code)}</small>}</td><td><small>{String(row.audit_id??row.invocation_id??row.event_id??"未提供")}</small></td></tr>)}</tbody></table></div>:<EmptyState title="暂无审计记录" description="完成生命周期、宿主或Skill调用后将在此显示。"/>}</Section>}
  </div>;
}
