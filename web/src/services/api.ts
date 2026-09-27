import {apiRequest,apiText,apiNdjson,ApiError} from '../lib/apiClient';
import type {CostBudgetResponse} from '../types/costBudget';
export {ApiError};
export type DataMode='demo'|'uat';
export type EnvironmentId='china_uat'|'overseas';
export type EnvironmentFilter='all'|EnvironmentId;
export interface PlatformEnvironment{
  environment_id:EnvironmentId;display_name:string;console_base_url:string|null;
  api_base_url:string|null;models_path:string|null;chat_completions_path:string|null;
  logs_page_url:string|null;currency:string|null;
  allowed_api_hosts?:string[];api_protocol?:'openai_compatible'|null;
  configuration_status:string;models_endpoint_status?:string;
  completion_endpoint_status?:string;log_page_status?:string;currency_status?:string;
  documentation_source?:string|null;documentation_checked_at?:string|null;
  real_execution_supported:boolean;key_configured?:boolean;
}
export interface EnvironmentListResponse{config_version:string;environments:PlatformEnvironment[]}
export interface OverseasLogPageStatus{
  environment_id:'overseas';log_page_status:string;logs_page_url:string|null;
  source_type:string|null;structural_validation_status:string;
  operator_confirmed_at:string|null;browser_live_validation_status:string;
  latest_collection_at:string|null;latest_record_count:number|null;
  latest_collection_id:string|null;value_sha256:string|null;
}
export interface OverseasLogPagePreview{
  validation_id:string;environment_id:'overseas';normalized_url:string;host:string;
  https:boolean;structurally_valid:boolean;validation_status:string;
  live_validation_status:string;warnings:string[];value_sha256:string;
}
export interface DomesticLogPageStatus{
  environment_id:'china_uat';log_page_status:string;logs_page_url:string|null;
  source_type:string|null;structural_validation_status:string;
  operator_confirmed_at:string|null;value_sha256:string|null;
  setting_version:string;allowed_origin:string|null;log_page_path:string|null;
  blocking_reason:string|null;reviewed_url:string;reviewed_origin:string;
  reviewed_path:string;configuration_reason:string|null;
}
export interface DomesticLogPagePreview{
  validation_id:string;environment_id:'china_uat';normalized_url:string;
  host:string;allowed_origin:string;path:string;setting_version:string;
  https:boolean;structurally_valid:boolean;validation_status:string;
  warnings:string[];value_sha256:string;
}
export interface ImportIssue{row:number;code:string;severity:string}
export interface ImportBatch{
  batch_id:string;import_batch_id:string;source_sha256:string;source_type:string;
  created_at:string;imported_at:string;row_count:number;imported_row_count:number;
  valid_count:number;warning_count:number;rejected_count:number;needs_confirmation_count:number;
  audit_status:string;detected_columns:string[];issues:ImportIssue[];
  normalized_preview:Array<Record<string,unknown>>;provenance:{filename:string;immutable:boolean};
}
export interface OverviewResponse{
  mode:DataMode;data_mode?:'real'|'mock';environment_id?:string;time_range?:string;
  source_type:string|null;source_types?:string[];is_mock?:boolean;records:number;
  channels:number;channel_summary?:Array<{channel_id:string;channel_name:string;request_count:number;
    success_count:number;success_rate:number;share:number|null;last_observation:string}>;
  healthy:number;errors:number;sample_size:number;last_updated:string|null;
  data_as_of?:string|null;last_synced_at?:string|null;warnings:string[];
  blockers?:Array<{code:string;message:string;action_path:string}>;
  routing_distribution:Record<string,number>;
  budget:{today_uat_spend:number|string|null;remaining_budget:number|null};
  runtime?:{environment_enabled:boolean;environment_state_source:string;
    credential_configured:boolean;credential_source:string;credential_fingerprint?:string|null;
    connection_status:string;log_sync_status:string;freshness_status:string};
  metrics?:CallLogAnalytics;
  recent_executions?:StandardizedCallLog[];
  historical_failures?:{count:number;classification:string;blocks_current_execution:false};
  channel_evidence_note?:string;
}
export interface HealthCard{channel_id:string;channel_name:string;sample_size:number;observed_success_rate:number|null;p50_latency:number|null;confidence:number;health_score:number|null;health_state:string;freshness_status:string;last_observation:string;source_type:string;evidence_limitations:string}
export interface ManagedSkillItem{
  skill_id:string;display_name:string;description:string;source_type:string;version:string;
  version_status:'draft'|'validated'|'published'|'deprecated';
  installation_status:'not_installed'|'installed'|'enabled'|'disabled'|'uninstalled';
  installed_version:string|null;requested_permissions:string[];page_ids:string[];
  binding?:string;input_schema:{required?:string[];properties?:Record<string,{type?:string;title?:string}>};last_invoked_at:string|null;
}
export interface ManagedSkillCatalog{items:ManagedSkillItem[];registered_count:number;published_count:number;
  installed_count:number;enabled_count:number;pending_count:number;audit_count:number;last_synced_at:string}
export interface ManagedSkillInvocation{invocation_id:string;audit_id:string;operator_id:string;skill_id:string;
  skill_version:string;started_at:string;finished_at:string;permission_decision:string;execution_result:string;
  data:Record<string,unknown>;uncertainty:string;network_called:boolean;write_performed:boolean}
export type SkillHostStatus='not_configured'|'pending_test'|'testing'|'connected'|'authentication_failed'|
  'network_failed'|'protocol_incompatible'|'disabled';
export interface ManagedSkillHost{
  host_id:string;host_name:string;host_type:string;base_url:string|null;environment:string;protocol:string;
  protocol_version:string|null;auth_type:string;credential_fingerprint:string|null;status:SkillHostStatus;
  enabled:boolean;capabilities:string[];installed_skill_count:number;last_tested_at:string|null;
  last_test_result:Record<string,unknown>|null;last_invoked_at:string|null;created_at:string;updated_at:string;
}
export interface ManagedSkillHostList{items:ManagedSkillHost[];external_host_status:string;connected_count?:number}
export interface ManagedSkillHostInput{
  host_name:string;host_type:string;base_url:string;environment:string;protocol:string;protocol_version?:string;
  auth_type:string;
}
export interface ManagedSkillHostTestResult{
  status:SkillHostStatus;http_status?:number|null;latency_ms?:number|null;tls_result?:string|null;
  authentication_result?:string|null;protocol_version?:string|null;host_version?:string|null;
  capability_count?:number;installed_skill_count?:number;tested_at:string;safe_error?:string|null;
  tls?:string|null;authentication?:string|null;error_code?:string|null;audit_id?:string;
}
export interface ManagedSkillHostInstallation{
  installation_id:string;host_id:string;skill_id:string;skill_version:string;status:string;installed_at:string;
}
export interface ManagedSkillHostInvocation{
  invocation_id:string;audit_id:string;skill_id:string;skill_version:string;host_id:string;protocol_version:string|null;
  started_at:string;finished_at:string;latency_ms:number;result?:Record<string,unknown>;safe_result?:Record<string,unknown>;
  status?:string;execution_result?:string;network_called:boolean;write_occurred:boolean;request_id?:string|null;decision_id?:string|null;
}
export interface HealthResponse{mode:DataMode;source_type:string|null;last_updated:string|null;items:HealthCard[]}
export interface AcceptanceLiveRow{
  requested_model:string;actual_model:string|null;stream:boolean;request_status:string;
  http_status:number|null;request_id:string|null;response_id:string|null;decision_id:string|null;
  total_latency_ms:number|null;first_token_latency_ms:number|null;input_tokens:number|null;
  cached_input_tokens:number|null;output_tokens:number|null;cost_amount:string|null;currency:string|null;
  error_category:string|null;error_source:string|null;is_fault_injected:boolean;
  strategy_variant:string|null;traffic_proposal_id:string|null;occurred_at:string;
}
export interface AcceptanceRunSummary{
  acceptance_run_id:string;status:string;started_at:string;
  evidence_counts:{historical:number;realtime:number;injected:number;provider_live:number;total:number};
  six_model_results:AcceptanceLiveRow[];fault_injection_results:AcceptanceLiveRow[];
  traffic_proposals:Array<Record<string,unknown>>;evidence_index:Record<string,string[]>;
}
export interface CollectorStatus{status:string;environment:string;environment_id:EnvironmentId;environment_name:string;read_only:boolean;allowed_hosts:string[];source_type:string|null;console_url:string|null;console_host:string|null;log_page_url:string|null;currency:string|null;configuration_consistent:boolean;blocking_reason?:string|null;authentication:string;cookies_persisted:boolean;credentials_persisted:boolean;default_limits:{maximum_pages:number;maximum_records:number;page_delay_ms:number}}
export interface CollectorCollection{collection_id:string;collected_at:string;status:string;source_type:string;environment:string;environment_id:EnvironmentId;record_count:number;duplicate_count:number;rejected_count:number;payload_sha256:string;import_batch_id:string|null}
export interface CollectorRun{
  run_id:string;environment_id:EnvironmentId;source_type:string;status:string;created_at:string;
  started_at:string|null;finished_at:string|null;date_from:string;date_to:string;
  maximum_pages:number;maximum_records:number;page_delay_ms:number;login_confirmed_at:string|null;
  current_page:number;captured_count:number;accepted_count:number;duplicate_count:number;
  rejected_count:number;elapsed_ms:number;last_heartbeat_at:string|null;collection_id:string|null;
  preview_payload_sha256:string|null;current_safe_url:string|null;warnings:string[];
  error_code:string|null;safe_error_message:string|null;stop_requested:boolean;
}
export interface CollectorRunPreview{
  collection_id:string;environment_id:EnvironmentId;environment_name?:string;source_type:string;
  record_count:number;captured_count:number;duplicate_count:number;rejected_count:number;
  payload_sha256:string;warnings:string[];records:Array<Record<string,unknown>>;
}
export interface LogSyncJob{
  sync_job_id:string;environment_id:EnvironmentId;state:string;created_at:string;
  started_at:string|null;stopped_at:string|null;last_poll_at:string|null;
  last_successful_poll_at:string|null;last_observed_log_time:string|null;
  safe_log_page_url:string;current_safe_url?:string|null;collected_count:number;
  inserted_count:number;duplicate_count:number;rejected_count:number;
  out_of_range_count:number;missing_timestamp_count:number;
  correlated_count:number;ambiguous_count:number;error_code:string|null;
  safe_error_message:string|null;stop_reason:string|null;error_at:string|null;
  suggested_next_action:string|null;browser_context_active:boolean;
  cookies_persisted:boolean;credentials_persisted:boolean;network_called:boolean;
  poll_interval_seconds:number;maximum_records:number;source_type:string;
  date_from_utc:string;date_to_utc:string;timezone:string;schema_status:string;
  periodic_polling:boolean;http_read_count:number;maximum_http_reads:number;
  page_size:number;maximum_pages:number;current_page:number;pages_read:number;
  consecutive_empty_reads:number;consecutive_failures:number;
  last_successful_read_at:string|null;watermark_utc:string|null;
  source_cursor:string|null;last_response_envelope_sha256:string|null;
  latest_metric_snapshot_id:string|null;latest_confidence_snapshot_id:string|null;
  provenance_manifest_sha256:string|null;
  schema_fingerprint?:string|null;schema_adapter_id?:string|null;
  adapter_version?:string|null;last_rejection_reason?:string|null;
}
export interface ShadowLogItem{
  record_id:string;platform_log_id:string|null;environment_id:string;occurred_at:string;
  request_id:string|null;response_id:string|null;decision_id:string|null;model:string|null;
  provider_request_id?:string|null;provider_response_id?:string|null;provider_trace_id?:string|null;
  actual_channel:string|null;shadow_recommendation:string|null;correlation_status:string;
  correlation_method:string|null;execution_id:string|null;comparable:boolean;matches:boolean|null;
  latency_ms:number|null;ttft_ms:number|null;actual_cost:number|null;currency:string|null;
  estimated_cost:number|null;cost_difference:number|null;http_status:number|null;
  result:string|null;error_category:string|null;source_type:string;sync_job_id:string;is_mock:false;
}
export interface ShadowDashboard{
  environment_id:string;source_type:string;is_mock:boolean;connection_status:string;
  sync_status:string;sync_interval_seconds:number;auto_sync_enabled:boolean;last_successful_at:string|null;
  next_sync_at:string|null;last_error:string|null;last_safe_error_message:string|null;
  cursor:Record<string,unknown>|null;latest_job:LogSyncJob|null;
  metrics:{synced_logs:number;linked_executions:number;unlinked_logs:number;
    comparable_logs:number;agreement_rate:number|null;average_latency_difference_ms:number|null;
    estimated_cost_difference:number|null};
  items:ShadowLogItem[];unlinked:ShadowLogItem[];sync_history:LogSyncJob[];
  association_status?:string;shadow_status?:string;
  technical:Record<string,unknown>;
}
export interface LogSyncCoordinatorStatus{
  environment_id:"china_uat";status:string;job_id:string|null;started_at:string|null;
  last_success_at:string|null;last_check_at:string|null;watermark:string|null;
  batch_read:number;inserted:number;duplicates:number;failed:number;
  synced_logs:number;linked:number;pending_link:number;shadow_results:number;
  login_status:string;next_schedule_at:string|null;last_error:string|null;
  requires_human:boolean;revision:number;read_only:true;uat_execution_switch_required:false;
}
export interface LogSyncReconciliation{
  execution_id:string|null;platform_log_id:string|null;estimated_cost:number|null;
  actual_cost:number|null;currency:string|null;difference:number|null;
  difference_percent:number|null;reconciliation_status:string;evidence_source:string;
  actual_channel_id:string|null;actual_channel_name:string|null;
  channel_evidence_status:string;scheduler_recommendation:string|null;
}
export interface PersistentSessionStatus{
 enabled:boolean;environment_id:"china_uat";
 browser:null|{state:string;running:boolean;browser_family:"Google Chrome";
  profile_scope:"domestic-uat-chrome";profile_persistent:boolean;
  debug_scope:"127.0.0.1_only";login_url:string};
 session:null|{persistent_session_id:string;environment_id:string;state:string;
  created_at:string;expires_at:string;last_validated_at:string|null;last_used_at:string|null;
  authentication_status:string;revocation_status:string;created_by_local_operator:boolean;
  auto_resume_enabled:boolean;failure_code:string|null;failure_summary:string|null;
  storage_types:string[];revision:number;usage_state:"available"|"in_use";
  lease:null|{lease_id:string;lease_owner_job_id:string;state:string;
   acquired_at:string;heartbeat_at:string;expires_at:string;
   released_at:string|null;release_reason:string|null;generation:number;version:number}};
 pairing:null|{pairing_id:string;environment_id:string;state:string;created_at:string;
  expires_at:string;current_safe_url:string|null;browser_state:string;
  failure_code:string|null;failure_summary:string|null;storage_diagnostic_status:string|null};
 active_job:null|{sync_job_id:string;state:string;last_successful_poll_at:string|null;
  inserted_count:number;correlated_count:number;actual_cost_enriched_count:number;
  lease_id:string;lease_heartbeat_at:string;lease_expires_at:string;lease_generation:number};
 latest_job:null|{sync_job_id:string;state:string;created_at:string;stopped_at:string|null;
  last_successful_poll_at:string|null;collected_count:number;inserted_count:number;
  rejected_count:number;duplicate_count:number;schema_status:string;stop_reason:string|null;
  error_code:string|null;safe_error_message:string|null;source_type:string;sync_mode:string;
  periodic_polling:boolean;http_read_count:number;maximum_http_reads:number;
  pages_read:number;maximum_pages:number;current_page:number;
  consecutive_empty_reads:number;consecutive_failures:number;
  last_successful_read_at:string|null;watermark_utc:string|null;
  source_cursor:string|null;lease_generation:number|null;
  latest_metric_snapshot_id:string|null;latest_confidence_snapshot_id:string|null;
  provenance_manifest_sha256:string|null;date_from_utc:string;date_to_utc:string;
  timezone:string;out_of_range_count:number;missing_timestamp_count:number;
  schema_adapter:null|{
   schema_fingerprint:string;schema_adapter_id:string|null;adapter_version:string|null;
   selection_status:string;rejection_reason:string|null;observed_at:string}};
 last_lease_event:null|{event_type:string;created_at:string;details:Record<string,unknown>};
}
export interface PersistentStorageDiagnostic{
 pairing_id:string;status:string;diagnostic:null|{
  cookie_metadata:Array<{name:string;domain:string;secure:boolean;sameSite:string;expires:number}>;
  local_storage_keys:string[];session_storage_keys:string[];indexed_db_names:string[];
  authentication_origins:string[];changed_storage_types:string[];
  indexed_db_capture_supported:boolean;
 };
}

export interface UatExecutionControl{status:string;execution_ready:boolean;task:null|{task_id:string;run_id:string;state:string;enabled:boolean;allowed_models:string[];allowed_channels:string[];max_requests:number;used_requests:number;remaining_requests:number;max_total_cost:string;reserved_cost:string;used_cost:string;remaining_cost:string;cost_currency:string;max_duration_seconds:number;max_concurrency:number;max_attempts_per_request:number;active_requests:number;expires_at:string;approval_reference:string|null;approval_expires_at:string|null;kill_switch_active:boolean;stopped_reason:string|null;created_at:string;approved_at:string|null;activated_at:string|null;audit_log:{event_id:string;event_type:string;created_at:string;details:Record<string,unknown>}[]}}
export interface EnvironmentExecutionState{environment_id:EnvironmentId;enabled:boolean;state_source:'operator_runtime_toggle'|'environment_configuration';updated_at:string|null;updated_by:string|null;setting_version:string}
export type CredentialSource='windows_encrypted_vault'|'temporary_session'|'session'|'environment'|'none';
export interface UatStatus{environment:string;base_url:string|null;key_configured:boolean;credential_source:CredentialSource;expires_at?:string|null;minutes_remaining?:number|null;key_fingerprint?:string;real_execution_enabled:boolean;daily_request_limit:number|null;daily_budget_cny:number|null;request_limit_enabled:boolean;budget_limit_enabled:boolean;remaining_budget_cny:number|null;requests_used_today:number;estimated_cost_used_today:number;execution_ready:boolean;blocking_reasons:string[];stream_execution_ready:boolean;execution_control?:UatExecutionControl}
export interface CredentialStatus{configured:boolean;credential_source:CredentialSource;created_at?:string;updated_at?:string;expires_at:string|null;minutes_remaining:number|null;key_fingerprint:string|null;encryption?:string|null;key_protection?:string|null}
export interface TemporaryCredentialStatus extends CredentialStatus{
  environment_id:EnvironmentId;auth_session_id:string|null;
  connection_status:'not_tested'|'success'|'authentication_failed'|'timeout'|'endpoint_unavailable';
}
export interface UatWorkbenchPair{name:string;value:string;type?:string;enabled?:boolean;description?:string}
export interface UatWorkbenchBody{
  method:'GET'|'POST'|'PUT'|'PATCH'|'DELETE'|'HEAD'|'OPTIONS';
  environment_id:EnvironmentId;path:string;query_params:UatWorkbenchPair[];
  headers:UatWorkbenchPair[];auth_session_id:string|null;
  auth:{method:'bearer'|'api_key_header'|'none';header_name:string;prefix:string};
  body:{type:'none'|'json'|'raw'|'form_data'|'urlencoded';value:unknown};
  content_type:string|null;timeout_seconds:number;stream:boolean;model:string|null;
  model_selection_mode:'specified'|'automatic';
  channel_id:string|null;routing_policy:string|null;execution_limits:Record<string,unknown>;
  request_type:'text'|'image'|'audio'|'video';multimodal_validation_id:string|null;
}
export interface SkillTrace{skill_id:string;skill_version:string;invocation_id:string;audit_id:string;permission_decision:string}
export interface UatWorkbenchValidation{valid:boolean;structurally_valid:boolean;errors:string[];blocking_reasons:string[];warnings:string[];safe_preview:Record<string,unknown>|null;skill_trace?:SkillTrace}
export interface UatWorkbenchResult{
  execution_status:'success'|'failed';request_id:string;decision_id:string|null;
  local_request_id?:string|null;provider_request_id?:string|null;
  provider_response_id?:string|null;provider_trace_id?:string|null;
  client_correlation_id?:string|null;
  http_status:number;method:string;path:string;response_headers:Record<string,string>;
  response_body:string|null;response_body_truncated:boolean;total_latency_ms:number;
  first_token_latency_ms?:number|null;
  network_called:true;attempts:number;retry_used:boolean;fallback_used:boolean;
  response_id?:string|null;requested_model?:string|null;actual_model?:string|null;
  channel_id?:string|null;provider?:string|null;input_tokens?:number|null;
  cached_input_tokens?:number|null;output_tokens?:number|null;
  total_tokens?:number|null;cost_amount?:string|null;currency?:string|null;
  capability_evidence?:Record<string,unknown>|null;
  routing_decision?:{policy:string;selected_model:string;selection_reason:string;
    confidence:string;catalog_candidate_count:number;
    candidates:Array<{model_id:string;score:number;successful_live_samples:number;
      failed_live_samples:number;average_latency_ms:number|null;reason:string}>;
    excluded:Array<{model_id:string;reason:string}>}|null;
  error:Record<string,unknown>|null;created_at:string;skill_trace?:SkillTrace;
}
export interface RoutingDecisionCandidate{
  model_id:string|null;channel_id?:string|null;score:number|null;
  capability_status?:string|null;health_status?:string|null;reason?:string|null;
  successful_live_samples?:number;failed_live_samples?:number;average_latency_ms?:number|null;
}
export interface RoutingDecisionDetail{
  decision_id:string;request_id:string;response_id?:string|null;
  decision_type:'specified_model'|'automatic_routing';environment_id:string;
  method?:string|null;path?:string|null;stream?:boolean;created_at?:string|null;
  policy_version?:string|null;configuration_version?:string|null;metric_snapshot_id?:string|null;
  candidates:RoutingDecisionCandidate[];exclusions:Array<{model_id?:string|null;channel_id?:string|null;reason?:string|null}>;
  selected_model?:string|null;selected_channel?:string|null;selection_reason?:string|null;
  confidence?:string|null;catalog_candidate_count?:number;
  execution_result:{status:string;http_status:number|null;request_id:string;response_id?:string|null;
    model?:string|null;provider?:string|null;channel_id?:string|null;channel_name?:string|null;
    total_latency_ms?:number|null;first_token_latency_ms?:number|null;input_tokens?:number|null;
    cached_input_tokens?:number|null;output_tokens?:number|null;cost_amount?:string|null;
    currency?:string|null;total_attempts:number;fallback:boolean;error_code?:string|null;error_category?:string|null};
  retry_history:Array<Record<string,unknown>>;
}
export type UatWorkbenchStreamEvent=
  |{type:'stage';stage:string;message:string}
  |{type:'delta';content:string}
  |{type:'result';result:UatWorkbenchResult}
  |{type:'error';code:string;status:number;message:string};
export interface ConnectionTest{connection_status:string;http_status:number|null;tested_at:string;target_host:string;credential_source:string;model_count:number;message:string}
export interface UatProfile{id:string;name:string;prompt:string;stream:boolean;default_max_tokens:number;purpose:string;expected_input_scale:string;expected_output_scale:string;status:'available'|'blocked_capability';blocking_reason:string|null}
export interface UatModelOutputCapability{model_id:string;provider:string|null;channel_id:string;max_context_tokens:number|null;max_input_tokens:number|null;max_output_tokens:number|null;confirmed_max_output_tokens:number|null;confirmed_channel_max_output_tokens?:number|null;streaming_supported:boolean|null;multimodal_capabilities:string[];evidence_source:string;evidence_type:string|null;evidence_version:string|null;observed_at:string|null;expires_at:string|null;fresh_until:string|null;confidence_status:string;reviewed_by:string|null;review_timestamp:string|null;status:string}
export interface UatEditorOptions{profiles:UatProfile[];models:{access_mode:string;fallback_allowed:string[];explanation:string|null};stream_execution_ready:boolean;streaming_explanation:string;policy_version:string;maximum_max_tokens:number|null;token_presets:number[];selected_channel:string;six_model_ids:string[];model_output_capabilities:UatModelOutputCapability[]}
export interface UatModel{id:string;display_name:string;owned_by:string|null;execution_allowed:boolean;stream_capability:'pending_confirmation';source_environment?:EnvironmentId}
export interface UatModelCatalog{environment_id?:EnvironmentId;environment_name?:string;status?:'ready'|'error';catalog_status?:'ready'|'error'|'blocked';source:string;fetched_at:string|null;model_count:number;models:UatModel[];cache?:'hit'|'miss';error?:{code:string;message:string};http_status?:number|null}

export type ProbeRunStatus='CREATED'|'RUNNING'|'PAUSED'|'COMPLETED'|'STOPPED'|'FAILED'|'AUTO_STOPPED'|'INTERRUPTED';
export interface ProbeRunSummary{
  request_count?:number;success_count?:number;failure_count?:number;duration_seconds?:number;success_rate?:number|null;natural_success_rate?:number|null;
  comprehensive_success_rate?:number|null;p50_ms?:number|null;p95_ms?:number|null;p99_ms?:number|null;
  input_tokens?:number;cached_tokens?:number;output_tokens?:number;actual_provider_cost?:string|null;
  actual_cost_synced_count?:number;estimated_versioned_price?:string|null;pending_provider_sync?:number;
  provider_live_failures?:number;uat_injected_failures?:number;throttle_count?:number;pause_count?:number;
  circuit_open_count?:number;recovery_count?:number;max_observed_concurrency?:number;
}
export interface ProbeRun{
  probe_run_id:string;name?:string;task_name?:string;status:ProbeRunStatus|string;environment_id:string;
  started_at?:string|null;finished_at?:string|null;created_at?:string|null;updated_at?:string|null;
  planned_duration_seconds?:number;duration_seconds?:number;progress?:number|null;model_ids?:string[];
  configuration?:Record<string,unknown>;configuration_json?:Record<string,unknown>|string|null;
  sent_count?:number;completed_count?:number;success_count?:number;failure_count?:number;
  summary?:ProbeRunSummary;summary_json?:ProbeRunSummary|Record<string,unknown>|string|null;
  audit_id?:string|null;evidence_path?:string|null;current_interval_seconds?:number|null;
  current_concurrency?:number|null;current_phase?:string|null;stop_requested?:boolean;
}
export interface ProbeListResponse{status?:string;items:ProbeRun[];total:number;limit?:number;offset?:number}
export interface ProbeCreateInput{
  name:string;environment_id:'china_uat';base_url:string;endpoint:string;method:string;models:string[];
  stream:boolean;prompt:string;duration_seconds:number;interval_seconds:number;max_concurrency:number;
  timeout_seconds:number;max_tokens:number;max_requests?:number|null;rotation_mode?:string;
  stop_thresholds:{consecutive_failures?:number;minimum_success_rate?:number;max_429_rate?:number;
    max_5xx_rate?:number;max_p95_ms?:number;max_actual_cost?:string|null;pause_on_circuit_open?:boolean;
    resume_after_recovery?:boolean};
}
export interface ProbeEvent{event_id:string;probe_run_id?:string;event_type:string;source_type?:string;
  created_at:string;model_id?:string|null;reason?:string|null;trigger_metric?:string|null;
  result?:string|null;request_id?:string|null;details?:Record<string,unknown>;details_json?:Record<string,unknown>|string|null}
export interface ProbeRequestRecord{local_request_id?:string;request_id?:string;provider_request_id?:string|null;
  decision_id?:string|null;occurred_at:string;model_id?:string;requested_model?:string;actual_model?:string|null;success?:boolean;
  status?:string;http_status?:number|null;latency_ms?:number|null;first_token_ms?:number|null;first_token_latency_ms?:number|null;
  input_tokens?:number|null;cached_tokens?:number|null;cached_input_tokens?:number|null;output_tokens?:number|null;actual_cost?:string|null;actual_provider_cost?:string|null;
  estimated_cost?:string|null;estimated_versioned_price?:string|null;cost_status?:string|null;error_category?:string|null;
  fault_source?:string|null;error_source?:string|null}
export interface ProbeMetrics{
  probe_run_id:string;summary?:ProbeRunSummary;request_count?:number;success_count?:number;failure_count?:number;
  natural_success_rate?:number|null;comprehensive_success_rate?:number|null;p50_ms?:number|null;p95_ms?:number|null;p99_ms?:number|null;
  input_tokens?:number;cached_input_tokens?:number;output_tokens?:number;actual_provider_cost?:string|null;
  actual_cost_synced_count?:number;estimated_versioned_price?:string|null;pending_provider_sync_count?:number;
  max_observed_concurrency?:number;request_series:Array<Record<string,unknown>>;
  latency_series?:Array<Record<string,unknown>>;error_distribution?:Array<Record<string,unknown>>;
  error_composition?:Array<Record<string,unknown>>;model_metrics:Array<Record<string,unknown>>;
  token_cost_series?:Array<Record<string,unknown>>;
  circuit_timeline?:Array<Record<string,unknown>>;freshness?:Record<string,unknown>;
}
export interface UatRequestBody{environment_id:EnvironmentId;mode:'real_uat_execute';confirmation:{confirmed:boolean;confirmation_text:string};request:{requested_model:string;channel_id:string;messages:Array<{role:string;content:string}>;stream:boolean;max_tokens:number};measurement:{plan_id:string;request_profile_id:string;session_id:string};shadow:{run_before_execution:boolean;strategy:string}}
export interface UatExecutionResult{validation:{valid:boolean;guard_status:string;blocking_reasons:string[];cost_estimate:{estimated_cost_cny:number};token_boundary?:{user_requested_max_tokens:number;model_max_output_tokens:number|null;channel_max_output_tokens:number|null;remaining_context_capacity:number|null;budget_affordable_output_tokens:number|null;effective_max_tokens:number|null;limiting_factor:string|null;evidence_source:string;evidence_version:string|null;fresh_until:string|null}};shadow_decision?:Record<string,unknown>;network_execution:{execution_attempted:boolean;network_called:boolean};observed_api_result?:Record<string,unknown>;backend_correlation?:{status:string};execution?:{execution_id:string;decision_id:string;status:string;user_requested_max_tokens?:number;effective_max_tokens?:number;limiting_factor?:string|null;estimated_cost?:number;actual_output_tokens?:number|null;actual_cost?:number|null}}
export interface ReplayResult{replay_id:string;strategy:string;execution_mode:string;selected_candidate:string;changed_decision_rate:number;cost_estimate_cny:number;latency_estimate_ms:number;sla_risk_estimate:number;concentration_risk:number;unroutable_count:number;low_confidence_count:number;stale_data_count:number;network_calls:number}
export interface HistoricalReplaySource{
  schema_version:string;environment_id:string;source_type:string|null;is_mock:false;
  sample_count:number;exact_association_count:number;unlinked_count:number;
  occurred_from:string|null;occurred_to:string|null;last_synced_at:string|null;
  source_watermark:string|null;sources:Array<{source_type:string;count:number}>;
  configuration_versions:string[];price_versions:string[];empty_message:string|null;
  actual:{record_count:number;input_tokens:number;cached_input_tokens:number;output_tokens:number;
    total_cost:string|null;average_cost:string|null;cost_coverage_count:number;
    p50_latency_ms:number|null;p95_latency_ms:number|null;p99_latency_ms:number|null;
    failure_count:number;stream_count:number;nonstream_count:number;model_count:number;
    currency:string|null;price_source:string|null;price_version:string|null};
  evidence_capabilities:{historical_aggregation:number;exact_decision_reconstruction:number;
    historical_statistics_only:number;authoritative_channel:number;unusable:number};
}
export interface HistoricalReplayItem{
  record_id:string;occurred_at:string;request_id:string|null;response_id:string|null;
  decision_id:string|null;source_type?:string;association?:'exact'|'historical_statistics'|'execution_only'|'unlinked';
  configuration_version?:string|null;metric_snapshot_id?:string|null;
  actual:{model:string|null;requested_model?:string|null;billed_model?:string|null;actual_model?:string|null;
    channel_id:string|null;channel_name:string|null;status:string|null;
    http_status:number|null;latency_ms:number|null;first_token_latency_ms?:number|null;
    stream?:boolean;duration_type?:string|null;cost_amount:string|null;currency:string|null;
    pricing_detail?:string|null;price_source?:string|null;price_version?:string|null;evidence_level?:string|null;
    input_tokens:number|null;cached_input_tokens:number|null;output_tokens:number|null;
    total_attempts:number|null;fallback:boolean;label:string};
  counterfactual:{strategy:string;model:string|null;channel_id:string|null;choice_changed:boolean|null;
    estimated_cost:string|null;cost_delta:string|null;currency:string|null;price_version:string|null;
    cost_basis:string|null;cost_unavailable_reason:string|null;estimated_latency_ms:number|null;
    latency_sample_count:number;latency_basis:string|null;confidence:'high'|'medium'|'low';
    reason:string|null;label:string};
  timeline:Array<{step:number;title:string;status:string;evidence:Record<string,unknown>}>;
}
export interface HistoricalReplayResult{
  replay_id:string;schema_version:string;execution_mode:string;is_mock:false;
  baseline_strategy:string;candidate_strategy:string;source:HistoricalReplaySource;
  sample_count:number;exact_association_count:number;unlinked_count:number;
  historical_statistics_count:number;execution_only_count:number;
  choice_comparable_count:number;choice_changed_count:number;choice_changed_rate:number|null;
  actual:{record_count:number;input_tokens:number;cached_input_tokens:number;output_tokens:number;
    total_tokens:number;total_cost:string|null;average_cost:string|null;cost_coverage_count:number;
    p50_latency_ms:number|null;p95_latency_ms:number|null;p99_latency_ms:number|null;
    failure_count:number;stream_count:number;nonstream_count:number;model_count:number;
    currency:string|null;price_source:string|null;price_version:string|null};
  cost:{baseline:string|null;candidate:string|null;delta_percent:number|null;coverage_count:number;
    actual_coverage_count:number;candidate_unavailable_reason:string|null;actual_formula:string;
    formula:string;currency:string|null};
  latency:{baseline_p95_ms:number|null;candidate_p95_ms:number|null;coverage_count:number;formula:string};
  success_changes:{success_to_failure:number|null;failure_to_success:number|null;reason:string};
  sla:{baseline_failure_count:number;baseline_timeout_count:number;baseline_rate_limit_count:number;baseline_p95_ms:number|null;baseline_p99_ms:number|null;candidate_risk:number|null;coverage_count:number;reason:string};
  fallback:{baseline:number;candidate:number|null;reason:string};
  concentration:{baseline_hhi:number|null;candidate_hhi:number|null;formula:string;reason:string|null};
  low_confidence_count:number;unestimable_count:number;risk_count:number;conclusion:string;disclaimer:string;
  created_at:string;network_calls:number;items:HistoricalReplayItem[];
}
export interface HistoricalReplayInput{
  environment_id:string;occurred_from?:string|null;occurred_to?:string|null;
  model?:string|null;channel?:string|null;request_type?:string|null;
  baseline_strategy:string;candidate_strategy:string;exact_only:boolean;limit:number;
}
export interface BugReportInput{request_id?:string;decision_id?:string;model?:string;channel?:string;request_type?:string;http_status?:number|null;message?:string;context?:string}
export interface BugReport{bug_id:string;title:string;environment:string;detected_time:string;request_id:string;decision_id:string;model:string;channel:string;request_type:string;http_status:number|null;sanitized_original_error:string;structured_classification:Record<string,unknown>;reproduction_steps:string[];expected_behavior:string;actual_behavior:string;retryable:boolean;fallback_allowed:boolean;fallback_trace:string[];fallback_evidence_status:string;evidence_paths:string[];impact:string;frequency:string;suggested_owner:string;suggested_next_action:string;limitations:string;markdown:string}
export interface ConfigReviewResult{
  status:'ready'|'insufficient_data';empty_message:string|null;environment_id:string;is_mock:false;
  data_source:{sources:Array<{source:string;count:number}>;time_from:string|null;time_to:string|null;sample_count:number;raw_count:number;deduplicated_count:number;exact_duplicate_count:number;duplicate_candidate_count:number;exact_link_count:number;channel_coverage_count:number;channel_coverage_rate:number|null;last_synced_at:string|null;configuration_version:string|null;price_version:string|null;price_synced_at:string|null;metric_snapshot_id:string|null;metric_generated_at:string|null};
  configuration:{policy_version:string|null;strategies:Array<{strategy_id:string;strategy_version:string|null;weights:Record<string,number>;purpose:string|null}>;fallback_configured:boolean;timeout_ms:number|null;maximum_attempts:number|null;retry_policy_version:string|null;retryable_errors:string[]};
  metrics:{sample_count:number;success_rate:number|null;p95_latency_ms:number|null;p99_latency_ms:number|null;input_tokens:number;output_tokens:number;total_cost:string|null;average_cost:string|null;cost_coverage:number|null;timeout_count:number;retry_count:number;fallback_count:number;concentration:{hhi:number|null;maximum_share:number|null;channel_count:number;sample_count:number;basis:string};model_metrics:Array<{model:string;sample_count:number;success_rate:number;p95_latency_ms:number|null}>};
  versions:Array<{configuration_version:string;revision:number;policy_version:string|null;retry_policy_version:string|null;source_type:string;source_sha256:string;created_at:string;created_by:string;change_reason:string;previous_version:string|null;checksum:string;is_active:boolean;payload:Record<string,unknown>;diff:Record<string,{before:unknown;after:unknown}>}>;
  selected_versions:{baseline:string;comparison:string};configuration_diff:Record<string,{before:unknown;after:unknown}>;
  risks:Array<{risk:string;status:string;observed_count:number|null;basis:string}>;
  impact:{status:'available'|'unavailable';current_version:string|null;previous_version:string|null;current:Record<string,unknown>|null;previous:Record<string,unknown>|null;cost_difference:string|null;latency_difference_ms:number|null;concentration_difference:number|null;basis:string};
  rollback:{status:string;target_version:string|null;message:string};
  evidence:Array<{occurred_at:string;request_id:string|null;decision_id:string|null;model:string|null;channel_id:string|null;channel_source:string;status:string;latency_ms:number|null;cost_amount:string|null;currency:string|null;configuration_version:string|null}>;
  technical:Record<string,unknown>;
}
export interface ExportCatalogItem{name:string;format:string}
export interface ExportGenerateResult{name:string;format:string;generated_at:string;sha256:string;content:string}
export interface SearchItem{
  entity_type:string;primary_id:string;title:string;summary:string;
  environment_id:string;source_type:string;is_mock:boolean;
  sample_or_evidence_id:string|null;updated_at:string|null;
  destination_path:string;matched_fields:string[];
}
export interface SearchResponse{
  query:string;environment_id:EnvironmentFilter;source_type:string;
  items:SearchItem[];groups:Record<string,number>;next_cursor:string|null;
  provenance:{source_type?:string;environment_id?:string;sample_size?:number;[key:string]:unknown};
}
export type MetricWindow='5m'|'1h'|'24h';
export interface DynamicMetricModel{
  model:string;request_count:number;success_rate:number;p95_ms:number|null;
  total_tokens:number;actual_cost:number|null;pending_cost_count:number;
  last_sample_at:string;age_seconds:number;
  confidence:{lower:number;upper:number;sample_size:number};
  status:'healthy'|'insufficient_sample'|'stale'|'blocked'|'unknown';eligible:boolean;
}
export interface DynamicMetricsOverview{
  update_status:{status:string;last_aggregated_at:string|null;source_watermark:string|null;next_refresh_at:string};
  scope:{environment_id:string;window:MetricWindow;traffic_class:string;source_type:string|null;timezone:string;display_timezone:string};
  kpis:{request_count:number;success_rate:number|null;p50_ms:number|null;p95_ms:number|null;p99_ms:number|null;
    total_tokens:number;actual_cost:number|null;estimated_cost:number|null;pending_cost_count:number;eligible_model_count:number};
  request_series:Array<{at:string;requests:number;success_rate:number}>;
  latency_series:Array<{at:string;p50:number|null;p95:number|null;p99:number|null;ttft_p95:number|null}>;
  token_series:Array<{at:string;input:number;cached:number;output:number}>;
  cost_series:Array<{at:string;actual:number|null;estimated:number|null;pending:number}>;
  model_metrics:DynamicMetricModel[];
  freshness:Array<{model:string;last_sample_at:string;age_seconds:number;status:string}>;
  confidence:Array<{model:string;sample_size:number;success_rate:number;lower:number;upper:number;eligible:boolean}>;
  snapshot_status:Record<string,number>;coverage:Record<string,number>;
  technical_metadata:{aggregation_version:string;source_contract:string;source_watermark:string|null;
    legacy_snapshot_count:number;snapshot_ids:string[];configuration_version:string|null};
}
export type CapabilityEvidenceStatus='confirmed'|'observed'|'pending'|'unsupported';
export type CapabilityKey='text'|'image_understanding'|'image_generation'|'audio_input'|'audio_output'|'video_input'|'video_generation'|'streaming'|'tools';
export interface ModelCapabilityCell{
  status:CapabilityEvidenceStatus;evidence_count:number;sample_count:number;success_count?:number;failure_count?:number;
  sources?:string[];last_success_at?:string|null;last_failure_at?:string|null;confidence?:number|null;expires_at?:string|null;reasons?:string[];
}
export interface ModelCapabilityItem{
  model_id:string;display_name:string;provider:string|null;endpoints:string[];tags:string[];context_limit:number|null;
  pricing_type:string|null;catalog_updated_at:string|null;capabilities:Record<CapabilityKey,ModelCapabilityCell>;
  call_summary:{call_count:number;success_count:number;success_rate:number|null;stream_count:number;non_stream_count:number;
    p50_ms:number|null;p95_ms:number|null;input_tokens:number;output_tokens:number;actual_cost:number|null;last_request_id:string|null;last_called_at:string|null};
  evidence_summary:{status:CapabilityEvidenceStatus;evidence_count:number;sources:string[];last_verified_at:string|null};
  limitations:string[];technical_metadata:Record<string,unknown>;
}
export interface CapabilityCoverageItem{capability:string;confirmed:number;observed:number;pending:number;unsupported:number}
export interface EvidenceSourceItem{source:string;count:number}
export interface ModelCapabilitiesOverview{
  status:string;update_status:{status:string;last_error?:string|null;next_retry_at?:string|null}|string;environment_id:string;
  catalog_status:string;catalog_model_count:number;call_evidence_model_count:number;confirmed_model_count:number;observed_model_count:number;
  pending_model_count:number;unsupported_model_count:number;mapping_count:number;recent_24h_calls:number;historical_log_count:number;
  realtime_log_count:number;evidence_count:number;last_sync_at:string|null;last_aggregated_at:string|null;models:ModelCapabilityItem[];
  capability_coverage:CapabilityCoverageItem[];evidence_sources:EvidenceSourceItem[];technical_metadata:Record<string,unknown>;
}
export interface ModelMappingItem{
  requested_model:string|null;billed_model:string|null;actual_model:string|null;call_count:number;consistency_rate:number|null;
  mismatch_count:number;missing_actual_count:number;last_observed:string|null;evidence_level:'exact'|'realtime_linked'|'historical_observation'|'pending'|string;source:string;
}
export interface ModelMappingsResponse{
  status:string;items:ModelMappingItem[];consistency_distribution:Array<{status:string;count:number}>|Record<string,number>;
  technical_metadata:Record<string,unknown>;
}
export interface MetricSnapshot{
  snapshot_id:string;aggregation_version:string;window_name:MetricWindow;
  window_seconds:number;environment_id:string;model:string;channel:string;
  stream:boolean|null;request_profile_id:string;currency:string|null;
  window_start:string;window_end:string;generated_at:string;
  last_evidence_at:string|null;evidence_age_seconds:number|null;
  freshness_status:'fresh'|'stale';data_state:
    'healthy_evidence'|'insufficient_data'|'stale'|'unknown'|'blocked';
  sample_count:number;source_watermark:string|null;
  metrics:{
    success_rate:number|null;latency_p50_ms:number|null;
    latency_p95_ms:number|null;latency_p99_ms:number|null;
    ttft_p50_ms:number|null;ttft_p95_ms:number|null;ttft_p99_ms:number|null;
    tpot_p50_ms:number|null;tpot_p95_ms:number|null;tpot_p99_ms:number|null;
    http_429_rate:number|null;http_5xx_rate:number|null;timeout_rate:number|null;
    incomplete_sse_rate:number|null;average_estimated_cost:number|null;
    estimated_cost_p95:number|null;average_actual_cost:number|null;
    actual_cost_p95:number|null;model_mapping_anomaly_rate:number|null;
    fallback_rate:number|null;schedulability_rate:number|null;
    missing_field_count:number;average_source_reliability:number;
    source_type_counts:Record<string,number>;contains_mock:boolean;
  };
}
export interface MetricSnapshotsResponse{
  status:'ready'|'unknown';aggregation_version:string;
  source_watermark:string|null;last_refresh_at:string|null;event_count:number;
  total:number;limit:number;offset:number;has_more:boolean;items:MetricSnapshot[];
  source_contract:string;network_called:boolean;
  refresh_runtime:{last_started_at:string|null;last_completed_at:string|null;
    last_status:string;last_error_code:string|null;consecutive_failures:number};
}
export interface StatisticalConfidenceSnapshot{
  confidence_snapshot_id:string;metric_snapshot_id:string;
  confidence_version:string;policy_version:string;policy_sha256:string;method:string;
  calculated_at:string;window_name:MetricWindow;confidence_level:number;
  total_event_count:number;valid_outcome_count:number;
  success_count:number;failure_count:number;raw_success_rate:number|null;
  weighted_success_rate:number|null;effective_sample_size:number;
  minimum_effective_sample_size:number;interval_lower:number|null;
  interval_upper:number|null;interval_width:number|null;
  adjusted_success_score:number|null;adjusted_failure_risk:number|null;
  small_sample_adjustment:number|null;outcome_coverage_factor:number;
  source_reliability_factor:number;freshness_factor:number;
  completeness_factor:number;confidence_label:'high'|'medium'|'low'|'insufficient';
  confidence_state:'ready'|'insufficient'|'blocked';
  boundary_case:string;unknown_source_types:string[];assumptions:string[];
  evidence_id_count:number;evidence_ids:string[];evidence_ids_truncated:boolean;
  evidence_manifest_sha256:string;
}
export interface StatisticalConfidenceResponse{
  status:'ready'|'unknown';confidence_version:string;policy_version:string;
  total:number;limit:number;offset:number;has_more:boolean;
  items:StatisticalConfidenceSnapshot[];network_called:false;
  source_contract:string;
}
export interface StickyBinding{
  sticky_binding_id:string;safe_route_key_fingerprint:string;
  environment_id:string;requested_model:string;selected_channel:string;
  state:'ACTIVE'|'EXPIRED'|'INVALIDATED'|'INTERRUPTED';
  policy_version:string;configuration_version:string;created_at:string;
  expires_at:string;maximum_expires_at:string;last_used_at:string;
  remaining_seconds:number;hit_count:number;invalidation_reason:string|null;
  interruption_reason:string|null;originating_decision_id:string;
  latest_decision_id:string;metric_snapshot_id:string|null;
  confidence_snapshot_id:string|null;evidence_ids:string[];is_mock:boolean;
  evidence_source:string;capability_scope:Record<string,unknown>;
}
export interface StickyBindingsResponse{
  status:string;total:number;limit:number;offset:number;has_more:boolean;
  items:StickyBinding[];
}
export interface StickyStatus{
  status:'ready'|'blocked';blocked_reason:string|null;enabled:boolean;
  operational:boolean;execution_ready?:boolean;
  policy_version:string;configuration_version:string;
  ttl_seconds:number;maximum_total_duration_seconds:number;
  route_key_derivation_version:string;capability_scope_version:string;
  required_gate_order:string[];state_counts:Record<string,number>;
  schema_version:number;network_called:false;
}
export interface StickyMetrics{
  status:string;environment_id:string|null;outcomes:Record<string,number>;
  hit_rate:number|null;interruption_reasons:Array<{reason:string;count:number}>;
  network_called:false;
}
export interface CircuitBreakerState{
  circuit_id:string;state:'CLOSED'|'OPEN'|'HALF_OPEN';opened_at:string|null;
  cooldown_until:string|null;half_open_successes:number;revision:number;
  policy_version:string;updated_at:string;
}
export interface CircuitBreakerStatus{
  status:string;policy_version:string;enabled:boolean;
  state_counts:Record<string,number>;active_probe_leases:number;
  items:CircuitBreakerState[];network_called:false;
}
export interface ExplorationStatus{
  status:string;mode:'shadow_only';feature_enabled:boolean;
  real_execution_allowed:false;policy_version:string;
  approvals:Array<{approval_id:string;environment_id:string;channel_id:string;
    model_id:string;created_at:string;expires_at:string;revoked_at:string|null;active:boolean}>;
  kill_switches:Array<{scope_type:string;scope_id:string;active:boolean;updated_at:string}>;
  usage:{requests:number;cost:number};budgets:Record<string,{requests:number;cost:number}>;
  stop_policy:Record<string,unknown>;allowed_circuit_states:string[];
  decisions:Array<{decision_id:string;request_id:string;run_id:string;environment_id:string;
    channel_id:string;model_id:string;selected_candidate_id:string;selector:string;created_at:string}>;
  authorization_status:string;network_called:false;
}
export interface CapabilityEvidenceResponse{
  status:string;policy_version:string;registry_version:string;network_called:false;
  fixed_scenarios:Record<string,string[]>;media_bounds:Record<string,unknown>;
  items:Array<{evidence_id:string;evidence_type:string;observed_at:string;
    environment_id:string;subject_id:string;subject_version:string;source:string;
    requirement:string;state:string;verification_status:string;registry_version:string;
    scenario_id:string|null;mime_types:string[];maximum_input_bytes:number|null;
    maximum_output_bytes:number|null;maximum_context_tokens:number|null;
    valid_until:string|null;revoked_at:string|null;supersedes_evidence_id:string|null}>;
}
export interface GovernanceKillSwitch{scope_type:string;scope_id:string;active:boolean;updated_at:string}
export interface TrafficChangeStatus{
  status:'disabled'|'sandbox_only';capability_id:'ADV-017';enabled:boolean;mode:'offline_sandbox';
  real_execution_allowed:false;policy_version:string;state_counts:Record<string,number>;
  maximum_rollout_percent:number;required_gates:string[];authorization_status:string;network_called:false;
  kill_switches:GovernanceKillSwitch[];items:Array<{proposal_id:string;environment_id:string;
    channel_id:string;model_id:string;state:string;rollout_percent:number;created_at:string;
    expires_at:string;activated_at:string|null;terminated_at:string|null;
    revision:number;policy_version:string}>;
}
export interface TrafficControlMetrics{
  request_count:number;success_rate:number|null;error_rate:number|null;
  p50_latency_ms:number|null;p95_latency_ms:number|null;p99_latency_ms:number|null;
  token_count:number|null;total_cost:number|null;average_cost:number|null;
  fallback_rate:number|null;coverage:{latency:number;cost:number};latest_at:string|null;
}
export interface TrafficControlCurrent{
  status:'ready';environment_id:'china_uat';current_mode:'sandbox_validation';
  production_execution:'not_configured';policy_version:string|null;
  configuration_version:string|null;current_model_id:string|null;
  current_channel_id:string|null;channel_evidence_available:boolean;
  data_updated_at:string|null;metrics_24h:TrafficControlMetrics;
  traffic_distribution:Array<{model_id:string;channel_id:string;percentage:number;request_count:number}>;
  network_called:false;
}
export interface TrafficControlChannel{
  channel_id:string;channel_name:string|null;channel_source:string;models:string[];
  last_seen_at:string;sample_count:number;
}
export interface TrafficControlImpact{
  status:'ready'|'insufficient_evidence';baseline:TrafficControlMetrics;
  target:TrafficControlMetrics;difference:Record<string,number|null>;coverage_rate:number;
  channel_concentration:number|null;limitations:string[];calculation_basis:string;
}
export type TrafficControlState='DRAFT'|'VALIDATED'|'PENDING_APPROVAL'|'APPROVED'|
  'CANARY_RUNNING'|'PAUSED'|'COMPLETED'|'AUTO_STOPPED'|'ROLLED_BACK'|'REJECTED';
export interface TrafficControlProposal{
  proposal_id:string;environment_id:string;environment_mode:'sandbox'|'china_uat'|'production';
  source_model_id:string|null;source_channel_id:string|null;target_model_id:string;
  target_channel_id:string;source_policy_version:string|null;target_policy_version:string;
  rollout_percent:number;state:TrafficControlState;reason:string;approval_reference:string|null;
  approval_id:string|null;observation_seconds:number;minimum_sample_count:number;
  stop_conditions:Record<string,number|boolean>;rollback_condition:string;impact:TrafficControlImpact;
  request_ids:string[];decision_ids:string[];last_trigger:Array<Record<string,unknown>>|null;
  proposer_id:string;operator_id:string|null;created_at:string;updated_at:string;
  started_at:string|null;paused_at:string|null;completed_at:string|null;
  rolled_back_at:string|null;revision:number;policy_version:string;
}
export interface TrafficControlProposalInput{
  environment_mode:'sandbox'|'china_uat'|'production';source_model_id?:string|null;
  source_channel_id?:string|null;target_model_id:string;target_channel_id:string;
  source_policy_version?:string|null;target_policy_version:string;rollout_percent:number;
  reason:string;approval_reference?:string|null;observation_seconds:number;
  minimum_sample_count:number;stop_conditions:Record<string,number|boolean>;
  rollback_condition:string;
}
export interface TrafficControlLiveMetrics{
  status:'ready';proposal_id:string;proposal_state:TrafficControlState;
  rollout_percent:number;metrics:TrafficControlMetrics;stop_conditions:Record<string,number|boolean>;
  triggers:Array<{condition:string;actual:number;threshold:number}>;snapshot_id:string;
  collected_at:string;network_called:false;
}
export interface ProbeGovernanceStatus{
  status:'disabled'|'mock_only';capability_id:'ADV-018';enabled:boolean;mode:'mock_only';
  live_execution_allowed:false;policy_version:string;state_counts:Record<string,number>;
  active_approval_count:number;active_lease_count:number;limits:Record<string,Record<string,number>>;
  backoff_policy:Record<string,number>;stop_policy:Record<string,number>;authorization_status:string;
  kill_switches:GovernanceKillSwitch[];stopped_scopes:Array<{scope_type:string;scope_id:string;
    consecutive_failures:number;sample_count:number;failure_count:number;stopped:boolean;
    backoff_until:string|null}>;network_called:false;
  items:Array<{lease_id:string;run_id:string;environment_id:string;channel_id:string;model_id:string;
    state:string;cost:number;created_at:string;expires_at:string;completed_at:string|null}>;
}
export interface ProbeTask{
  task_id:string;name:string;environment_id:string;channel_id:string;model_id:string;
  request_template:'mock_success'|'mock_failure';frequency_seconds:number;
  max_requests:number;used_requests:number;max_concurrency:number;deadline:string;
  stop_condition:'request_limit'|'first_failure';state:string;last_result:string|null;
  last_run_at:string|null;next_run_at:string|null;created_at:string;started_at:string|null;
  stopped_at:string|null;transport:'mock';network_called:false;
}
export interface ProbeTasksResponse{status:'ready';mode:'mock_only';items:ProbeTask[];total:number;network_called:false}
export interface HighCostTestStatus{
  status:'disabled'|'mock_only';capability_id:'ADV-019';enabled:boolean;mode:'mock_only';
  real_execution_allowed:false;policy_version:string;state_counts:Record<string,number>;
  known_currencies:string[];limits:Record<string,Record<string,number>>;
  reserved_by_currency:Record<string,number>;authorization_status:string;network_called:false;
  kill_switches:GovernanceKillSwitch[];items:Array<{test_id:string;environment_id:string;model_id:string;
    currency:string;estimated_cost:number;actual_cost:number|null;state:string;created_at:string;
    expires_at:string;reserved_at:string|null;started_at:string|null;reconciled_at:string|null;
    terminated_at:string|null;revision:number;policy_version:string}>;
}
export interface SafetyGovernanceTimeline{
  status:'ready'|'empty';series:Array<{domain:string;event_type:string;timestamp:string}>;
  latest_event_at:string|null;freshness_status:'fresh'|'stale'|'unknown';stale_after_seconds:number;
  scheduler_overhead:{status:'pending_confirmation';sample_count:0;reason:string};
  permission_boundary:string;generated_demo_points:false;network_called:false;
}
export interface FormalAgentSkillDiscovery{
  registry_version:string;skill_count:number;host_deployment_status:'repository_harness_only';
  network_called:false;skills:Array<{skill_id:string;version:string;binding:string;
    read_only:true;artifact_sha256:string}>;
}
export interface FormalAgentSkillStatus{
  status:'ready'|'degraded'|'unavailable';registered_skill_count:number;
  browser_invocation_allowed:false;grant_issuance_allowed:false;
  host_deployment_status:'repository_harness_only';network_called:false;
}
export interface FormalAgentSkillInvocation{
  invocation_id:string;skill_id:string;status:'success'|'unavailable';
  data:Record<string,unknown>;uncertainty:string;provenance:Record<string,unknown>;
  limitations:string[];audit_id:string;network_called:false;
}
export interface MultimodalValidationResult{
  validation_id:string;status:'validated'|'blocked';allowed:boolean;reason:string;
  request_type:'image'|'audio'|'video';mime_type:string;input_bytes:number;
  model_id:string;channel_id:string;evidence_ids:string[];registry_version:string;
  network_called:false;media_persisted:false;
}
export interface AttributionChain{
  decision_id:string;status:string;authoritative_actual_channel:string|null;
  scheduler_decision:null|{run_id:string;request_id:string;selected_target_id:string|null;
    selected_channel_id:string|null;model_id:string;policy_version:string|null;
    metric_snapshot_ids:string[];confidence_snapshot_ids:string[];
    downstream_correlation_id:string|null;execution_status:string|null};
  execution_events:Array<{event_id:string;downstream_correlation_id:string;
    authoritative_actual_channel:string;execution_result:string;source_type:string;
    observed_at:string}>;event_count:number;
}
export interface AttributionChainsResponse{
  status:string;items:AttributionChain[];total:number;network_called:false;
}
export interface PriceCatalogStatus{
  status:'fresh'|'stale'|'expired'|'unavailable';source_id:string;
  catalog_version:string|null;record_count:number;fetched_at?:string;
  last_status?:string;last_error_code?:string|null;network_called:false;
  policy_version?:string;automatic_sync_state?:string;scheduler_consumption?:string;
  model_catalog?:{status:'ready'|'unavailable';price_version:string|null;
    environment_id?:string;effective_from?:string;effective_to?:string|null;
    source_type?:string;source_reference?:string;captured_at?:string;checksum?:string;
    model_count:number;confirmed_count:number;pending_count:number};
}
export interface PriceAuditResponse{
  status:'ready';items:Array<{audit_id:string;event_type:string;source_id:string;
    created_at:string;details:Record<string,unknown>}>;network_called:false;
}
export interface SchedulerOverheadStage{
  stage:string;sample_count:number;p50_ms:number|null;p95_ms:number|null;
  p99_ms:number|null;max_ms:number|null;
}
export interface SchedulerOverheadResponse{
  status:'ready'|'never_measured';window_minutes:number;sample_count:number;
  cold_start_sample_count:number;stages:SchedulerOverheadStage[];
  provider_latency_included:false;network_called:false;metric_definition:string;
  excluded_time:string[];percentiles:string[];
}
export interface SchedulerPerformanceResponse{
  scope:{environment_id:string;window_minutes:number;start:string;end:string;traffic_class:string;
    strategy_id:string|null;model_id:string|null;stream:boolean|null};
  kpis:{request_count:number;scheduler_p50_ms:number|null;scheduler_p95_ms:number|null;
    scheduler_p99_ms:number|null;scheduler_end_to_end_ratio:number|null;largest_stage:string|null};
  stage_series:Array<Record<string,string|number|null>>;
  latency_series:Array<{time:string;p50_ms:number|null;p95_ms:number|null;p99_ms:number|null}>;
  stage_distribution:Array<{stage:string;average_ms:number|null;p95_ms:number|null;sample_count:number;share:number|null}>;
  provider_comparison:Array<{time:string;request_id:string;scheduler_ms:number;provider_ms:number|null;end_to_end_ms:number}>;
  strategy_comparison:Array<{strategy_id:string;sample_count:number;p50_ms:number|null;p95_ms:number|null;
    average_candidate_count:number|null;average_filtered_count:number|null;average_scoring_ms:number|null}>;
  recent_records:Array<{local_request_id:string;provider_request_id:string|null;decision_id:string|null;
    occurred_at:string;strategy_id:string;model_id:string|null;candidate_count:number|null;filtered_count:number|null;
    scheduler_total_ms:number;provider_latency_ms:number|null;end_to_end_ms:number;success:number;stream:number;
    traffic_class:string;source_type:string;error_category:string|null}>;
  freshness:{generated_at:string;latest_record_at:string|null};coverage:{scheduler_samples:number;provider_samples:number;
    end_to_end_samples:number;source_types:string[]};
}
export interface DecisionReconstruction{
  schema_version?:string;decision_id:string;run_id?:string;request_id?:string;
  status:'ready'|'blocked';reason?:string;missing_fields?:string[];
  request_classification?:Record<string,unknown>;
  policy_binding?:{runtime_version?:string;catalog_version?:string;
    catalog_sha256?:string;decision_policy_version?:string;
    metric_snapshot_ids?:string[];confidence_snapshot_ids?:string[]};
  candidate_evaluation?:{candidate_count:number;eligible_count:number;excluded_count:number;
    candidate_ranking:Array<Record<string,unknown>>;excluded_candidates:Array<Record<string,unknown>>;
    selected_target_id?:string|null;selected_channel_id?:string|null;fallback_order:string[]};
  decision_result?:Record<string,unknown>;
  execution_trace?:{execution_attempted:boolean;execution_status?:string;
    attempts:Array<Record<string,unknown>>;fallback_executed:boolean;fallback_trace:string[];
    stopped_reason?:string|null;error_category?:string|null;network_called:false};
  attribution?:AttributionChain;limitations?:string[];runtime_record_sha256?:string;
  component_sha256?:Record<string,string>;reconstruction_sha256?:string;network_called:false;
}
export interface StandardizedCallLog{
  cursor_id:number;record_id:string;occurred_at:string;request_id:string|null;
  response_id:string|null;decision_id:string|null;local_request_id?:string|null;
  provider_request_id?:string|null;provider_response_id?:string|null;
  provider_trace_id?:string|null;client_correlation_id?:string|null;
  provider_log_id?:string|null;provider_sync_failure_reason?:string|null;
  match_method?:string|null;match_confidence?:string|null;
  cost_status?:string|null;cost_type?:string|null;environment_id:string;
  requested_model:string;actual_model:string|null;provider:string|null;
  channel_id:string|null;channel_name:string|null;endpoint_type:string|null;
  stream:boolean;request_status:string;http_status:number|null;
  error_code:string|null;error_category:string|null;retryable:boolean|null;
  attempt_number:number;total_attempts:number;total_latency_ms:number|null;
  first_token_latency_ms:number|null;input_tokens:number|null;
  cached_input_tokens:number|null;output_tokens:number|null;
  cost_amount:string|null;currency:string|null;configuration_version:string|null;
  metric_snapshot_id:string|null;source_type:'historical_uat_csv'|'realtime_execution';
  evidence_scope:'model_usage_only'|'channel_eligible';is_historical:boolean;
  created_at:string;updated_at:string;
}
export interface CallLogsResponse{
  status:'ready';schema_version:string;items:StandardizedCallLog[];
  total:number;historical_count:number;next_cursor:number;updated_at:string|null;
}
export interface CallLogAnalytics{
  status:'ready';schema_version:string;environment_id:string;request_count:number;
  success_count:number;failure_count:number;success_rate:number|null;
  total_latency_ms:number;average_latency_ms:number|null;p50_latency_ms:number|null;
  p95_latency_ms:number|null;p99_latency_ms:number|null;
  average_first_token_latency_ms:number|null;input_tokens:number;
  cached_input_tokens:number;output_tokens:number;total_cost:string|null;currency:string|null;
  average_cost:string|null;stream_count:number;nonstream_count:number;
  model_mismatch_count:number;model_counts:Record<string,number>;model_costs:Record<string,string>;
  error_categories:Record<string,number>;retry_count:number;fallback_count:number;
  today_request_count:number;today_tokens:number;today_cost:string|null;
  measurement_coverage?:{latency:number;tokens:number;cost:number};
  trend:Array<{date:string;request_count:number;success_rate:number|null;
    average_latency_ms:number|null;total_tokens:number;total_cost:string|null;currency:string|null}>;
  generated_at:string;
}
export interface ObservabilityOverviewResponse{
  scope:{environment_id:string;time_range:string;start:string;end:string;traffic_class:string;
    model_id:string|null;stream:boolean|null;source_type:string};
  filters:{models:string[]};
  kpis:{requests:number;success_rate:number|null;p50_ms:number|null;p95_ms:number|null;p99_ms:number|null;
    ttft_p95_ms:number|null;tokens:number;actual_cost:string|null;latency_coverage:number;
    ttft_coverage:number;token_coverage:number;cost_coverage:number;deltas:Record<string,number|null>;
    provider_actual_cost:string|null;historical_actual_cost:string|null;estimated_cost:string|null;
    pending_cost_count:number};
  request_series:Array<{time:string;business:number;probe:number;requests:number;success_rate:number}>;
  latency_series:Array<{time:string;p50_ms:number|null;p95_ms:number|null;p99_ms:number|null;ttft_p95_ms:number|null}>;
  token_series:Array<{time:string;input_tokens:number;cached_tokens:number;output_tokens:number}>;
  cost_series:Array<{time:string;provider_actual:string|null;historical_actual:string|null;estimated:string|null;pending:number}>;
  model_distribution:Array<{model_id:string;request_count:number;success_rate:number|null;p95_ms:number|null;
    tokens:number;cost:string|null;source_types:string[]}>;
  stream_distribution:{stream:number;nonstream_completion:number;non_completion:number};
  error_distribution:Array<{code:string;count:number}>;
  reliability:{fallback_rate:number|null;retry_rate:number|null;open_count:number;half_open_count:number;
    active_probes:number;timeline:Array<Record<string,unknown>>};
  freshness:{watermark:number|null;last_synced_at:string|null;source_counts:Record<string,number>;
    provider_log_status:string};
  coverage:{records:number;latency:number;ttft:number;tokens:number;actual_cost:number};
  recent_records:Array<{occurred_at:string;traffic_class:string;model_id:string;status:string;
    total_latency_ms:number|null;first_token_ms:number|null;tokens:number;cost:string|null;
    cost_type:string|null;request_id:string|null;decision_id:string|null;error_category:string|null}>;
  generated_at:string;
}
function uploadForm(file:File,sourceType:string,mapping:Record<string,string>={}){
  const form=new FormData();form.append('file',file);form.append('source_type',sourceType);form.append('mapping',JSON.stringify(mapping));form.append('environment_id',activeEnvironmentFilter==='all'?'china_uat':activeEnvironmentFilter);return form;
}
let activeEnvironmentFilter:EnvironmentFilter='all';
const environmentQuery=()=>`&environment_id=${encodeURIComponent(activeEnvironmentFilter)}`;
export const api={
  enterpriseSession:(signal?:AbortSignal)=>apiRequest<{
    status:string;principal:{principal_id:string;principal_type:'human';tenant_id:string;
    workspace_id:string;roles:string[];authn_method:string;expires_at:string;
    is_development_identity:boolean};
  }>('/api/v1/security/session',{signal}),
  logoutEnterpriseSession:()=>apiRequest<{status:'revoked'}>(
    '/api/v1/security/session/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  setEnvironmentFilter:(value:EnvironmentFilter)=>{activeEnvironmentFilter=value;},
  overview:(mode:DataMode,signal?:AbortSignal,timeRange='all')=>apiRequest<OverviewResponse>(`/api/v1/overview?mode=${mode}${environmentQuery()}&time_range=${encodeURIComponent(timeRange)}`,{signal}),
  health:(mode:DataMode,signal?:AbortSignal)=>apiRequest<HealthResponse>(`/api/v1/health/channels?mode=${mode}${environmentQuery()}`,{signal}),
  metricSnapshots:(window:MetricWindow,environmentId:EnvironmentFilter,limit=50,offset=0,signal?:AbortSignal)=>{
    const params=new URLSearchParams({window,limit:String(limit),offset:String(offset)});
    if(environmentId!=='all')params.set('environment_id',environmentId);
    return apiRequest<MetricSnapshotsResponse>(`/api/v1/metrics/snapshots?${params.toString()}`,{signal});
  },
  dynamicMetricsOverview:(window:MetricWindow,environmentId:EnvironmentFilter,trafficClass='business',signal?:AbortSignal)=>{
    const params=new URLSearchParams({window,traffic_class:trafficClass});
    params.set('environment_id',environmentId==='all'?'china_uat':environmentId);
    return apiRequest<DynamicMetricsOverview>(`/api/v1/dynamic-metrics/overview?${params.toString()}`,{signal});
  },
  ensureDynamicMetrics:()=>apiRequest<{job_id:string;status:string;already_running:boolean;watermark:string|null;started_at:string|null}>(
    '/api/v1/dynamic-metrics/ensure',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  modelCapabilitiesOverview:(environmentId='china_uat',signal?:AbortSignal)=>apiRequest<ModelCapabilitiesOverview>(
    `/api/v1/model-capabilities/overview?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  modelCapabilityDetail:(modelId:string,environmentId='china_uat',signal?:AbortSignal)=>apiRequest<ModelCapabilityItem>(
    `/api/v1/model-capabilities/models/${encodeURIComponent(modelId)}?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  modelMappings:(environmentId='china_uat',signal?:AbortSignal)=>apiRequest<ModelMappingsResponse>(
    `/api/v1/model-mappings?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  ensureModelCapabilities:(environmentId='china_uat')=>apiRequest<{job_id:string;status:string;already_running:boolean;watermark?:string|null;started_at?:string|null}>(
    '/api/v1/model-capabilities/ensure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId})}),
  statisticalConfidence:(limit=500,signal?:AbortSignal)=>
    apiRequest<StatisticalConfidenceResponse>(
      `/api/v1/statistical-confidence/snapshots?confidence_version=weighted_wilson_v1&limit=${limit}`,
      {signal}),
  circuitBreakerStatus:(signal?:AbortSignal)=>apiRequest<CircuitBreakerStatus>(
    '/api/v1/circuit-breakers/status',{signal}),
  circuitBreakerTransitions:(circuitId:string,signal?:AbortSignal)=>apiRequest<{
    status:string;state:Record<string,unknown>;items:Array<Record<string,unknown>>;network_called:false;
  }>(`/api/v1/circuit-breakers/${encodeURIComponent(circuitId)}/transitions`,{signal}),
  recordCircuitEvent:(circuitId:string,action:'success'|'failure',probeLeaseId?:string)=>
    apiRequest<{status:string;result:Record<string,unknown>;network_called:false}>(
      `/api/v1/circuit-breakers/${encodeURIComponent(circuitId)}/events`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action,probe_lease_id:probeLeaseId||null})}),
  explorationStatus:(signal?:AbortSignal)=>apiRequest<ExplorationStatus>(
    '/api/v1/exploration-governance/status',{signal}),
  capabilityEvidence:(signal?:AbortSignal)=>apiRequest<CapabilityEvidenceResponse>(
    '/api/v1/capability-evidence?limit=100',{signal}),
  trafficChangeStatus:(signal?:AbortSignal)=>apiRequest<TrafficChangeStatus>(
    '/api/v1/traffic-change-governance/status?limit=100',{signal}),
  trafficControlCurrent:(signal?:AbortSignal)=>apiRequest<TrafficControlCurrent>(
    '/api/v1/traffic-change-governance/current',{signal}),
  trafficControlChannels:(signal?:AbortSignal)=>apiRequest<{status:'ready';items:TrafficControlChannel[];total:number;network_called:false}>(
    '/api/v1/traffic-change-governance/channels',{signal}),
  trafficControlImpact:(targetModelId:string,targetChannelId:string,signal?:AbortSignal)=>apiRequest<TrafficControlImpact>(
    `/api/v1/traffic-change-governance/impact?target_model_id=${encodeURIComponent(targetModelId)}&target_channel_id=${encodeURIComponent(targetChannelId)}`,{signal}),
  trafficControlProposals:(signal?:AbortSignal)=>apiRequest<{status:'ready';items:TrafficControlProposal[];total:number;network_called:false}>(
    '/api/v1/traffic-change-governance/proposals?limit=100',{signal}),
  createTrafficControlProposal:(body:TrafficControlProposalInput)=>apiRequest<TrafficControlProposal>(
    '/api/v1/traffic-change-governance/control-proposals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  deleteTrafficControlProposal:(id:string)=>apiRequest<{proposal_id:string;state:'DELETED';network_called:false}>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}`,{method:'DELETE'}),
  trafficControlAction:(id:string,action:'validate'|'submit'|'pause'|'resume'|'complete',body:Record<string,unknown>={})=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  approveTrafficControlProposal:(id:string,approvalReference?:string)=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/control-approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approval_reference:approvalReference||null})}),
  rejectTrafficControlProposal:(id:string,reason:string)=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/reject`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})}),
  activateTrafficControlProposal:(id:string,requestIds:string[]=[],decisionIds:string[]=[])=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/control-activate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_ids:requestIds,decision_ids:decisionIds})}),
  adjustTrafficControlProposal:(id:string,rolloutPercent:number)=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/adjust`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rollout_percent:rolloutPercent})}),
  rollbackTrafficControlProposal:(id:string,reason:string)=>apiRequest<TrafficControlProposal>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/rollback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})}),
  trafficControlMetrics:(id:string,signal?:AbortSignal)=>apiRequest<TrafficControlLiveMetrics>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/metrics`,{signal}),
  trafficControlAudit:(id:string,signal?:AbortSignal)=>apiRequest<{status:'ready';proposal_id:string;items:Array<{audit_id:number;event_type:string;actor_id:string;created_at:string;details:Record<string,unknown>}>;network_called:false}>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(id)}/audit`,{signal}),
  proposeTrafficChange:(body:Record<string,unknown>)=>apiRequest<Record<string,unknown>>(
    '/api/v1/traffic-change-governance/proposals',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  approveTrafficChange:(proposalId:string,body:Record<string,unknown>)=>apiRequest<Record<string,unknown>>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(proposalId)}/approve`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  activateTrafficChange:(proposalId:string,body:Record<string,unknown>)=>apiRequest<Record<string,unknown>>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(proposalId)}/activate`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  terminateTrafficChange:(proposalId:string,body:Record<string,unknown>)=>apiRequest<Record<string,unknown>>(
    `/api/v1/traffic-change-governance/proposals/${encodeURIComponent(proposalId)}/terminate`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  probeGovernanceStatus:(signal?:AbortSignal)=>apiRequest<ProbeGovernanceStatus>(
    '/api/v1/probe-governance/status?limit=100',{signal}),
  probeTasks:(signal?:AbortSignal)=>apiRequest<ProbeTasksResponse>(
    '/api/v1/probe-governance/tasks?limit=100',{signal}),
  latestStrategyEffect:(signal?:AbortSignal)=>apiRequest<{status:'ready'|'no_real_data';item:Record<string,unknown>|null}>(
    '/api/v1/strategy-effects/latest',{signal}),
  latestContinuousProbe:(signal?:AbortSignal)=>apiRequest<{status:'ready'|'no_real_data';item:Record<string,unknown>|null}>(
    '/api/v1/continuous-probes/latest',{signal}),
  probes:(params:{query?:string;status?:string;time_range?:string;start?:string;end?:string;limit?:number;offset?:number}={},signal?:AbortSignal)=>{
    const search=new URLSearchParams();
    if(params.query)search.set('q',params.query);if(params.status)search.set('status',params.status);
    if(params.limit!==undefined)search.set('limit',String(params.limit));if(params.offset!==undefined)search.set('offset',String(params.offset));
    const now=new Date();
    if(params.time_range==='today'){const start=new Date(now);start.setHours(0,0,0,0);search.set('started_from',start.toISOString());}
    if(params.time_range==='7d')search.set('started_from',new Date(now.getTime()-7*86400000).toISOString());
    if(params.start)search.set('started_from',params.start);if(params.end)search.set('started_to',params.end);
    return apiRequest<ProbeListResponse>(`/api/v1/probes${search.size?`?${search}`:""}`,{signal});
  },
  createProbe:(body:ProbeCreateInput)=>apiRequest<ProbeRun>('/api/v1/probes',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  probe:(probeRunId:string,signal?:AbortSignal)=>apiRequest<ProbeRun>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}`,{signal}),
  probeAction:(probeRunId:string,action:'pause'|'resume'|'stop')=>apiRequest<ProbeRun>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}/${action}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  cloneProbe:(probeRunId:string)=>apiRequest<ProbeRun|{configuration:ProbeCreateInput}>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}/clone`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  probeMetrics:(probeRunId:string,signal?:AbortSignal)=>apiRequest<ProbeMetrics>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}/metrics`,{signal}),
  probeEvents:(probeRunId:string,signal?:AbortSignal)=>apiRequest<{items:ProbeEvent[];total?:number;next_after_event_id?:string|null}>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}/events`,{signal}),
  probeRequests:(probeRunId:string,signal?:AbortSignal)=>apiRequest<{items:ProbeRequestRecord[];total:number}>(
    `/api/v1/probes/${encodeURIComponent(probeRunId)}/requests`,{signal}),
  createProbeTask:(body:{name:string;environment_id:string;channel_id:string;model_id:string;
    request_template:'mock_success'|'mock_failure';frequency_seconds:number;max_requests:number;
    max_concurrency:number;deadline:string;stop_condition:'request_limit'|'first_failure'})=>
    apiRequest<ProbeTask>('/api/v1/probe-governance/tasks',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  startProbeTask:(taskId:string)=>apiRequest<ProbeTask>(
    `/api/v1/probe-governance/tasks/${encodeURIComponent(taskId)}/start`,{method:'POST',body:'{}'}),
  stopProbeTask:(taskId:string)=>apiRequest<ProbeTask>(
    `/api/v1/probe-governance/tasks/${encodeURIComponent(taskId)}/stop`,{method:'POST',body:'{}'}),
  highCostTestStatus:(signal?:AbortSignal)=>apiRequest<HighCostTestStatus>(
    '/api/v1/high-cost-test-governance/status?limit=100',{signal}),
  safetyGovernanceTimeline:(signal?:AbortSignal)=>apiRequest<SafetyGovernanceTimeline>(
    '/api/v1/safety-governance/timeline?limit=200',{signal}),
  formalAgentSkillDiscovery:(signal?:AbortSignal)=>apiRequest<FormalAgentSkillDiscovery>(
    '/api/v1/formal-agent-skills/discovery',{signal}),
  formalAgentSkillStatus:(signal?:AbortSignal)=>apiRequest<FormalAgentSkillStatus>(
    '/api/v1/formal-agent-skills/status',{signal}),
  invokeFormalAgentSkill:(body:{skill_id:string;arguments:Record<string,unknown>;
    decision_id:string;evidence_ids:string[]})=>apiRequest<FormalAgentSkillInvocation>(
      '/api/v1/formal-agent-skills/invoke-local',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  managedSkills:(signal?:AbortSignal)=>apiRequest<ManagedSkillCatalog>('/api/v1/skills',{signal}),
  managedSkill:(skillId:string,signal?:AbortSignal)=>apiRequest<Record<string,unknown>>(
    `/api/v1/skills/${encodeURIComponent(skillId)}`,{signal}),
  invokeManagedSkill:(skillId:string,argumentsValue:Record<string,unknown>)=>apiRequest<ManagedSkillInvocation>(
    `/api/v1/skills/${encodeURIComponent(skillId)}/invoke`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({arguments:argumentsValue})},150_000),
  managedSkillInvocations:(skillId:string,signal?:AbortSignal)=>apiRequest<{items:ManagedSkillInvocation[]}>(
    `/api/v1/skills/${encodeURIComponent(skillId)}/invocations?limit=20`,{signal}),
  managedSkillAction:(skillId:string,action:'validate'|'enable'|'disable',body:Record<string,unknown>={})=>
    apiRequest<Record<string,unknown>>(`/api/v1/skills/${encodeURIComponent(skillId)}/${action}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  managedSkillLifecycle:(skillId:string,action:'publish'|'install'|'upgrade'|'rollback'|'uninstall',
    version?:string,installationId='current')=>{
      const root=`/api/v1/skills/${encodeURIComponent(skillId)}`;
      const path=action==='publish'?`${root}/versions/${encodeURIComponent(version??'')}/publish`:
        action==='uninstall'?`${root}/installations/${encodeURIComponent(installationId)}`:`${root}/${action}`;
      return apiRequest<Record<string,unknown>>(path,{method:action==='uninstall'?'DELETE':'POST',
        headers:{'Content-Type':'application/json'},body:action==='uninstall'?undefined:JSON.stringify({version})});
    },
  managedSkillAudits:(signal?:AbortSignal)=>apiRequest<{items:Array<Record<string,unknown>>}>(
    '/api/v1/skills/audit-records?limit=200',{signal}),
  managedSkillHosts:(signal?:AbortSignal)=>apiRequest<ManagedSkillHostList>('/api/v1/skills/hosts',{signal}),
  managedSkillHost:(hostId:string,signal?:AbortSignal)=>apiRequest<ManagedSkillHost>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}`,{signal}),
  createManagedSkillHost:(body:ManagedSkillHostInput)=>apiRequest<ManagedSkillHost>('/api/v1/skills/hosts',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  updateManagedSkillHost:(hostId:string,body:Partial<ManagedSkillHostInput>)=>apiRequest<ManagedSkillHost>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}`,{
      method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  setManagedSkillHostCredential:(hostId:string,body:{auth_type:string;credential:string;header_name?:string})=>
    apiRequest<{credential_fingerprint:string;status:SkillHostStatus}>(
      `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/credentials`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  revokeManagedSkillHostCredential:(hostId:string)=>apiRequest<{status:SkillHostStatus}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/credentials`,{method:'DELETE'}),
  testManagedSkillHost:(hostId:string)=>apiRequest<ManagedSkillHostTestResult>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/test`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  setManagedSkillHostEnabled:(hostId:string,enabled:boolean)=>apiRequest<ManagedSkillHost>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/${enabled?'enable':'disable'}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  managedSkillHostCapabilities:(hostId:string,signal?:AbortSignal)=>apiRequest<{items:Array<Record<string,unknown>>}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/capabilities`,{signal}),
  managedSkillHostInstallations:(hostId:string,signal?:AbortSignal)=>apiRequest<{items:ManagedSkillHostInstallation[]}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/installations`,{signal}),
  installManagedSkillOnHost:(hostId:string,body:{skill_id:string;version:string})=>apiRequest<ManagedSkillHostInstallation>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/installations`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  uninstallManagedSkillFromHost:(hostId:string,installationId:string)=>apiRequest<{status:string}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/installations/${encodeURIComponent(installationId)}`,
    {method:'DELETE'}),
  setManagedSkillHostInstallationEnabled:(hostId:string,installationId:string,enabled:boolean)=>
    apiRequest<ManagedSkillHostInstallation>(
      `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/installations/${encodeURIComponent(installationId)}/${enabled?'enable':'disable'}`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  invokeManagedSkillHost:(hostId:string,body:{skill_id:string;arguments:Record<string,unknown>})=>
    apiRequest<ManagedSkillHostInvocation>(`/api/v1/skills/hosts/${encodeURIComponent(hostId)}/invoke`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},150_000),
  managedSkillHostInvocations:(hostId:string,signal?:AbortSignal)=>apiRequest<{items:ManagedSkillHostInvocation[]}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/invocations`,{signal}),
  managedSkillHostAudits:(hostId:string,signal?:AbortSignal)=>apiRequest<{items:Array<Record<string,unknown>>}>(
    `/api/v1/skills/hosts/${encodeURIComponent(hostId)}/audit`,{signal}),
  validateMultimodal:(body:{environment_id:string;model_id:string;channel_id:string;
    subject_version:string;request_type:'image'|'audio'|'video';mime_type:string;
    input_bytes:number})=>apiRequest<MultimodalValidationResult>('/api/v1/multimodal/validate',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  schedulerAttribution:(signal?:AbortSignal)=>apiRequest<AttributionChainsResponse>(
    '/api/v1/scheduler-attribution/chains?limit=100',{signal}),
  activeAcceptanceRun:(signal?:AbortSignal)=>apiRequest<{acceptance_run_id:string;status:string;started_at:string}>(
    '/api/v1/acceptance-runs/active?environment_id=china_uat',{signal}),
  acceptanceLiveSummary:(runId:string,signal?:AbortSignal)=>apiRequest<AcceptanceRunSummary>(
    `/api/v1/acceptance-runs/${encodeURIComponent(runId)}/live-summary`,{signal}),
  priceStatus:(signal?:AbortSignal)=>apiRequest<PriceCatalogStatus>(
    '/api/v1/prices/status',{signal}),
  priceAudit:(signal?:AbortSignal)=>apiRequest<PriceAuditResponse>(
    '/api/v1/prices/audit?limit=100',{signal}),
  schedulerOverhead:(windowMinutes=60,signal?:AbortSignal)=>apiRequest<SchedulerOverheadResponse>(
    `/api/v1/scheduler/overhead?window_minutes=${windowMinutes}`,{signal}),
  schedulerPerformance:(filters:{environment_id:string;time_range:string;traffic_class:string;
    start?:string;end?:string;strategy_id?:string;model_id?:string;stream?:boolean},signal?:AbortSignal)=>{
      const params=new URLSearchParams();Object.entries(filters).forEach(([key,value])=>{
        if(value!==undefined&&value!=="")params.set(key,String(value));
      });return apiRequest<SchedulerPerformanceResponse>(`/api/v1/observability/scheduler-performance?${params}`,{signal});
    },
  decisionReconstruction:(decisionId:string,signal?:AbortSignal)=>apiRequest<DecisionReconstruction>(
    `/api/v1/scheduler/decisions/${encodeURIComponent(decisionId)}/reconstruction`,{signal}),
  stickyStatus:(signal?:AbortSignal)=>apiRequest<StickyStatus>(
    '/api/v1/sticky-routing/status',{signal}),
  stickyBindings:(filters:{environment_id?:string;state?:string;model?:string;channel?:string;limit?:number;offset?:number},signal?:AbortSignal)=>{
    const params=new URLSearchParams();
    Object.entries(filters).forEach(([key,value])=>{
      if(value!==undefined&&value!=="")params.set(key,String(value));
    });
    return apiRequest<StickyBindingsResponse>(
      `/api/v1/sticky-routing/bindings?${params.toString()}`,{signal});
  },
  stickyBinding:(bindingId:string,signal?:AbortSignal)=>apiRequest<StickyBinding>(
    `/api/v1/sticky-routing/bindings/${encodeURIComponent(bindingId)}`,{signal}),
  stickyMetrics:(environmentId?:string,signal?:AbortSignal)=>apiRequest<StickyMetrics>(
    `/api/v1/sticky-routing/metrics${environmentId?`?environment_id=${encodeURIComponent(environmentId)}`:""}`,{signal}),
  invalidateStickyBinding:(bindingId:string,environmentId:string,reason:string,idempotencyKey:string)=>
    apiRequest<{status:string;binding:StickyBinding;idempotent_replay:boolean}>(
      `/api/v1/sticky-routing/bindings/${encodeURIComponent(bindingId)}/invalidate`,
      {method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':idempotencyKey},
       body:JSON.stringify({environment_id:environmentId,reason,
         confirmation_text:'我确认使此粘性路由绑定失效'})}),
  refreshMetricSnapshots:(rebuild=false)=>apiRequest<{
    status:string;rebuild:boolean;inserted?:number;duplicates?:number;
    event_count?:number;rejected_count:number;network_called:false;
    protected_evidence_modified:false;
  }>('/api/v1/metrics/refresh',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({explicit_confirmation:true,rebuild})}),
  costBudget:(mode:DataMode,signal?:AbortSignal)=>apiRequest<CostBudgetResponse>(`/api/v1/budgets/summary?mode=${mode}${environmentQuery()}`,{signal}),
  get:<T>(path:string,mode:DataMode,signal?:AbortSignal)=>apiRequest<T>(`${path}?mode=${mode}${environmentQuery()}`,{signal}),
  preview:(file:File,sourceType:string,mapping:Record<string,string>={})=>apiRequest<ImportBatch>('/api/v1/imports/preview',{method:'POST',body:uploadForm(file,sourceType,mapping)}),
  validate:(file:File,sourceType:string,mapping:Record<string,string>={})=>apiRequest<ImportBatch>('/api/v1/imports/validate',{method:'POST',body:uploadForm(file,sourceType,mapping)}),
  confirm:(file:File,sourceType:string,mapping:Record<string,string>={})=>apiRequest<ImportBatch>('/api/v1/imports/confirm',{method:'POST',body:uploadForm(file,sourceType,mapping)},20000),
  imports:(signal?:AbortSignal)=>apiRequest<{items:ImportBatch[]}>('/api/v1/imports',{signal}),
  qualityReport:(batchId:string)=>apiText(`/api/v1/imports/${encodeURIComponent(batchId)}/quality-report`),
  uatStatus:(signal?:AbortSignal)=>apiRequest<UatStatus>('/api/v1/uat/status',{signal}),
  uatEditorOptions:(signal?:AbortSignal,channelId='unified-routing')=>apiRequest<UatEditorOptions>(`/api/v1/uat/editor-options?channel_id=${encodeURIComponent(channelId)}`,{signal}),
  uatExecutionControl:(signal?:AbortSignal)=>apiRequest<UatExecutionControl>('/api/v1/uat/execution-control',{signal}),
  createUatExecutionControl:(body:Record<string,unknown>)=>apiRequest<UatExecutionControl>('/api/v1/uat/execution-control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  approveUatExecutionControl:(taskId:string,body:Record<string,unknown>)=>apiRequest<UatExecutionControl>(`/api/v1/uat/execution-control/${encodeURIComponent(taskId)}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  activateUatExecutionControl:(taskId:string)=>apiRequest<UatExecutionControl>(`/api/v1/uat/execution-control/${encodeURIComponent(taskId)}/activate`,{method:'POST'}),
  reviewUatOutputCapability:(body:Record<string,unknown>)=>apiRequest<{status:string;capability:UatModelOutputCapability}>('/api/v1/uat/output-capabilities/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  stopUatExecutionControl:(reason:string,killSwitch=false)=>apiRequest<UatExecutionControl>('/api/v1/uat/execution-control/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason,kill_switch:killSwitch})}),
  uatModels:(refresh=false,signal?:AbortSignal)=>apiRequest<UatModelCatalog>(`/api/v1/uat/models${refresh?'?refresh=true':''}`,{signal}),
  credentialStatus:(signal?:AbortSignal)=>apiRequest<CredentialStatus>('/api/v1/uat/credentials/status',{signal}),
  setSessionCredential:(apiKey:string)=>apiRequest<CredentialStatus>('/api/v1/uat/credentials/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:apiKey,confirmation:{confirmed:true,text:'I authorize encrypted persistence of this UAT key for this Windows user until replacement or logout.'}})}),
  clearSessionCredential:()=>apiRequest<CredentialStatus>('/api/v1/uat/credentials/session',{method:'DELETE',headers:{'Content-Type':'application/json'},body:'{}'}),
  testCredential:()=>apiRequest<ConnectionTest>('/api/v1/uat/credentials/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  validateUat:(body:UatRequestBody)=>apiRequest<UatExecutionResult['validation']>('/api/v1/uat/validate-request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  executeUat:(body:UatRequestBody)=>apiRequest<UatExecutionResult>('/api/v1/uat/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},40000),
  uatExecutions:(signal?:AbortSignal)=>apiRequest<{items:Array<Record<string,unknown>>}>('/api/v1/uat/executions',{signal}),
  callLogs:(environmentId:EnvironmentId,afterCursor=0,limit=200,signal?:AbortSignal)=>
    apiRequest<CallLogsResponse>(`/api/v1/call-logs?environment_id=${encodeURIComponent(environmentId)}&after_cursor=${afterCursor}&limit=${limit}`,{signal}),
  routingDecisions:(requestId?:string,signal?:AbortSignal)=>apiRequest<{status:string;items:RoutingDecisionDetail[];total:number}>(`/api/v1/routing-decisions${requestId?`?request_id=${encodeURIComponent(requestId)}`:''}`,{signal}),
  routingDecision:(decisionId:string,signal?:AbortSignal)=>apiRequest<RoutingDecisionDetail>(`/api/v1/routing-decisions/${encodeURIComponent(decisionId)}`,{signal}),
  callLogAnalytics:(environmentId:EnvironmentId,signal?:AbortSignal,filters:{model?:string;
    source_type?:string;channel_id?:string;provider?:string;request_status?:string;
    stream?:boolean;occurred_from?:string;occurred_to?:string}={})=>{
    const params=new URLSearchParams({environment_id:environmentId});
    Object.entries(filters).forEach(([key,value])=>{
      if(value!==undefined&&value!=="")params.set(key,String(value));
    });
    return apiRequest<CallLogAnalytics>(`/api/v1/call-logs/analytics?${params.toString()}`,{signal});
  },
  observabilityOverview:(filters:{environment_id:string;time_range:string;start?:string;end?:string;
    traffic_class:string;model_id?:string;stream?:boolean;source_type:string},signal?:AbortSignal)=>{
    const params=new URLSearchParams();Object.entries(filters).forEach(([key,value])=>{
      if(value!==undefined&&value!=="")params.set(key,String(value));
    });
    return apiRequest<ObservabilityOverviewResponse>(`/api/v1/observability/overview?${params.toString()}`,{signal});
  },
  attachBackendEvidence:(executionId:string,platformLogId:string)=>apiRequest<Record<string,unknown>>(`/api/v1/uat/executions/${encodeURIComponent(executionId)}/attach-backend-evidence`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform_log_id:platformLogId||null})}),
  collectorStatus:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<CollectorStatus>(`/api/v1/collector/status?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  collectorCollections:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<{environment_id:EnvironmentId;items:CollectorCollection[]}>(`/api/v1/collector/collections?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  collectorProgress:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<Record<string,unknown>>(`/api/v1/collector/progress?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  collectorCommand:(environmentId:EnvironmentId,dateFrom:string,dateTo:string,pages:number,records:number,signal?:AbortSignal)=>apiRequest<CollectorStatus&{command:string}>(`/api/v1/collector/command-preview?environment_id=${encodeURIComponent(environmentId)}&date_from=${encodeURIComponent(dateFrom)}&date_to=${encodeURIComponent(dateTo)}&maximum_pages=${pages}&maximum_records=${records}`,{signal}),
  collectorPreview:(environmentId:EnvironmentId,body:Record<string,unknown>)=>apiRequest<Record<string,unknown>>('/api/v1/collector/import/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,environment_id:environmentId})}),
  collectorConfirm:(environmentId:EnvironmentId,collectionId:string,payloadSha256:string)=>apiRequest<Record<string,unknown>>('/api/v1/collector/import/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,collection_id:collectionId,payload_sha256:payloadSha256,confirmed:true})}),
  startCollectorRun:(body:{environment_id:EnvironmentId;date_from:string;date_to:string;maximum_pages:number;maximum_records:number;page_delay_ms:number;operator_confirmation:boolean})=>apiRequest<{run_id:string;environment_id:EnvironmentId;status:string;browser_launch_requested:boolean}>('/api/v1/collector/runs/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  collectorRun:(runId:string,signal?:AbortSignal)=>apiRequest<CollectorRun>(`/api/v1/collector/runs/${encodeURIComponent(runId)}`,{signal}),
  confirmCollectorLogin:(runId:string,environmentId:EnvironmentId)=>apiRequest<{run_id:string;status:string}>(`/api/v1/collector/runs/${encodeURIComponent(runId)}/confirm-login`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,explicit_confirmation:true})}),
  stopCollectorRun:(runId:string,environmentId:EnvironmentId)=>apiRequest<{run_id:string;status:string}>(`/api/v1/collector/runs/${encodeURIComponent(runId)}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,explicit_confirmation:true})}),
  collectorRunPreview:(runId:string,environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<CollectorRunPreview>(`/api/v1/collector/runs/${encodeURIComponent(runId)}/preview?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  confirmCollectorRunImport:(runId:string,environmentId:EnvironmentId,collectionId:string,payloadSha256:string)=>apiRequest<Record<string,unknown>>(`/api/v1/collector/runs/${encodeURIComponent(runId)}/confirm-import`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,collection_id:collectionId,payload_sha256:payloadSha256,explicit_confirmation:true})}),
  startLogSync:(body:{environment_id:EnvironmentId;date_from:string;date_to:string;timezone:string;sync_interval_seconds:number;maximum_records:number})=>apiRequest<LogSyncJob>('/api/v1/log-sync/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  logSyncJob:(jobId:string,signal?:AbortSignal)=>apiRequest<LogSyncJob>(`/api/v1/log-sync/jobs/${encodeURIComponent(jobId)}`,{signal}),
  logSyncJobs:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<{items:LogSyncJob[]}>(`/api/v1/log-sync/jobs?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  confirmLogSyncLogin:(jobId:string,environmentId:EnvironmentId)=>apiRequest<LogSyncJob>(`/api/v1/log-sync/jobs/${encodeURIComponent(jobId)}/confirm-login`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,explicit_confirmation:true})}),
  stopLogSync:(jobId:string,environmentId:EnvironmentId)=>apiRequest<LogSyncJob>(`/api/v1/log-sync/jobs/${encodeURIComponent(jobId)}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:environmentId,explicit_confirmation:true})}),
  logSyncEvents:(environmentId:EnvironmentId,afterId=0,signal?:AbortSignal)=>apiRequest<{items:Array<{event_id:number;event_type:string;created_at:string;details:Record<string,unknown>}>;transport:string;recommended_interval_seconds:number}>(`/api/v1/log-sync/events?environment_id=${encodeURIComponent(environmentId)}&after_id=${afterId}`,{signal}),
  ensureLogSync:()=>apiRequest<{job_id:string|null;status:string;already_running:boolean;watermark:string|null;started_at:string|null}>('/api/v1/log-sync/ensure',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  logSyncStatus:(signal?:AbortSignal)=>apiRequest<LogSyncCoordinatorStatus>('/api/v1/log-sync/status',{signal}),
  logSyncHistory:(signal?:AbortSignal)=>apiRequest<{items:LogSyncJob[];environment_id:string}>('/api/v1/log-sync/history',{signal}),
  logSyncReconciliation:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<{environment_id:EnvironmentId;items:LogSyncReconciliation[]}>(`/api/v1/log-sync/reconciliation?environment_id=${encodeURIComponent(environmentId)}`,{signal}),
  shadowDashboard:(signal?:AbortSignal)=>apiRequest<ShadowDashboard>('/api/v1/shadow/dashboard',{signal}),
  ensureShadowSync:()=>apiRequest<Record<string,unknown>>('/api/v1/shadow/ensure-sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  syncShadowNow:()=>apiRequest<Record<string,unknown>>('/api/v1/shadow/sync-now',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  setShadowSyncEnabled:(enabled:boolean)=>apiRequest<Record<string,unknown>>('/api/v1/shadow/source-config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}),
  confirmShadowCorrelation:(recordId:string,executionId:string)=>apiRequest<Record<string,unknown>>(`/api/v1/shadow/correlations/${encodeURIComponent(recordId)}/confirm`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({execution_id:executionId})}),
  persistentStatus:(signal?:AbortSignal)=>apiRequest<PersistentSessionStatus>('/api/v1/log-sync/persistent/status',{signal}),
  persistentDiagnostic:(pairingId:string,signal?:AbortSignal)=>apiRequest<PersistentStorageDiagnostic>(`/api/v1/log-sync/persistent/pairing/${encodeURIComponent(pairingId)}/diagnostic`,{signal}),
  startPersistentPairing:(ttlHours:number)=>apiRequest<Record<string,unknown>>('/api/v1/log-sync/persistent/pairing/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:'china_uat',ttl_hours:ttlHours,explicit_confirmation:true})}),
  startPersistentReauthentication:(ttlHours:number)=>apiRequest<Record<string,unknown>>('/api/v1/log-sync/persistent/reauthentication/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment_id:'china_uat',ttl_hours:ttlHours,explicit_confirmation:true})}),
  persistentChromeStatus:(signal?:AbortSignal)=>apiRequest<NonNullable<PersistentSessionStatus['browser']>>('/api/v1/log-sync/persistent/browser/status',{signal}),
  openOrFocusPersistentChrome:()=>apiRequest<NonNullable<PersistentSessionStatus['browser']>&{action:string}>('/api/v1/log-sync/persistent/browser/open-or-focus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({explicit_confirmation:true})}),
  closePersistentChrome:()=>apiRequest<NonNullable<PersistentSessionStatus['browser']>>('/api/v1/log-sync/persistent/browser/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({explicit_confirmation:true})}),
  checkPersistentAuthentication:(pairingId:string)=>apiRequest<Record<string,unknown>>(`/api/v1/log-sync/persistent/pairing/${encodeURIComponent(pairingId)}/check-authentication`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({explicit_confirmation:true})}),
  confirmPersistentPairing:(pairingId:string)=>apiRequest<Record<string,unknown>>(`/api/v1/log-sync/persistent/pairing/${encodeURIComponent(pairingId)}/encrypted-save`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({explicit_confirmation:true})}),
  validatePersistentSession:(sessionId:string)=>apiRequest<Record<string,unknown>>(`/api/v1/log-sync/persistent/sessions/${encodeURIComponent(sessionId)}/validate`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({environment_id:'china_uat',explicit_confirmation:true})}),
  startPersistentLogSync:(body:{environment_id:EnvironmentId;date_from:string;date_to:string;timezone:string;periodic_polling:boolean;sync_interval_seconds:number;maximum_records:number;maximum_http_reads:number;maximum_records_observed:number;maximum_records_accepted:number;maximum_elapsed_seconds:number;page_size:number;maximum_pages:number;persistent_session_id:string;explicit_confirmation:boolean})=>apiRequest<LogSyncJob>('/api/v1/log-sync/persistent/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  setPersistentAutoResume:(sessionId:string,enabled:boolean)=>apiRequest<Record<string,unknown>>('/api/v1/log-sync/persistent/auto-resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({persistent_session_id:sessionId,enabled,explicit_confirmation:true})}),
  revokePersistentSession:(sessionId:string,confirmationText:string)=>apiRequest<Record<string,unknown>>('/api/v1/log-sync/persistent/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({persistent_session_id:sessionId,confirmation_text:confirmationText})}),
  environments:(signal?:AbortSignal)=>apiRequest<EnvironmentListResponse>('/api/v1/environments',{signal}),
  environmentStatus:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<UatStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/status`,{signal}),
  environmentExecutionState:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<EnvironmentExecutionState>(`/api/v1/environments/${encodeURIComponent(environmentId)}/execution-state`,{signal}),
  setEnvironmentExecutionState:(environmentId:EnvironmentId,enabled:boolean)=>apiRequest<EnvironmentExecutionState>(`/api/v1/environments/${encodeURIComponent(environmentId)}/execution-state`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}),
  environmentModels:(environmentId:EnvironmentId,refresh=false,signal?:AbortSignal)=>apiRequest<UatModelCatalog>(`/api/v1/environments/${encodeURIComponent(environmentId)}/models${refresh?'?refresh=true':''}`,{signal}),
  environmentCredentialStatus:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<CredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/status`,{signal}),
  setEnvironmentCredential:(environmentId:EnvironmentId,apiKey:string)=>apiRequest<CredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/session`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:apiKey,confirmation:{confirmed:true,text:'I authorize encrypted persistence of this API key for the selected environment until replacement or logout.'}})}),
  clearEnvironmentCredential:(environmentId:EnvironmentId)=>apiRequest<CredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/session`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:'{}'}),
  temporaryCredentialStatus:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<TemporaryCredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/temporary/status`,{signal}),
  setTemporaryCredential:(environmentId:EnvironmentId,apiKey:string)=>apiRequest<TemporaryCredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/temporary`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:apiKey})}),
  restoreTemporaryCredentialFromVault:(environmentId:EnvironmentId)=>apiRequest<TemporaryCredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/temporary/from-persistent`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),
  clearTemporaryCredential:(environmentId:EnvironmentId)=>apiRequest<TemporaryCredentialStatus>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/temporary`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:'{}'}),
  testTemporaryCredential:(environmentId:EnvironmentId,authSessionId:string,auth:UatWorkbenchBody['auth'])=>apiRequest<ConnectionTest&{environment_id:EnvironmentId;auth_session_id:string;models:UatModel[]}>(`/api/v1/environments/${encodeURIComponent(environmentId)}/credentials/temporary/test`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auth_session_id:authSessionId,auth})},20000),
  workbenchModels:(environmentId:EnvironmentId,authSessionId:string)=>apiRequest<UatModelCatalog>(`/api/v1/environments/${encodeURIComponent(environmentId)}/request-workbench/models`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auth_session_id:authSessionId})}),
  validateWorkbenchRequest:(environmentId:EnvironmentId,body:UatWorkbenchBody)=>apiRequest<ManagedSkillInvocation>('/api/v1/skills/validate-uat-request/invoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({arguments:{...body,environment_id:environmentId,endpoint:body.path}})}).then(value=>({...value.data,skill_trace:{skill_id:value.skill_id,skill_version:value.skill_version,invocation_id:value.invocation_id,audit_id:value.audit_id,permission_decision:value.permission_decision}} as unknown as UatWorkbenchValidation)),
  executeWorkbenchRequest:(environmentId:EnvironmentId,body:UatWorkbenchBody,signal?:AbortSignal)=>apiRequest<ManagedSkillInvocation>('/api/v1/skills/execute-uat-request/invoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({arguments:{...body,environment_id:environmentId,endpoint:body.path}}),signal},70000).then(value=>({...value.data,skill_trace:{skill_id:value.skill_id,skill_version:value.skill_version,invocation_id:value.invocation_id,audit_id:value.audit_id,permission_decision:value.permission_decision}} as unknown as UatWorkbenchResult)),
  streamWorkbenchRequest:(environmentId:EnvironmentId,body:UatWorkbenchBody,onEvent:(event:UatWorkbenchStreamEvent)=>void,signal:AbortSignal)=>apiNdjson<UatWorkbenchStreamEvent>(`/api/v1/environments/${encodeURIComponent(environmentId)}/request-workbench/execute-stream`,body,onEvent,signal),
  validateEnvironmentRequest:(environmentId:EnvironmentId,body:UatRequestBody)=>apiRequest<UatExecutionResult['validation']>(`/api/v1/environments/${encodeURIComponent(environmentId)}/validate-request`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  executeEnvironmentRequest:(environmentId:EnvironmentId,body:UatRequestBody)=>apiRequest<UatExecutionResult>(`/api/v1/environments/${encodeURIComponent(environmentId)}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},40000),
  environmentExecutions:(environmentId:EnvironmentId,signal?:AbortSignal)=>apiRequest<{items:Array<Record<string,unknown>>}>(`/api/v1/environments/${encodeURIComponent(environmentId)}/executions`,{signal}),
  validateOverseasDiscovery:()=>apiRequest<Record<string,unknown>>('/api/v1/environments/overseas/discovery/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:{confirmed:true,text:'I confirm one read-only overseas model-catalog validation request.'}})},20000),
  overseasLogPageStatus:(signal?:AbortSignal)=>apiRequest<OverseasLogPageStatus>('/api/v1/environments/overseas/log-page/status',{signal}),
  domesticLogPageStatus:(signal?:AbortSignal)=>apiRequest<DomesticLogPageStatus>('/api/v1/environments/china_uat/log-page/status',{signal}),
  previewDomesticLogPage:()=>apiRequest<DomesticLogPagePreview>('/api/v1/environments/china_uat/log-page/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:'https://uat.weimeta.cn/console/billing/logs'})}),
  confirmDomesticLogPage:(validationId:string,valueSha256:string)=>apiRequest<DomesticLogPageStatus>('/api/v1/environments/china_uat/log-page/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({validation_id:validationId,value_sha256:valueSha256,explicit_confirmation:true})}),
  previewOverseasLogPage:(url:string)=>apiRequest<OverseasLogPagePreview>('/api/v1/environments/overseas/log-page/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}),
  confirmOverseasLogPage:(validationId:string,valueSha256:string)=>apiRequest<OverseasLogPageStatus>('/api/v1/environments/overseas/log-page/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({validation_id:validationId,value_sha256:valueSha256,explicit_confirmation:true})}),
  disableOverseasLogPage:()=>apiRequest<OverseasLogPageStatus>('/api/v1/environments/overseas/log-page/disable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({explicit_confirmation:true})}),
  runReplay:(strategy:string)=>apiRequest<ReplayResult>('/api/v1/replay/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy})}),
  historicalReplaySource:(filters:Partial<HistoricalReplayInput>={},signal?:AbortSignal)=>{
    const params=new URLSearchParams();
    Object.entries(filters).forEach(([key,value])=>{
      if(value!==null&&value!==undefined&&value!==''&&!['baseline_strategy','candidate_strategy','limit'].includes(key))params.set(key,String(value));
    });
    return apiRequest<HistoricalReplaySource>(`/api/v1/historical-replay/source?${params.toString()}`,{signal});
  },
  runHistoricalReplay:(body:HistoricalReplayInput)=>apiRequest<HistoricalReplayResult>('/api/v1/historical-replay/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  historicalReplay:(replayId:string,signal?:AbortSignal)=>apiRequest<HistoricalReplayResult>(`/api/v1/historical-replay/runs/${encodeURIComponent(replayId)}`,{signal}),
  historicalReplayExportUrl:(replayId:string)=>`/api/v1/historical-replay/runs/${encodeURIComponent(replayId)}/export.csv`,
  generateBugReport:(body:BugReportInput)=>apiRequest<BugReport>('/api/v1/bugs/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  configReview:(signal?:AbortSignal,baselineVersion?:string,comparisonVersion?:string)=>{
    const query=new URLSearchParams({environment_id:'china_uat'});
    if(baselineVersion)query.set('baseline_version',baselineVersion);
    if(comparisonVersion)query.set('comparison_version',comparisonVersion);
    return apiRequest<ConfigReviewResult>(`/api/v1/config-review?${query}`,{signal});
  },
  generateExport:(name:string,mode:DataMode)=>apiRequest<ExportGenerateResult>('/api/v1/exports/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,mode,environment_id:activeEnvironmentFilter})}),
  search:(query:string,environmentId:EnvironmentFilter,sourceType?:string,limit=50,cursor?:string,signal?:AbortSignal)=>{
    const params=new URLSearchParams({q:query,environment_id:environmentId,limit:String(limit)});
    if(sourceType)params.set('source_type',sourceType);
    if(cursor)params.set('cursor',cursor);
    return apiRequest<SearchResponse>(`/api/v1/search?${params.toString()}`,{signal});
  },
};
