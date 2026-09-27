import {KeyboardEvent,useEffect,useMemo,useRef,useState} from "react";
import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {Link} from "react-router-dom";
import {api,ApiError,DataMode,EnvironmentId,UatModel,UatWorkbenchBody,UatWorkbenchPair,UatWorkbenchResult} from "../services/api";
import {PageHeader,StatusBadge,formatLocalTime} from "../components/console/ConsoleUI";

type Method=UatWorkbenchBody["method"];
type ConversationMessage={id:string;role:"user"|"assistant";content:string;result?:UatWorkbenchResult;stopped?:boolean;chatContext?:boolean};
type Stage="idle"|"preparing"|"routing"|"calling"|"receiving"|"completed"|"failed"|"stopped";

const METHODS:Method[]=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"];
const CHAT_PATH="/v1/chat/completions";
const uid=()=>globalThis.crypto?.randomUUID?.()??`${Date.now()}-${Math.random()}`;
const messageOf=(error:unknown)=>error instanceof ApiError?error.message:error instanceof Error?error.message:"请求未完成";

function parseAssistantText(result:UatWorkbenchResult):string{
  if(!result.response_body)return result.method==="HEAD"?"HEAD 请求已完成，响应不包含正文。":"响应没有正文。";
  const raw=result.response_body;
  try{
    const parsed=JSON.parse(raw) as Record<string,unknown>;
    const choices=Array.isArray(parsed.choices)?parsed.choices:[];
    const first=choices[0] as Record<string,unknown>|undefined;
    const message=first?.message as Record<string,unknown>|undefined;
    if(typeof message?.content==="string")return message.content;
    if(typeof parsed.output_text==="string")return parsed.output_text;
    return JSON.stringify(parsed,null,2);
  }catch{
    const chunks=raw.split(/\r?\n/).filter(line=>line.startsWith("data:"));
    const text=chunks.flatMap(line=>{
      const value=line.slice(5).trim();if(!value||value==="[DONE]")return [];
      try{
        const parsed=JSON.parse(value) as {choices?:Array<{delta?:{content?:string};message?:{content?:string}}>};
        return [parsed.choices?.[0]?.delta?.content??parsed.choices?.[0]?.message?.content??""];
      }catch{return []}
    }).join("");
    return text||raw;
  }
}

function friendlyFailure(result:UatWorkbenchResult):string{
  const status=result.http_status;
  if(status===401||status===403)return `请求失败：${status} 鉴权失败\n请检查 API Key 是否有效。`;
  if(status===429)return `请求失败：429 请求频率受限\n系统已完成 ${Math.max(0,(result.attempts??1)-1)} 次重试，最终仍失败。`;
  if(status===404)return "请求失败：404 接口不存在\n请检查国内 UAT URL 路径。";
  if(status>=500)return `请求失败：${status} 上游服务异常\n请稍后重试或查看执行详情。`;
  return `请求失败：HTTP ${status}\n${parseAssistantText(result)}`;
}

function ResultDetails({result}:{result:UatWorkbenchResult}){
  const total=result.total_tokens??([result.input_tokens,result.output_tokens].some(v=>v!=null)
    ?(result.input_tokens??0)+(result.output_tokens??0):null);
  const facts:Array<[string,unknown]>=[
    ["HTTP 状态码",result.http_status],["Request ID",result.request_id],["Response ID",result.response_id],
    ["decision_id",result.decision_id],["invocation_id",result.skill_trace?.invocation_id],["audit_id",result.skill_trace?.audit_id],
    ["执行 Skill",result.skill_trace?`${result.skill_trace.skill_id} · ${result.skill_trace.skill_version}`:null],
    ["URL",result.path],["HTTP Method",result.method],["流式状态",result.method==="GET"||result.method==="HEAD"||result.method==="OPTIONS"?"不适用":result.first_token_latency_ms!=null?"流式":"按请求配置"],
    ["模型",result.actual_model||result.requested_model],["Provider",result.provider],["渠道",result.channel_id],
    ["重试",Math.max(0,(result.attempts??1)-1)],["Fallback",result.fallback_used?"已发生":"未发生"],
    ["开始时间",result.created_at],["总耗时",`${Math.round(result.total_latency_ms)} ms`],
    ["首 Token 耗时",typeof result.first_token_latency_ms==="number"&&Number.isFinite(result.first_token_latency_ms)?`${Math.round(result.first_token_latency_ms)} ms`:null],
    ["输入 Token",result.input_tokens],["输出 Token",result.output_tokens],["缓存 Token",result.cached_input_tokens],
    ["总 Token",total],["费用",result.cost_amount==null?"待 Provider 日志同步":`${result.currency||"CNY"} ${result.cost_amount}`],
    ["价格版本","未提供"],["错误分类",result.error?String(result.error.category??"未分类"):"无"],
  ];
  return <details className="agent-execution-details"><summary>查看执行详情</summary>
    {result.decision_id?<Link className="decision-log-link" to={`/routing/decisions/${encodeURIComponent(result.decision_id)}`}>查看决策日志</Link>:<span className="decision-log-unavailable" title="本次请求未生成调度决策">本次请求未生成调度决策</span>}
    <dl>{facts.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value==null||value===""?"未提供":String(value)}</dd></div>)}</dl>
    {result.routing_decision&&<section className="agent-routing-evidence"><h4>自动调度决策</h4>
      <p>从真实目录 {result.routing_decision.catalog_candidate_count} 个候选中选择 <strong>{result.routing_decision.selected_model}</strong>；依据：{result.routing_decision.selection_reason}；置信度：{result.routing_decision.confidence}。</p>
      <table><thead><tr><th>候选模型</th><th>评分</th><th>成功样本</th><th>失败样本</th><th>平均延迟</th><th>原因</th></tr></thead><tbody>
        {result.routing_decision.candidates.slice(0,5).map(item=><tr key={item.model_id}><td>{item.model_id}</td><td>{item.score}</td><td>{item.successful_live_samples}</td><td>{item.failed_live_samples}</td><td>{item.average_latency_ms==null?"未提供":`${item.average_latency_ms} ms`}</td><td>{item.reason}</td></tr>)}
      </tbody></table></section>}
    <details className="agent-technical-details"><summary>技术详情</summary><pre>{JSON.stringify({response_headers:result.response_headers,error:result.error,capability_evidence:result.capability_evidence},null,2)}</pre></details>
  </details>;
}

export function UatWorkbenchPage({mode,environment}:{mode:DataMode;environment:EnvironmentId;onEnvironmentChange:(value:EnvironmentId)=>void}){
  const qc=useQueryClient();
  const [method,setMethod]=useState<Method>("POST");
  const [fullUrl,setFullUrl]=useState("");
  const [apiKey,setApiKey]=useState("");
  const [authSessionId,setAuthSessionId]=useState<string|null>(null);
  const [credentialSource,setCredentialSource]=useState<"manual"|"vault"|null>(null);
  const [showKey,setShowKey]=useState(false);
  const [selectionMode,setSelectionMode]=useState<"automatic"|"specified">("automatic");
  const [model,setModel]=useState("");
  const [catalogModels,setCatalogModels]=useState<UatModel[]>([]);
  const [catalogMeta,setCatalogMeta]=useState<{count:number;testedAt:string}|null>(null);
  const [stream,setStream]=useState(false);
  const [input,setInput]=useState("");
  const [messages,setMessages]=useState<ConversationMessage[]>([]);
  const [stage,setStage]=useState<Stage>("idle");
  const [localError,setLocalError]=useState("");
  const [systemPrompt,setSystemPrompt]=useState("");
  const [temperature,setTemperature]=useState("0.7");
  const [maxTokens,setMaxTokens]=useState("2048");
  const [timeout,setTimeoutValue]=useState("30");
  const [maxAttempts,setMaxAttempts]=useState("1");
  const [paramsText,setParamsText]=useState("[]");
  const [headersText,setHeadersText]=useState("[]");
  const abortRef=useRef<AbortController|null>(null);
  const activeAssistantRef=useRef<string|null>(null);
  const stoppedRef=useRef(false);

  const realMode=mode==="uat";
  const status=useQuery({queryKey:["agent-uat-status",environment],queryFn:({signal})=>api.environmentStatus(environment,signal),enabled:realMode});
  const envState=useQuery({queryKey:["agent-uat-environment",environment],queryFn:({signal})=>api.environmentExecutionState(environment,signal),enabled:realMode&&environment==="china_uat"});
  const persistentCredential=useQuery({queryKey:["agent-persistent-credential",environment],queryFn:({signal})=>api.environmentCredentialStatus(environment,signal),enabled:realMode&&environment==="china_uat"});
  const temporaryCredential=useQuery({queryKey:["agent-temporary-credential",environment],queryFn:({signal})=>api.temporaryCredentialStatus(environment,signal),enabled:realMode&&environment==="china_uat"});
  useEffect(()=>{
    if(temporaryCredential.data?.configured&&temporaryCredential.data.auth_session_id){
      setAuthSessionId(temporaryCredential.data.auth_session_id);setCredentialSource("vault");
    }
  },[temporaryCredential.data]);
  const baseUrl=status.data?.base_url||"https://api-uat.weimeta.cn";
  const target=useMemo(()=>{
    try{const base=new URL(baseUrl);const url=new URL(fullUrl);return url.origin===base.origin?{path:url.pathname,error:""}:{path:"",error:"URL 必须属于国内 UAT 环境。"}}
    catch{return {path:"",error:fullUrl?"请输入合法的完整 URL。":""}}
  },[baseUrl,fullUrl]);
  const isChat=target.path===CHAT_PATH;
  const needsContent=!(["GET","HEAD","OPTIONS"] as Method[]).includes(method)||isChat;
  const adapterError=(["POST","PUT","PATCH","DELETE"] as Method[]).includes(method)&&target.path&&target.path!==CHAT_PATH
    ?"当前接口没有自然语言请求适配器，不能自动构造请求。":null;
  const canSend=realMode&&Boolean(envState.data?.enabled)&&!target.error&&!adapterError&&Boolean(fullUrl&&(apiKey||authSessionId)&&method)
    &&(!needsContent||Boolean(input.trim()))&&(!isChat||selectionMode==="automatic"||Boolean(model.trim()))&&stage!=="calling"&&stage!=="receiving";

  const parsePairs=(value:string,label:string):UatWorkbenchPair[]=>{
    let parsed:unknown;
    try{parsed=JSON.parse(value)}catch{throw new Error(`${label} 必须是合法 JSON。`)}
    if(!Array.isArray(parsed))throw new Error(`${label} 必须是数组。`);
    return parsed.map((item,index)=>{
      if(!item||typeof item!=="object")throw new Error(`${label} 第 ${index+1} 项格式不正确。`);
      const row=item as Record<string,unknown>;
      if(typeof row.name!=="string"||typeof row.value!=="string")throw new Error(`${label} 第 ${index+1} 项必须包含字符串 name 和 value。`);
      return {name:row.name,value:row.value,enabled:row.enabled!==false};
    });
  };
  const buildRequest=(authSessionId:string):UatWorkbenchBody=>{
    // Directory/status responses can be very large and are not conversation
    // turns.  Never feed them into a later completion request.
    const history=messages.filter(item=>item.chatContext).map(item=>({role:item.role,content:item.content}));
    const chatMessages=[...(systemPrompt.trim()?[{role:"system",content:systemPrompt.trim()}]:[]),...history,...(input.trim()?[{role:"user",content:input.trim()}]:[])];
    const bodyValue=isChat?{
      ...(selectionMode==="specified"&&model.trim()?{model:model.trim()}:{}),messages:chatMessages,stream,
      temperature:Number(temperature),max_tokens:Number(maxTokens),
    }:null;
    return {method,environment_id:"china_uat",path:target.path,
      query_params:parsePairs(paramsText,"Query Params"),headers:parsePairs(headersText,"自定义 Headers"),
      auth_session_id:authSessionId,auth:{method:"bearer",header_name:"Authorization",prefix:"Bearer"},
      body:{type:bodyValue?"json":"none",value:bodyValue},content_type:bodyValue?"application/json":null,
      timeout_seconds:Number(timeout),stream,model:selectionMode==="specified"?model.trim()||null:null,
      model_selection_mode:selectionMode,channel_id:null,routing_policy:isChat?(selectionMode==="automatic"?"latency_first":"direct_model"):null,
      execution_limits:{max_attempts:Number(maxAttempts)},request_type:"text",multimodal_validation_id:null};
  };

  const ensureCredential=async()=>{
    if(apiKey.trim()){
      const saved=await api.setTemporaryCredential("china_uat",apiKey.trim());
      if(!saved.auth_session_id)throw new Error("临时凭据会话未建立。");
      setAuthSessionId(saved.auth_session_id);setCredentialSource("manual");return saved.auth_session_id;
    }
    if(authSessionId)return authSessionId;
    throw new Error("请输入 API Key，或明确选择已保存的后端加密凭据。");
  };
  const refreshModels=useMutation({mutationFn:async()=>{
    const sessionId=await ensureCredential();
    return api.testTemporaryCredential("china_uat",sessionId,{method:"bearer",header_name:"Authorization",prefix:"Bearer"});
  },onSuccess:data=>{setCatalogModels(data.models??[]);setCatalogMeta({count:data.model_count,testedAt:data.tested_at});setLocalError("")},onError:error=>setLocalError(messageOf(error))});
  const useVaultCredential=useMutation({mutationFn:async()=>{
    // A password manager may try to restore the last manually entered secret
    // when this component rerenders.  The vault flow never needs that value in
    // the browser, so clear it both before and after the server-side restore.
    setApiKey("");setShowKey(false);
    const restored=await api.restoreTemporaryCredentialFromVault("china_uat");
    if(!restored.auth_session_id)throw new Error("后端加密凭据未能建立临时会话。");
    setAuthSessionId(restored.auth_session_id);setCredentialSource("vault");
    return api.testTemporaryCredential("china_uat",restored.auth_session_id,{method:"bearer",header_name:"Authorization",prefix:"Bearer"});
  },onSuccess:data=>{setApiKey("");setCatalogModels(data.models??[]);setCatalogMeta({count:data.model_count,testedAt:data.tested_at});setLocalError("")},onError:error=>setLocalError(messageOf(error))});

  const execute=useMutation({mutationFn:async()=>{
    stoppedRef.current=false;setLocalError("");setStage("preparing");
    if(!canSend)throw new Error(target.error||adapterError||(!(apiKey||authSessionId)?"请输入 API Key，或选择已保存的后端加密凭据。":"请填写完整请求配置。"));
    const userText=input.trim();setMessages(current=>[...current,{id:uid(),role:"user",content:userText||`${method} ${target.path}`,chatContext:isChat}]);setInput("");
    setStage(selectionMode==="automatic"?"routing":"calling");
    const sessionId=await ensureCredential();
    const controller=new AbortController();abortRef.current=controller;setStage("calling");
    const request=buildRequest(sessionId);setStage("receiving");
    if(!stream)return {result:await api.executeWorkbenchRequest("china_uat",request,controller.signal),assistantId:null};
    const assistantId=uid();activeAssistantRef.current=assistantId;
    setMessages(current=>[...current,{id:assistantId,role:"assistant",content:"",chatContext:isChat}]);
    let finalResult:UatWorkbenchResult|undefined;
    await api.streamWorkbenchRequest("china_uat",request,event=>{
      if(event.type==="stage")setStage(event.stage==="calling"?"calling":"receiving");
      if(event.type==="delta"){
        setStage("receiving");
        setMessages(current=>current.map(item=>item.id===assistantId?{...item,content:item.content+event.content}:item));
      }
      if(event.type==="result")finalResult=event.result;
      if(event.type==="error")throw new Error(`${event.message}（${event.code}）`);
    },controller.signal);
    if(!finalResult)throw new Error("流式请求结束，但没有收到最终执行结果。");
    return {result:finalResult,assistantId};
  },onSuccess:({result,assistantId})=>{
    const content=result.execution_status==="success"?parseAssistantText(result):friendlyFailure(result);
    setMessages(current=>assistantId
      ?current.map(item=>item.id===assistantId?{...item,content:item.content||content,result}:item)
      :[...current,{id:uid(),role:"assistant",content,result,chatContext:isChat}]);
    setStage("completed");abortRef.current=null;activeAssistantRef.current=null;
    for(const key of [["routing-runs"],["console-overview"],["call-log-analytics"],["dynamic-metrics"]])qc.invalidateQueries({queryKey:key});
  },onError:error=>{
    abortRef.current=null;
    if(stoppedRef.current){
      const activeId=activeAssistantRef.current;activeAssistantRef.current=null;
      setMessages(current=>activeId
        ?current.map(item=>item.id===activeId?{...item,content:item.content||"用户已停止生成。",stopped:true}:item)
        :[...current,{id:uid(),role:"assistant",content:"用户已停止生成。",stopped:true,chatContext:isChat}]);
      setStage("stopped");return;
    }
    setLocalError(messageOf(error));setMessages(current=>[...current,{id:uid(),role:"assistant",content:`请求未完成：${messageOf(error)}`,chatContext:false}]);setStage("failed");
  }});

  const send=()=>{if(!execute.isPending)execute.mutate()};
  const onComposerKeyDown=(event:KeyboardEvent<HTMLTextAreaElement>)=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();if(canSend)send()}};
  const stop=()=>{stoppedRef.current=true;abortRef.current?.abort()};
  const clearKey=()=>{setApiKey("");setAuthSessionId(null);setCredentialSource(null);setShowKey(false);setCatalogModels([]);setCatalogMeta(null);api.clearTemporaryCredential("china_uat").catch(()=>undefined)};
  const progress=stage==="preparing"?"正在准备请求":stage==="routing"?"正在选择模型和渠道":stage==="calling"?"正在调用国内 UAT":stage==="receiving"?"正在接收响应":stage==="completed"?"请求完成":stage==="stopped"?"用户已停止":"";
  const preview=useMemo(()=>isChat?{model:selectionMode==="automatic"?"[自动调度]":model||"[未选择]",messages:"[当前对话内容已脱敏]",stream,temperature:Number(temperature),max_tokens:Number(maxTokens)}:null,[isChat,selectionMode,model,stream,temperature,maxTokens]);

  return <div className="agent-request-page">
    <PageHeader title="发起调度" description="输入任务，系统将使用所选模型或自动调度完成国内 UAT 请求。" actions={<Link className="secondary button-link" to="/routing/runs">执行记录</Link>}/>
    <section className="agent-config-bar" aria-label="请求配置">
      <label className="agent-url-field"><span>URL</span><input aria-label="完整 UAT URL" value={fullUrl} onChange={event=>setFullUrl(event.target.value)} placeholder="https://api-uat.weimeta.cn/v1/chat/completions" spellCheck={false}/></label>
      <label><span>HTTP Method</span><select aria-label="HTTP Method" value={method} onChange={event=>setMethod(event.target.value as Method)}>{METHODS.map(item=><option key={item}>{item}</option>)}</select></label>
      <fieldset><legend>响应方式</legend><label><input type="radio" name="stream" checked={stream} onChange={()=>setStream(true)}/> 流式</label><label><input type="radio" name="stream" checked={!stream} onChange={()=>setStream(false)}/> 非流式</label></fieldset>
      <label className="agent-key-field"><span>API Key</span><span className="agent-inline-input"><input aria-label="API Key" name="uat-temporary-credential" type={showKey?"text":"password"} value={apiKey} onChange={event=>{setApiKey(event.target.value);setAuthSessionId(null);setCredentialSource(null);setCatalogMeta(null);setCatalogModels([])}} placeholder={credentialSource==="vault"?"已使用后端加密凭据":"输入当前会话 API Key"} autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" data-form-type="other"/><button type="button" onClick={()=>setShowKey(value=>!value)}>{showKey?"隐藏":"显示"}</button><button type="button" onClick={clearKey}>清除</button></span>{persistentCredential.data?.configured&&!apiKey&&<button className="agent-vault-credential" type="button" disabled={useVaultCredential.isPending} onClick={()=>useVaultCredential.mutate()}>{useVaultCredential.isPending?"正在验证…":credentialSource==="vault"?"后端加密凭据已选用":"使用已保存的后端凭据"}</button>}</label>
      <label className="agent-model-field"><span>模型</span><select aria-label="模型选择方式" value={selectionMode} onChange={event=>setSelectionMode(event.target.value as "automatic"|"specified")}><option value="automatic">自动调度</option><option value="specified">指定真实模型</option></select></label>
      {selectionMode==="specified"&&<label className="agent-model-id"><span>模型 ID</span><span className="agent-inline-input"><input aria-label="搜索或输入模型 ID" list="agent-model-options" value={model} onChange={event=>setModel(event.target.value)} placeholder="搜索或手动输入真实模型 ID"/><datalist id="agent-model-options">{catalogModels.map(item=><option key={item.id} value={item.id}>{item.display_name} · {item.owned_by||"Provider 未提供"}</option>)}</datalist><button type="button" disabled={refreshModels.isPending} onClick={()=>refreshModels.mutate()}>{refreshModels.isPending?"读取中…":"刷新目录"}</button></span></label>}
      {selectionMode==="automatic"&&<button className="agent-catalog-action" type="button" disabled={refreshModels.isPending} onClick={()=>refreshModels.mutate()}>{refreshModels.isPending?"读取模型中…":"刷新真实模型目录"}</button>}
      <div className="agent-config-status"><StatusBadge tone={envState.data?.enabled?"success":"danger"}>{envState.data?.enabled?"国内 UAT 已开启":"国内 UAT 已关闭"}</StatusBadge>{catalogMeta&&<span>真实目录 {catalogMeta.count} 个 · {formatLocalTime(catalogMeta.testedAt)}</span>}</div>
    </section>
    {target.error&&<div className="agent-blocker" role="alert">{target.error}</div>}
    {adapterError&&<div className="agent-blocker" role="alert">{adapterError}</div>}
    {isChat&&<div className="agent-capability-note">当前模型能力信息尚未审核时，本次普通文本请求仍会通过真实 UAT 调用验证。</div>}

    <main className="agent-conversation" aria-live="polite">
      {messages.length===0?<div className="agent-empty">输入任务，系统将使用所选模型或自动调度完成请求。</div>:messages.map(message=><article key={message.id} className={`agent-message ${message.role}`}>
        <header>{message.role==="user"?"你":"任务结果"}</header><div className="agent-message-content">{message.content}</div>
        {message.stopped&&<div className="agent-stopped-label">用户已停止</div>}
        {message.result&&<><div className="agent-result-summary">状态 {message.result.http_status} · 模型 {message.result.actual_model||message.result.requested_model||"未提供"} · 渠道 {message.result.channel_id||"未提供"} · {(message.result.total_latency_ms/1000).toFixed(2)}s · {message.result.total_tokens??"Token 未提供"}{message.result.total_tokens!=null?" Token":""} · {message.result.cost_amount==null?"费用待 Provider 日志同步":`${message.result.currency||"CNY"} ${message.result.cost_amount}`}</div><div className="agent-result-actions"><button onClick={()=>navigator.clipboard?.writeText(message.content)}>复制结果</button><button onClick={()=>setInput(messages.filter(item=>item.role==="user").at(-1)?.content||"")}>重新发送</button><button onClick={()=>setInput("")}>继续追问</button></div><ResultDetails result={message.result}/></>}
      </article>)}
      {progress&&stage!=="completed"&&<div className="agent-progress"><span className="agent-pulse"/>{progress}</div>}
    </main>

    <details className="agent-advanced"><summary>高级设置</summary><div className="agent-advanced-grid">
      <label>System Prompt<textarea value={systemPrompt} onChange={event=>setSystemPrompt(event.target.value)}/></label>
      <label>Temperature<input type="number" min="0" max="2" step="0.1" value={temperature} onChange={event=>setTemperature(event.target.value)}/></label>
      <label>max_tokens<input type="number" min="1" value={maxTokens} onChange={event=>setMaxTokens(event.target.value)}/></label>
      <label>超时时间（秒）<input type="number" min="1" max="60" value={timeout} onChange={event=>setTimeoutValue(event.target.value)}/></label>
      <label>最大尝试次数<input type="number" min="1" value={maxAttempts} onChange={event=>setMaxAttempts(event.target.value)}/></label>
      <label>Query Params<textarea aria-label="Query Params" value={paramsText} onChange={event=>setParamsText(event.target.value)} placeholder='[{"name":"q","value":"关键词","enabled":true}]'/></label>
      <label>自定义 Headers<textarea aria-label="自定义 Headers" value={headersText} onChange={event=>setHeadersText(event.target.value)} placeholder='[{"name":"Accept-Language","value":"zh-CN","enabled":true}]'/></label>
    </div><details><summary>原始请求预览 / 开发者 JSON 模式</summary><pre>{JSON.stringify(preview,null,2)}</pre></details></details>

    <section className="agent-composer" aria-label="请求输入区"><label htmlFor="agent-prompt">请求内容</label><textarea id="agent-prompt" value={input} onChange={event=>setInput(event.target.value)} onKeyDown={onComposerKeyDown} placeholder="输入你希望模型完成的任务……" disabled={execute.isPending}/><div className="agent-composer-actions"><span>Enter 发送 · Shift+Enter 换行</span><button className="secondary" type="button" onClick={()=>setInput("")} disabled={!input||execute.isPending}>清空</button>{execute.isPending?<button className="danger" type="button" onClick={stop}>停止生成</button>:<button className="primary" type="button" disabled={!canSend} onClick={send}>发送请求</button>}</div></section>
    {localError&&<div className="agent-error" role="alert">{localError}</div>}
    <p className="api-security-footnote">API Key 仅进入当前后端临时会话，不写入浏览器存储、URL、请求预览或调用日志。</p>
  </div>;
}
