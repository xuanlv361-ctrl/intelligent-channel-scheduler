/* eslint-disable react-refresh/only-export-components */
import type {ReactNode} from "react";

export type StatusTone="success"|"warning"|"danger"|"neutral"|"info";

export function PageHeader({title,description,actions}:{title:string;description:string;actions?:ReactNode}){
  return <header className="console-page-header"><div><p className="console-eyebrow">INTELLIGENT ROUTING</p><h1>{title}</h1><p>{description}</p></div>{actions&&<div className="page-actions">{actions}</div>}</header>;
}
export function StatusBadge({tone="neutral",children}:{tone?:StatusTone;children:ReactNode}){
  return <span className={`status-badge status-${tone}`}>{children}</span>;
}
export function MetricCard({label,value,meta,children}:{label:string;value:ReactNode;meta?:ReactNode;children?:ReactNode}){
  return <article className="compact-metric"><span>{label}</span><strong>{value}</strong>{meta&&<small>{meta}</small>}{children}</article>;
}
export function EmptyState({title,description,action}:{title:string;description:string;action?:ReactNode}){
  return <div className="console-empty" role="status"><div aria-hidden="true">○</div><b>{title}</b><p>{description}</p>{action}</div>;
}
export function ErrorState({title,description,onRetry}:{title:string;description:string;onRetry?:()=>void}){
  return <div className="console-error" role="alert"><b>{title}</b><p>{description}</p>{onRetry&&<button className="secondary" onClick={onRetry}>重新加载</button>}</div>;
}
export function Section({title,description,actions,children,className=""}:{title:string;description?:string;actions?:ReactNode;children:ReactNode;className?:string}){
  return <section className={`console-section ${className}`}><header><div><h2>{title}</h2>{description&&<p>{description}</p>}</div>{actions&&<div className="section-actions">{actions}</div>}</header>{children}</section>;
}
export function formatValue(value:unknown,fallback="未提供"){
  if(value===null||value===undefined||value==="")return fallback;
  if(typeof value==="boolean")return value?"已开启":"已关闭";
  return String(value);
}
export function formatLocalTime(value:unknown){
  if(!value)return "未提供";
  const date=new Date(String(value));
  return Number.isNaN(date.getTime())?"未提供":date.toLocaleString("zh-CN",{hour12:false});
}
