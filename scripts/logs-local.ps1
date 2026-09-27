param([ValidateRange(1,500)][int]$Tail=100)
. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
foreach ($name in @("backend.error.log","backend.out.log","worker.error.log","worker.out.log","supervisor.log")) {
  $path=Join-Path $script:LogRoot $name
  Write-Host "`n=== $name ==="
  if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Tail $Tail } else { Write-Host "No log entries." }
}
