param([switch]$StartNow)
$ErrorActionPreference="Stop"
. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
Initialize-LocalRuntimeDirectories
$taskName="IntelligentChannelScheduler.LocalConsole"
$powershell="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$supervisor=Join-Path $PSScriptRoot "autostart-supervisor.ps1"
$arguments="-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`""
$action=New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $script:RepoRoot
$trigger=New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal=New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 7) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Routing Quality Console same-origin local runtime." -Force | Out-Null

$shell=New-Object -ComObject WScript.Shell
$desktop=[Environment]::GetFolderPath("Desktop")
$openName="$([char]0x667A)$([char]0x80FD)$([char]0x6E20)$([char]0x9053)$([char]0x8C03)$([char]0x5EA6)$([char]0x63A7)$([char]0x5236)$([char]0x53F0).lnk"
$stopName="$([char]0x505C)$([char]0x6B62)$([char]0x667A)$([char]0x80FD)$([char]0x6E20)$([char]0x9053)$([char]0x8C03)$([char]0x5EA6)$([char]0x670D)$([char]0x52A1).lnk"
$openLink=$shell.CreateShortcut((Join-Path $desktop $openName))
$openLink.TargetPath=$powershell
$openLink.Arguments="-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'start-local.ps1')`" -OpenBrowser"
$openLink.WorkingDirectory=$script:RepoRoot
$openLink.Description="Start services and open $script:ConsoleUrl"
$openLink.Save()
$stopLink=$shell.CreateShortcut((Join-Path $desktop $stopName))
$stopLink.TargetPath=$powershell
$stopLink.Arguments="-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'stop-local.ps1')`""
$stopLink.WorkingDirectory=$script:RepoRoot
$stopLink.Description="Stop Routing Quality Console services"
$stopLink.Save()
Write-Host "Installed logon task: $taskName"
Write-Host "Canonical URL: $script:ConsoleUrl"
if ($StartNow) { Start-ScheduledTask -TaskName $taskName }
