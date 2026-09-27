. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
Initialize-LocalRuntimeDirectories
Set-Content -LiteralPath $script:SupervisorPidFile -Value $PID -Encoding ASCII
$log=Join-Path $script:LogRoot "supervisor.log"
$recoveries=0
try {
  & (Join-Path $PSScriptRoot "start-local.ps1") *>> $log
  while ($true) {
    Start-Sleep -Seconds 10
    if (Test-ConsoleHealth -Path "/ready") { continue }
    if ($recoveries -ge 3) {
      "$(Get-Date -Format o) recovery_limit_reached" | Add-Content -LiteralPath $log
      exit 2
    }
    $recoveries++
    "$(Get-Date -Format o) recovery_attempt=$recoveries" | Add-Content -LiteralPath $log
    & (Join-Path $PSScriptRoot "restart-local.ps1") -KeepSupervisor *>> $log
  }
} finally {
  if (Test-Path -LiteralPath $script:SupervisorPidFile) { Remove-Item -Force -LiteralPath $script:SupervisorPidFile }
}
