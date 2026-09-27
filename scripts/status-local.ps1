. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
Initialize-LocalRuntimeDirectories
$backend = Get-ManagedProcess $script:BackendPidFile "backend.app:app"
$worker = Get-ManagedProcess $script:WorkerPidFile "backend.local_runtime_worker"
$agentHost = Get-ManagedProcess $script:AgentHostPidFile "external_agent_host.runner"
$result = [ordered]@{
  url=$script:ConsoleUrl; backend_pid=if($backend){$backend.ProcessId}else{$null}
  worker_pid=if($worker){$worker.ProcessId}else{$null}
  agent_host_pid=if($agentHost){$agentHost.ProcessId}else{$null}
  health=Test-ConsoleHealth; ready=Test-ConsoleHealth -Path "/ready"
  agent_host_health=Test-AgentHostHealth; agent_host_ready=Test-AgentHostHealth -Path "/ready"
  database=$script:DatabasePath; state_directory=$script:StateRoot
}
$result | ConvertTo-Json -Depth 5
if (-not $result.ready -or -not $result.agent_host_ready) { exit 1 }
