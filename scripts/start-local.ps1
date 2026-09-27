param([switch]$OpenBrowser,[switch]$RebuildFrontend)
. (Join-Path $PSScriptRoot "local-runtime-common.ps1")
Initialize-LocalRuntimeDirectories

$consoleAlreadyHealthy = Test-ConsoleHealth
if ($consoleAlreadyHealthy -and (Test-AgentHostHealth)) {
  Write-Host "Routing Quality Console is already running: $script:ConsoleUrl"
  if ($OpenBrowser) { Start-Process $script:ConsoleUrl }
  exit 0
}

$owner = Get-PortOwner $script:ConsolePort
if (-not $consoleAlreadyHealthy -and $null -ne $owner) {
  throw "Fixed port $script:ConsolePort is owned by an unhealthy process (PID $($owner.OwningProcess))."
}

$distIndex = Join-Path $script:RepoRoot "web\dist\index.html"
if ($RebuildFrontend -or -not (Test-Path -LiteralPath $distIndex)) {
  $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
  $build = Start-Process -FilePath $npm -ArgumentList "run","build" -WorkingDirectory (Join-Path $script:RepoRoot "web") -Wait -PassThru -WindowStyle Hidden
  if ($build.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $distIndex)) { throw "Frontend production build failed." }
}

$migrationStatus = Join-Path $script:RunRoot "migration-status.json"
& $script:PythonExe -m backend.local_runtime_preflight --database $script:DatabasePath --status-file $migrationStatus
if ($LASTEXITCODE -ne 0) { throw "Database migration failed; run scripts\logs-local.ps1." }

$env:ROUTING_CONSOLE_DATABASE_PATH = $script:DatabasePath
$env:ROUTING_CONSOLE_STATE_DIR = $script:StateRoot
$env:ROUTING_CONSOLE_FRONTEND_DIST = (Join-Path $script:RepoRoot "web\dist")
$env:ROUTING_CONSOLE_REQUIRE_WORKER = "true"
$env:ROUTING_CONSOLE_DEPLOYMENT_PROFILE = "development"
$env:ROUTING_CONSOLE_ALLOWED_HOSTS = "127.0.0.1:$script:ConsolePort,localhost:$script:ConsolePort"
$env:ROUTING_CONSOLE_ALLOWED_ORIGINS = "http://127.0.0.1:$script:ConsolePort,http://localhost:$script:ConsolePort"
$env:UAT_LOCAL_API_HOSTS = "127.0.0.1:$script:ConsolePort,localhost:$script:ConsolePort"
$env:UAT_PERSISTENT_SESSION_ENABLED = "true"
$env:UAT_PERSISTENT_USE_FORMAL_CHROME = "true"
$env:AGENT_SKILL_HOST_ALLOW_LOOPBACK_HTTP = "true"
$env:AGENT_SKILL_HOST_ALLOWED_HOSTS = "127.0.0.1"
$env:AGENT_SKILL_HOST_LOOPBACK_PORTS = "$script:AgentHostPort"

$agentHost = Get-ManagedProcess $script:AgentHostPidFile "external_agent_host.runner"
if ($null -eq $agentHost) {
  $owner = Get-PortOwner $script:AgentHostPort
  if ($null -ne $owner) { throw "Agent Host port $script:AgentHostPort is owned by unmanaged PID $($owner.OwningProcess)." }
  $hostOut = Join-Path $script:LogRoot "agent-host.out.log"
  $hostErr = Join-Path $script:LogRoot "agent-host.error.log"
  $process = Start-Process -FilePath $script:PythonExe -ArgumentList "-m","external_agent_host.runner" -WorkingDirectory $script:RepoRoot -WindowStyle Hidden -RedirectStandardOutput $hostOut -RedirectStandardError $hostErr -PassThru
  Set-Content -LiteralPath $script:AgentHostPidFile -Value $process.Id -Encoding ASCII
}
if (-not (Wait-AgentHostReady)) { throw "Independent Agent Host did not become ready." }

$worker = Get-ManagedProcess $script:WorkerPidFile "backend.local_runtime_worker"
if ($null -eq $worker) {
  $workerOut = Join-Path $script:LogRoot "worker.out.log"
  $workerErr = Join-Path $script:LogRoot "worker.error.log"
  $process = Start-Process -FilePath $script:PythonExe -ArgumentList "-m","backend.local_runtime_worker","--state-dir",$script:StateRoot -WorkingDirectory $script:RepoRoot -WindowStyle Hidden -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -PassThru
  Set-Content -LiteralPath $script:WorkerPidFile -Value $process.Id -Encoding ASCII
}

$backend = Get-ManagedProcess $script:BackendPidFile "backend.app:app"
if ($null -eq $backend) {
  $backendOut = Join-Path $script:LogRoot "backend.out.log"
  $backendErr = Join-Path $script:LogRoot "backend.error.log"
  $process = Start-Process -FilePath $script:PythonExe -ArgumentList "-m","uvicorn","backend.app:app","--app-dir",$script:RepoRoot,"--host",$script:ConsoleHost,"--port",$script:ConsolePort -WorkingDirectory $script:RepoRoot -WindowStyle Hidden -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru
  Set-Content -LiteralPath $script:BackendPidFile -Value $process.Id -Encoding ASCII
}

if (-not (Wait-ConsoleReady)) {
  throw "Local backend did not become ready; run scripts\logs-local.ps1."
}
Write-Host "Routing Quality Console is ready: $script:ConsoleUrl"
Write-Host "Independent Agent Host is ready: $script:AgentHostUrl"
if ($OpenBrowser) { Start-Process $script:ConsoleUrl }
