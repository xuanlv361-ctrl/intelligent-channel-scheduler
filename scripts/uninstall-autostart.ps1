param([switch]$KeepRunning)
$ErrorActionPreference="Stop"
. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
$taskName="IntelligentChannelScheduler.LocalConsole"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
$desktop=[Environment]::GetFolderPath("Desktop")
$openName="$([char]0x667A)$([char]0x80FD)$([char]0x6E20)$([char]0x9053)$([char]0x8C03)$([char]0x5EA6)$([char]0x63A7)$([char]0x5236)$([char]0x53F0).lnk"
$stopName="$([char]0x505C)$([char]0x6B62)$([char]0x667A)$([char]0x80FD)$([char]0x6E20)$([char]0x9053)$([char]0x8C03)$([char]0x5EA6)$([char]0x670D)$([char]0x52A1).lnk"
foreach($name in @($openName,$stopName)) {
  $path=Join-Path $desktop $name
  if(Test-Path -LiteralPath $path){Remove-Item -Force -LiteralPath $path}
}
if(-not $KeepRunning){& (Join-Path $PSScriptRoot "stop-local.ps1")}
Write-Host "Autostart removed. Persistent data was retained."
