import {useQuery} from "@tanstack/react-query";
import {Link,useSearchParams} from "react-router-dom";
import {api,DataMode,EnvironmentFilter,SearchItem} from "../services/api";
import {formatSourceType} from "../lib/presentation";

const ENTITY_LABELS:Record<string,string>={
  request:"请求",execution:"执行记录",decision:"决策",error:"错误",channel:"渠道",
  model:"模型",import:"导入证据",collection:"采集证据",report:"报告",cost:"成本记录",
};

function safeDestination(value:string){
  return value.startsWith("/")&&!value.startsWith("//")?value:null;
}

function PageHeader({description}:{description?:string}){
  return <header className="page-title">
    <p className="eyebrow">LOCAL EVIDENCE SEARCH</p>
    <h1>全局搜索</h1>
    {description&&<p>{description}</p>}
  </header>;
}

export default function SearchPage({environment,mode}:{environment:EnvironmentFilter;mode:DataMode}){
  const [params,setParams]=useSearchParams();
  const query=(params.get("q")||"").trim();
  const cursor=params.get("cursor")||undefined;
  const sourceType=mode==="demo"?"demo_mock":undefined;
  const result=useQuery({
    queryKey:["global-search",query,environment,sourceType||"all",cursor||""],
    queryFn:({signal})=>api.search(query,environment,sourceType,50,cursor,signal),
    enabled:Boolean(query),
  });
  if(!query)return <section className="search-page"><PageHeader description="在顶部输入关键词，搜索当前环境与数据来源范围内的本地证据。"/><div className="state empty">请输入请求、渠道、模型、错误或证据 ID。</div></section>;
  if(result.isLoading)return <section className="search-page"><PageHeader/><div className="state loading" role="status">正在搜索本地证据…</div></section>;
  if(result.error)return <section className="search-page"><PageHeader/><div className="state error" role="alert"><b>全局搜索加载失败。</b><button onClick={()=>result.refetch()}>重试</button></div></section>;
  const data=result.data!;
  const groups=data.items.reduce<Record<string,SearchItem[]>>((acc,item)=>{
    (acc[item.entity_type]??=[]).push(item);return acc;
  },{});
  return <section className="search-page">
    <PageHeader description={`“${data.query}”在当前范围内匹配 ${data.items.length} 条证据。`}/>
    <div className="search-provenance">
      <span>环境：{data.environment_id==="all"?"全部环境":data.environment_id==="china_uat"?"国内 UAT":"海外站"}</span>
      <span>数据来源：{formatSourceType(data.provenance?.source_type||data.source_type)}</span>
      <span>仅展示脱敏摘要</span>
    </div>
    {data.items.length===0?<div className="state empty">当前环境与数据来源范围内没有匹配证据。</div>:Object.entries(groups).map(([type,items])=>
      <section className="panel search-group" key={type}><header><h2>{ENTITY_LABELS[type]||type}</h2><span>{data.groups?.[type]??items.length} 条</span></header>
        <div className="search-results">{items.map(item=>{
          const destination=safeDestination(item.destination_path);
          return <article className="search-result" key={`${item.environment_id}:${item.entity_type}:${item.primary_id}`}>
            <div><span className={item.is_mock?"badge badge-demo":"badge badge-uat"}>{item.is_mock?"演示数据":"真实证据"}</span><small>{item.environment_id==="china_uat"?"国内 UAT":item.environment_id==="overseas"?"海外站":item.environment_id} · {formatSourceType(item.source_type)}</small></div>
            <h3>{destination?<Link to={destination}>{item.title}</Link>:item.title}</h3>
            <p>{item.summary}</p><footer><code>{item.primary_id}</code>{item.sample_or_evidence_id&&<span>证据：{item.sample_or_evidence_id}</span>}{item.updated_at&&<time>{item.updated_at}</time>}</footer>
          </article>;
        })}</div>
      </section>)}
    {data.next_cursor&&<button className="secondary" onClick={()=>{const next=new URLSearchParams(params);next.set("cursor",data.next_cursor!);setParams(next)}}>下一页</button>}
  </section>;
}
