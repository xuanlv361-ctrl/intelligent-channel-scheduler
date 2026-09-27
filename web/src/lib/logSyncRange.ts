export type LogSyncQuickRange="15m"|"1h"|"24h"|"today"|"custom";

const pad=(value:number)=>String(value).padStart(2,"0");
export function toLocalDateTimeInput(value:Date){
 return `${value.getFullYear()}-${pad(value.getMonth()+1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}
export function calculateQuickRange(kind:Exclude<LogSyncQuickRange,"custom">,now=new Date()){
 const end=new Date(now);
 const start=new Date(now);
 if(kind==="15m")start.setMinutes(start.getMinutes()-15);
 if(kind==="1h")start.setHours(start.getHours()-1);
 if(kind==="24h")start.setHours(start.getHours()-24);
 if(kind==="today")start.setHours(0,0,0,0);
 return {dateFrom:toLocalDateTimeInput(start),dateTo:toLocalDateTimeInput(end)};
}
export function browserTimezone(){
 return Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC";
}
export function validateLogSyncRange(dateFrom:string,dateTo:string,now=new Date(),maximumHours=168,clockSkewSeconds=300){
 if(!dateFrom||!dateTo)return "必须填写开始和结束日期时间";
 const start=new Date(dateFrom);
 const end=new Date(dateTo);
 if(Number.isNaN(start.getTime())||Number.isNaN(end.getTime()))return "日期时间格式无效";
 if(start.getTime()>=end.getTime())return "开始时间必须早于结束时间";
 if(end.getTime()>now.getTime()+clockSkewSeconds*1000)return "结束时间超过允许的时钟偏差";
 if(end.getTime()-start.getTime()>maximumHours*3600000)return `时间范围不能超过 ${maximumHours} 小时`;
 return null;
}
export function rangeAsUtc(dateFrom:string,dateTo:string){
 return {dateFromUtc:new Date(dateFrom).toISOString(),dateToUtc:new Date(dateTo).toISOString()};
}
