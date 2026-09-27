$ErrorActionPreference = "Stop"
$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:PythonExe = if ($env:ICS_PYTHON_EXE -and (Test-Path -LiteralPath $env:ICS_PYTHON_EXE)) { $env:ICS_PYTHON_EXE } elseif (Test-Path -LiteralPath "C:\Users\LX\anaconda3\python.exe") { "C:\Users\LX\anaconda3\python.exe" } else { (Get-Command python.exe -ErrorAction Stop).Source }
$script:ConsoleHost = "127.0.0.1"
$script:ConsolePort = if ($env:ICS_LOCAL_CONSOLE_PORT -match '^\d+$') { [int]$env:ICS_LOCAL_CONSOLE_PORT } else { 5174 }
$script:ConsoleUrl = "http://$script:ConsoleHost`:$script:ConsolePort"
$script:AgentHostPort = 8010
$script:AgentHostUrl = "http://127.0.0.1:$script:AgentHostPort"
$script:StateRoot = if ($env:ICS_LOCAL_STATE_ROOT) { [IO.Path]::GetFullPath($env:ICS_LOCAL_STATE_ROOT) } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "IntelligentChannelScheduler" }
$script:RunRoot = Join-Path $script:StateRoot "run"
$script:LogRoot = Join-Path $script:StateRoot "logs"
$script:DataRoot = Join-Path $script:StateRoot "data"
$script:ConfigRoot = Join-Path $script:StateRoot "config"
$script:RuntimeConfig = Join-Path $script:ConfigRoot "local-runtime.json"
$script:BackendPidFile = Join-Path $script:RunRoot "backend.pid"
$script:WorkerPidFile = Join-Path $script:RunRoot "worker.pid"
$script:AgentHostPidFile = Join-Path $script:RunRoot "agent-host.pid"
$script:SupervisorPidFile = Join-Path $script:RunRoot "supervisor.pid"
$script:DatabasePath = Join-Path $script:DataRoot "routing_quality_console.sqlite3"

function Initialize-LocalRuntimeDirectories {
  foreach ($path in @($script:StateRoot,$script:RunRoot,$script:LogRoot,$script:DataRoot,$script:ConfigRoot)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
  }
  if (-not (Test-Path -LiteralPath $script:DatabasePath)) {
    $legacy = Join-Path $script:RepoRoot "data\routing_quality_console.sqlite3"
    if (Test-Path -LiteralPath $legacy) { Copy-Item -LiteralPath $legacy -Destination $script:DatabasePath }
  }
  $config = [ordered]@{
    schema_version = "local_runtime_v1"; repository = $script:RepoRoot
    host = $script:ConsoleHost; port = $script:ConsolePort; url = $script:ConsoleUrl
    agent_host_url = $script:AgentHostUrl
    database = $script:DatabasePath; state_directory = $script:StateRoot
    frontend_distribution = (Join-Path $script:RepoRoot "web\dist")
    domestic_uat_formal_chrome_enabled = $true
  }
  $temporary = "$script:RuntimeConfig.tmp"
  $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -Force -LiteralPath $temporary -Destination $script:RuntimeConfig
}

function Get-ManagedProcess([string]$PidFile,[string]$ExpectedMarker) {
  if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
  $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
  if ($raw -notmatch '^\d+$') { Remove-Item -Force -LiteralPath $PidFile; return $null }
  $record = Get-CimInstance Win32_Process -Filter "ProcessId=$raw" -ErrorAction SilentlyContinue
  if ($null -eq $record -or $record.CommandLine -notlike "*$ExpectedMarker*") {
    Remove-Item -Force -LiteralPath $PidFile
    return $null
  }
  return $record
}

function Test-ConsoleHealth([string]$Path = "/health",[int]$TimeoutSeconds = 2) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$script:ConsoleUrl$Path" -TimeoutSec $TimeoutSeconds
    return $response.StatusCode -eq 200
  } catch { return $false }
}

function Wait-ConsoleReady([int]$Attempts = 60,[int]$DelayMilliseconds = 500) {
  for ($index=0; $index -lt $Attempts; $index++) {
    if (Test-ConsoleHealth -Path "/ready") { return $true }
    Start-Sleep -Milliseconds $DelayMilliseconds
  }
  return $false
}

function Test-AgentHostHealth([string]$Path = "/health",[int]$TimeoutSeconds = 2) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$script:AgentHostUrl$Path" -TimeoutSec $TimeoutSeconds
    return $response.StatusCode -eq 200
  } catch { return $false }
}

function Wait-AgentHostReady([int]$Attempts = 60,[int]$DelayMilliseconds = 500) {
  for ($index=0; $index -lt $Attempts; $index++) {
    if (Test-AgentHostHealth -Path "/ready") { return $true }
    Start-Sleep -Milliseconds $DelayMilliseconds
  }
  return $false
}

function Get-PortOwner([int]$Port) {
  return Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
}
