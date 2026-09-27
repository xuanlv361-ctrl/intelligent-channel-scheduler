export const sourceLabels={
  demo_mock:"演示数据",integration_test_fixture:"集成测试数据",measured_uat:"真实 UAT 数据",
  measured_unified_uat:"统一路由 UAT 数据",backend_log_export:"后端日志证据",unknown:"待确认"
  ,measured_uat_browser_collector:"真实 UAT 浏览器采集证据"
  ,standardized_call_logs:"标准化真实调用日志"
} as const;
export const enumLabels:Record<string,string>={
  budget_ok:"预算正常",near_limit:"接近预算上限",blocked:"已阻止",cost_anomaly:"成本异常",
  usage_mismatch:"Token 或费用不一致",awaiting_backend_log:"等待后台日志",exact_match:"精确匹配",
  strong_match:"高置信度匹配",ambiguous:"存在多个可能记录",unmatched:"未找到匹配日志",
  manually_confirmed:"人工确认",selected:"已选择",unroutable:"不可调度",healthy:"健康",
  degraded:"性能下降",unhealthy:"不健康",insufficient_data:"样本不足",insufficient:"样本不足",stale:"数据已过期",
  unknown:"待确认",stream_capability_unknown:"流式能力待确认",stream_not_supported:"不支持流式",
  model_mismatch:"模型不匹配",success:"成功",failure:"失败",passed:"通过",failed:"失败",
  unsupported:"不支持",pending_confirmation:"待确认",not_tested:"未测试",available:"可用",
  retryable:"可重试",rate_limited:"请求受限",upstream_5xx:"上游服务异常",gateway_timeout:"网关超时",
  user_parameter_error:"请求参数错误",user_authentication_error:"用户鉴权失败",
  channel_authentication_error:"渠道鉴权失败",upstream_timeout:"上游超时",sse_incomplete:"流式响应不完整",
  budget_exceeded:"预算已超限",local_request_error:"本地请求错误",unknown_error:"未分类错误"
  ,unexpected_model:"实际模型与请求不一致",actual_model_missing:"实际模型待确认",
  exact:"精确匹配",valid:"有效",warning:"警告",rejected:"已拒绝",needs_confirmation:"需要确认",
  immutable_audited:"不可变且已审计",fresh:"数据新鲜",success_response:"成功响应",
  healthy_evidence:"健康证据",never_run:"尚未刷新",running:"刷新中",
  transport:"传输层",provider:"提供方",request:"请求层",parser:"解析层",
  complete:"完整",partial:"部分完整",json:"JSON",text:"文本",
  succeeded:"执行成功",connected:"已连接",not_configured:"未配置",none:"无"
  ,previewed:"等待导入确认",confirmed:"已确认导入",idle:"空闲",ready:"已就绪"
  ,high:"高",medium:"中",low:"低",critical:"严重",cost_concentration:"费用过度集中"
  ,pending_key_validation:"等待密钥验证",read_only_validated:"只读验证完成",
  overseas_completion_not_yet_authorized:"海外完成执行未授权",
  overseas_log_page_unconfirmed:"海外日志页待确认",not_attempted:"尚未尝试",
  operator_confirmed:"操作员已确认",openai_compatible:"OpenAI 兼容协议",
  local_runtime_config:"本地运行时配置",structurally_valid:"结构校验通过"
  ,real_execution_disabled:"UAT 环境当前已关闭，请打开 UAT 环境后再执行。"
  ,capability_pending:"当前模型或渠道的能力信息待确认，暂不能执行真实请求。"
  ,capability_pending_confirmation:"当前模型或渠道的能力信息待确认，暂不能执行真实请求。"
  ,DISABLED:"尚未创建",DRAFT:"草稿",PENDING_APPROVAL:"待审批",APPROVED:"已批准",ACTIVE:"执行中"
  ,COMPLETED:"已完成",STOPPED:"已停止",EXPIRED:"已过期",BUDGET_EXHAUSTED:"预算已耗尽",KILLED:"已终止"
};
export const labelEnum=(value:unknown)=>enumLabels[String(value??"unknown")]||String(value??"待确认");
export const formatSourceType=(value:unknown)=>sourceLabels[String(value??"unknown") as keyof typeof sourceLabels]||"待确认";
export const formatMissingValue=(value:unknown,kind:"unknown"|"unavailable"|"na"|"not-executed"|"blocked"|"no-sample"="unknown")=>{
  if(value!==null&&value!==undefined&&value!=="")return String(value);
  return {unknown:"待确认",unavailable:"暂无数据",na:"不适用","not-executed":"尚未执行",blocked:"已阻止","no-sample":"样本不足"}[kind];
};
export const formatMoney=(value:unknown,places=4)=>value===null||value===undefined||value===""?"待确认":`¥${Number(value).toFixed(places)}`;
export const formatPercentage=(value:unknown,ratio=true)=>value===null||value===undefined||value===""?"暂无数据":`${(Number(value)*(ratio?100:1)).toFixed(1)}%`;
export const formatLatency=(value:unknown)=>value===null||value===undefined||value===""?"暂无数据":`${Math.round(Number(value))} ms`;
export const formatTokenCount=(value:unknown)=>value===null||value===undefined||value===""?"待确认":Math.round(Number(value)).toLocaleString("zh-CN");
export const formatTimestamp=(value:unknown)=>!value?"待确认":new Date(String(value)).toLocaleString("zh-CN",{hour12:false});
export const formatBoolean=(value:unknown)=>value===true?"是":value===false?"否":"待确认";
export const formatHttpStatus=(value:unknown)=>!value?"待确认":`${value} ${Number(value)<400?"成功":Number(value)<500?"客户端错误":"服务端错误"}`;
export const formatExecutionStatus=labelEnum;
export const formatCorrelationStatus=labelEnum;
export const formatHealthStatus=labelEnum;
export const formatErrorCategory=labelEnum;
export const formatMappingStatus=labelEnum;
export const formatCompatibilityStatus=labelEnum;
export const suspiciousMojibake=(value:string)=>/缁楊|濞撶|閸氬|缂栫|鐎瑰|閺嗗|闁告|锟斤拷|�/.test(value);
