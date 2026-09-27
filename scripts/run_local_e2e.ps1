param([ValidateRange(1, 65535)][int]$UnifiedPort = 5184)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$web = Join-Path $repo "web"
$python = "C:\Users\LX\anaconda3\python.exe"
$node = (Get-Command node -ErrorAction Stop).Source
$backend = $null

function Test-LocalPort([int]$Port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    if (-not $result.AsyncWaitHandle.WaitOne(250)) { return $false }
    $client.EndConnect($result)
    return $true
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

if (Test-LocalPort $UnifiedPort) { throw "Local E2E unified port $UnifiedPort is already in use." }
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath (Join-Path $web "node_modules\playwright\cli.js"))) {
  throw "Playwright is not installed under web/node_modules."
}

$testRoot = Join-Path $web "test-results"
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
$database = Join-Path $resolvedTestRoot "playwright.sqlite3"
$backendOut = Join-Path $resolvedTestRoot "backend.out.log"
$backendErr = Join-Path $resolvedTestRoot "backend.err.log"
$credentialDirectory = Join-Path $resolvedTestRoot ("credentials-" + [guid]::NewGuid().ToString("N"))
foreach ($path in @($database, "$database-shm", "$database-wal")) {
  if (-not $path.StartsWith($resolvedTestRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove a test database outside web/test-results."
  }
  if (Test-Path -LiteralPath $path) { Remove-Item -Force -LiteralPath $path }
}

$env:ROUTING_CONSOLE_DB = "web/test-results/playwright.sqlite3"
$env:WEIMETA_UAT_API_KEY = ""
$env:WEIMETA_REAL_EXECUTION_ENABLED = "true"
$env:WEIMETA_TEST_TRANSPORT = "1"
$env:ROUTING_CONSOLE_TEST_MODE = "1"
$env:UAT_FRONTEND_ORIGINS = "http://127.0.0.1:$UnifiedPort"
$env:ROUTING_CONSOLE_ALLOWED_ORIGINS = "http://127.0.0.1:$UnifiedPort"
$env:UAT_LOCAL_API_HOSTS = "127.0.0.1:$UnifiedPort,localhost:$UnifiedPort"
$env:ROUTING_CONSOLE_ALLOWED_HOSTS = "127.0.0.1:$UnifiedPort,localhost:$UnifiedPort"
$env:ROUTING_CONSOLE_FRONTEND_DIST = "web/dist"
$env:UAT_PERSISTENT_CREDENTIAL_DIRECTORY = $credentialDirectory
$env:PLAYWRIGHT_EXTERNAL_SERVERS = "1"
$ephemeralSigningKey = New-Object byte[] 48
$randomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
  $randomSource.GetBytes($ephemeralSigningKey)
} finally {
  $randomSource.Dispose()
}
$env:ROUTING_CONSOLE_ENTERPRISE_SIGNING_KEY = [Convert]::ToBase64String(
  $ephemeralSigningKey)

try {
  $backend = Start-Process -FilePath $python -ArgumentList @(
    "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1",
    "--port", $UnifiedPort
  ) -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
  $ready = $false
  foreach ($attempt in 1..120) {
    if ($backend.HasExited) {
      $backendFailure = if (Test-Path $backendErr) { Get-Content -Raw $backendErr } else { "" }
      throw "Local E2E service exited before readiness. Backend: $backendFailure"
    }
    try {
      $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
        -Uri "http://127.0.0.1:$UnifiedPort/ready"
      if ($response.StatusCode -eq 200) {
        $ready = $true
        break
      }
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  if (-not $ready) { throw "Local E2E services did not become ready." }

  Push-Location -LiteralPath $web
  try {
    & $node (Join-Path $web "node_modules\playwright\cli.js") "test" `
      "--config" (Join-Path $web "playwright.config.ts") "--reporter=line"
    $testExitCode = $LASTEXITCODE
  } finally {
    Pop-Location
  }
} finally {
  if ($null -ne $backend -and -not $backend.HasExited) {
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    $backend.WaitForExit(5000) | Out-Null
  }
}

if ($testExitCode -ne 0) { exit $testExitCode }
