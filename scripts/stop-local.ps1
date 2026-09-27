param([switch]$KeepSupervisor)
. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
$targets = @(
  @($script:BackendPidFile,"backend.app:app"),
  @($script:WorkerPidFile,"backend.local_runtime_worker"),
  @($script:AgentHostPidFile,"external_agent_host.runner")
)
if(-not $KeepSupervisor){$targets=@(@($script:SupervisorPidFile,"autostart-supervisor.ps1"))+$targets}
foreach ($target in $targets) {
  $process = Get-ManagedProcess $target[0] $target[1]
  if ($null -ne $process) {
    Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
  }
  if (Test-Path -LiteralPath $target[0]) { Remove-Item -Force -LiteralPath $target[0] }
}
Write-Host "Routing Quality Console, Worker and independent Agent Host stopped."
