param([switch]$OpenBrowser,[switch]$KeepSupervisor)
& (Join-Path $PSScriptRoot "stop-local.ps1") -KeepSupervisor:$KeepSupervisor
& (Join-Path $PSScriptRoot "start-local.ps1") -OpenBrowser:$OpenBrowser
exit $LASTEXITCODE
