[CmdletBinding(DefaultParameterSetName = "DryRun")]
param(
    [Parameter(ParameterSetName = "DryRun", Mandatory = $true)]
    [Parameter(ParameterSetName = "Execute", Mandatory = $true)]
    [ValidateNotNullOrEmpty()][string]$SessionId,
    [ValidateRange(1, 20)][int]$MaxRequests = 20,
    [ValidateRange(1, 86400)][int]$DelaySeconds = 3,
    [Parameter(ParameterSetName = "DryRun")][switch]$DryRun,
    [Parameter(ParameterSetName = "Execute", Mandatory = $true)][switch]$ConfirmExecute,
    [Parameter(ParameterSetName = "SelfTest", Mandatory = $true)][switch]$SelfTestTransport,
    [ValidateNotNullOrEmpty()][string]$OutputDirectory = "output",
    [switch]$Amend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Endpoint = [Uri]"https://api-uat.weimeta.cn/v1/chat/completions"
$Root = Split-Path -Parent $PSScriptRoot
$PlanPath = Join-Path $Root "output/unified_routing_plan_v3.csv"
$PayloadPath = Join-Path $Root "output/unified_routing_payloads_v3.json"
$ResolvedOutput = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $Root $OutputDirectory))
}
$ResultPath = Join-Path $ResolvedOutput "unified_uat_execution_v3.jsonl"
$EvidenceRoot = if ($env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and
    -not [string]::IsNullOrWhiteSpace($env:WEIMETA_UAT_TEST_EVIDENCE_ROOT)) {
    [IO.Path]::GetFullPath($env:WEIMETA_UAT_TEST_EVIDENCE_ROOT)
} else {
    Join-Path $Root "evidence/unified_uat_v3"
}
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

function Assert-Endpoint {
    param([Uri]$Uri)
    if ($Uri.Scheme -cne "https" -or
        $Uri.Host -cne "api-uat.weimeta.cn" -or
        $Uri.AbsolutePath -cne "/v1/chat/completions" -or
        -not [string]::IsNullOrEmpty($Uri.Query) -or
        -not $Uri.IsDefaultPort) {
        throw "endpoint_not_allowed"
    }
}

function Write-Utf8Atomic {
    param([string]$Path, [string]$Content)
    $parent = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temp = Join-Path $parent (".{0}.{1}.tmp" -f ([IO.Path]::GetFileName($Path)), [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText($temp, $Content, $Utf8NoBom)
        if (Test-Path -LiteralPath $Path) {
            $replaceBackup = Join-Path $parent (".replace-backup.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
            try {
                [IO.File]::Replace($temp, $Path, $replaceBackup)
            } finally {
                if (Test-Path -LiteralPath $replaceBackup) { Remove-Item -LiteralPath $replaceBackup -Force }
            }
        } else {
            [IO.File]::Move($temp, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

function Get-Sha256Text {
    param([AllowEmptyString()][string]$Text)
    $bytes = $Utf8NoBom.GetBytes($Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-SafeText {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return "" }
    $safe = $Text -replace '(?i)Bearer\s+[A-Za-z0-9._~+/\-=]+', '[REDACTED_BEARER]'
    $safe = $safe -replace '(?i)(api[_-]?key|authorization|cookie|password)\s*[:=]\s*[^\s,;"}]+', '$1=[REDACTED]'
    return $safe
}

function Get-SafeTransportMessage {
    param(
        [AllowNull()][string]$Message,
        [AllowNull()][string]$ApiKey
    )
    $safe = Get-SafeText $Message
    if (-not [string]::IsNullOrEmpty($ApiKey)) {
        $safe = $safe.Replace($ApiKey, "[REDACTED_KEY]")
    }
    $safe = $safe -replace '(?i)(set-cookie|cookie|authorization)\s*:\s*[^\r\n]+', '$1: [REDACTED]'
    $safe = $safe -replace '[\r\n]+', ' '
    $safe = $safe.Trim()
    if ($safe.Length -gt 500) { $safe = $safe.Substring(0, 500) }
    return $safe
}

function New-TransportException {
    param(
        [ValidateSet("assembly_load", "key_validation", "request_construction", "dns_or_connect",
            "tls_handshake", "send_request", "read_headers", "read_body", "parse_response")]
        [string]$Stage,
        [string]$Code,
        [System.Exception]$Exception,
        [bool]$NetworkCalled,
        [AllowNull()][string]$ApiKey
    )
    $safeMessage = Get-SafeTransportMessage $Exception.Message $ApiKey
    $wrapped = New-Object System.InvalidOperationException($safeMessage, $Exception)
    $wrapped.Data["transport_stage"] = $Stage
    $wrapped.Data["transport_exception_type"] = $Exception.GetType().FullName
    $wrapped.Data["transport_error_code"] = $Code
    $wrapped.Data["safe_transport_message"] = $safeMessage
    $wrapped.Data["network_called"] = $NetworkCalled
    return $wrapped
}

function Initialize-SystemNetHttp {
    try {
        if ($env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and
            $env:WEIMETA_UAT_TEST_FORCE_ASSEMBLY_FAILURE -eq "1") {
            throw (New-Object System.IO.FileNotFoundException("System.Net.Http test assembly load failure"))
        }
        Add-Type -AssemblyName System.Net.Http
        if ($env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and
            $env:WEIMETA_UAT_TEST_FORCE_SEND_FAILURE -eq "1" -and
            $null -eq ("WeimetaLocalThrowingHandler" -as [type])) {
            Add-Type -ReferencedAssemblies ([System.Net.Http.HttpClient].Assembly.Location) -TypeDefinition @'
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
public sealed class WeimetaLocalThrowingHandler : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        throw new HttpRequestException("Local mock SendAsync failure.");
    }
}
'@
        }
    } catch {
        throw (New-TransportException "assembly_load" "system_net_http_load_failed" $_.Exception $false $null)
    }
}

function Test-AndNormalizeApiKey {
    param([AllowNull()][string]$ApiKey)
    try {
        if ([string]::IsNullOrWhiteSpace($ApiKey)) { throw "api_key_missing" }
        if ($ApiKey.Contains("`r") -or $ApiKey.Contains("`n")) { throw "api_key_contains_crlf" }
        $normalized = $ApiKey.Trim()
        if ([string]::IsNullOrEmpty($normalized)) { throw "api_key_missing" }
        foreach ($character in $normalized.ToCharArray()) {
            $code = [int][char]$character
            if ($code -lt 33 -or $code -gt 126) { throw "api_key_not_printable_ascii" }
        }
        return $normalized
    } catch {
        throw (New-TransportException "key_validation" "invalid_api_key" $_.Exception $false $ApiKey)
    }
}

function Get-HeaderValue {
    param($Headers, [string[]]$Names)
    foreach ($name in $Names) {
        if ($null -ne $Headers -and $Headers.Contains($name)) {
            $values = $Headers.GetValues($name)
            if ($values) { return ($values -join ",") }
        }
    }
    return $null
}

function Convert-ToNullableInt {
    param($Value)
    if ($null -eq $Value -or "$Value" -eq "") { return $null }
    $parsed = 0L
    if (-not [long]::TryParse("$Value", [ref]$parsed) -or $parsed -lt 0) { throw "token_or_cost_anomaly" }
    return $parsed
}

function Convert-ToNullableCost {
    param($Value)
    if ($null -eq $Value -or "$Value" -eq "") { return $null }
    $parsed = 0D
    if (-not [decimal]::TryParse(
        "$Value", [Globalization.NumberStyles]::Any,
        [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed
    ) -or $parsed -lt 0) { throw "token_or_cost_anomaly" }
    return $parsed
}

function Get-ObservedCost {
    param($Object)
    if ($null -eq $Object) { return $null }
    if ($Object.PSObject.Properties.Name -contains "cost") {
        return Convert-ToNullableCost $Object.cost
    }
    if ($Object.PSObject.Properties.Name -contains "cost_cny") {
        return Convert-ToNullableCost $Object.cost_cny
    }
    return $null
}

function Get-OptionalProperty {
    param($Object, [string]$Name)
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) {
        return $Object.$Name
    }
    return $null
}

function Get-ExistingRecords {
    if (-not (Test-Path -LiteralPath $ResultPath)) { return @() }
    $records = @()
    foreach ($line in [IO.File]::ReadAllLines($ResultPath, $Utf8NoBom)) {
        if (-not [string]::IsNullOrWhiteSpace($line)) { $records += ($line | ConvertFrom-Json) }
    }
    return @($records)
}

function Save-Record {
    param([pscustomobject]$Record)
    $records = @(Get-ExistingRecords)
    $matching = @($records | Where-Object { $_.plan_id -eq $Record.plan_id })
    if ($matching.Count -gt 0 -and -not $Amend) { throw "duplicate_plan_id" }
    if ($matching.Count -gt 0) {
        $backupDir = Join-Path $ResolvedOutput "backups"
        [IO.Directory]::CreateDirectory($backupDir) | Out-Null
        $stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
        Copy-Item -LiteralPath $ResultPath -Destination (Join-Path $backupDir "unified_uat_execution_v3_before_amend_$stamp.jsonl")
        $records = @($records | Where-Object { $_.plan_id -ne $Record.plan_id })
    }
    $records += $Record
    $lines = @($records | ForEach-Object { $_ | ConvertTo-Json -Depth 20 -Compress })
    Write-Utf8Atomic -Path $ResultPath -Content (($lines -join "`n") + "`n")
}

function Save-Evidence {
    param($Payload, [pscustomobject]$Record, [string]$Body)
    $directory = Join-Path $EvidenceRoot $Record.execution_id
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $messageMetadata = @($Payload.messages | ForEach-Object {
        $content = [string]$_.content
        [ordered]@{ role = $_.role; content_length = $content.Length; content_sha256 = Get-Sha256Text $content }
    })
    $requestMetadata = [ordered]@{
        endpoint = $Endpoint.AbsoluteUri; plan_id = $Record.plan_id; execution_id = $Record.execution_id
        session_id = $Record.session_id; request_profile_id = $Record.request_profile_id
        requested_model = $Record.requested_model; stream = $Record.stream; max_tokens = $Record.max_tokens
        message_count = $messageMetadata.Count; messages = $messageMetadata
        sensitive_prompt_saved = $false; authorization_saved = $false
    }
    $responseMetadata = [ordered]@{
        http_status = $Record.http_status; content_type = $Record.content_type
        latency_ms = $Record.latency_ms; ttft_ms = $Record.ttft_ms
        response_id = $Record.response_id; request_id = $Record.request_id
        actual_model = $Record.actual_model; input_tokens = $Record.input_tokens
        output_tokens = $Record.output_tokens; total_tokens = $Record.total_tokens
        known_cost = $Record.known_cost; finish_reason = $Record.finish_reason; sse_complete = $Record.sse_complete
        done_received = $Record.done_received; response_body_sha256 = $Record.response_body_sha256
        network_called = $Record.network_called; transport_stage = $Record.transport_stage
        transport_exception_type = $Record.transport_exception_type
        transport_error_code = $Record.transport_error_code
        safe_transport_message = $Record.safe_transport_message
    }
    $bodyName = if ($Record.stream) { "response_sse.txt" } else { "response_body.txt" }
    Write-Utf8Atomic (Join-Path $directory "request_metadata.json") (($requestMetadata | ConvertTo-Json -Depth 10) + "`n")
    Write-Utf8Atomic (Join-Path $directory "response_metadata.json") (($responseMetadata | ConvertTo-Json -Depth 10) + "`n")
    Write-Utf8Atomic (Join-Path $directory $bodyName) ((Get-SafeText $Body) + "`n")
    $files = @("request_metadata.json", "response_metadata.json", $bodyName)
    $manifest = [ordered]@{
        execution_id = $Record.execution_id; plan_id = $Record.plan_id
        created_at = [DateTimeOffset]::UtcNow.ToString("o"); source_type = "measured_unified_uat"
        is_mock = $false; actual_channel = $null; actual_channel_status = "pending_platform_log_correlation"
        scheduler_recommendation_used_as_actual_channel = $false
        files = @($files | ForEach-Object {
            $path = Join-Path $directory $_
            [ordered]@{ name = $_; sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
        })
    }
    Write-Utf8Atomic (Join-Path $directory "evidence_manifest.json") (($manifest | ConvertTo-Json -Depth 10) + "`n")
}

function Invoke-MockTransport {
    param($Payload)
    if ($env:ROUTING_CONSOLE_TEST_MODE -ne "1" -or [string]::IsNullOrWhiteSpace($env:WEIMETA_UAT_TEST_TRANSPORT_FILE)) {
        throw "mock_transport_not_authorized"
    }
    $mockPath = [IO.Path]::GetFullPath($env:WEIMETA_UAT_TEST_TRANSPORT_FILE)
    $spec = Get-Content -LiteralPath $mockPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $item = @($spec | Where-Object { $_.plan_id -eq $Payload.plan_id }) | Select-Object -First 1
    if ($null -eq $item) { throw "mock_response_missing" }
    return [pscustomobject]@{
        status = [int]$item.status; content_type = [string]$item.content_type
        request_id = $item.request_id; body = [string]$item.body
        latency_ms = if ($null -ne $item.latency_ms) { [double]$item.latency_ms } else { 1.0 }
        ttft_ms = if ($null -ne $item.ttft_ms) { [double]$item.ttft_ms } else { $null }
        network_called = $false; transport_stage = $null; transport_exception_type = $null
        transport_error_code = $null; safe_transport_message = $null
    }
}

function Invoke-UatTransport {
    param($Payload, [string]$ApiKey)
    Assert-Endpoint $Endpoint
    Initialize-SystemNetHttp
    $normalizedKey = Test-AndNormalizeApiKey $ApiKey
    $handler = $null; $client = $null; $request = $null; $content = $null; $response = $null
    $networkCalled = $false
    try {
        try {
            if ($env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and
                $env:WEIMETA_UAT_TEST_FORCE_CONSTRUCTION_FAILURE -eq "1") {
                throw (New-Object -TypeName System.InvalidOperationException -ArgumentList "request construction test failure")
            }
            $handler = if ($env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and
                $env:WEIMETA_UAT_TEST_FORCE_SEND_FAILURE -eq "1") {
                New-Object -TypeName WeimetaLocalThrowingHandler
            } else {
                New-Object -TypeName System.Net.Http.HttpClientHandler
            }
            $client = New-Object -TypeName System.Net.Http.HttpClient -ArgumentList (,$handler)
            $request = New-Object -TypeName System.Net.Http.HttpRequestMessage -ArgumentList @(
                [System.Net.Http.HttpMethod]::Post,
                $Endpoint
            )
            $bodyObject = [ordered]@{
                model = [string]$(if ($Payload.requested_model) { $Payload.requested_model } else { $Payload.model })
                messages = $Payload.messages; stream = [bool]$Payload.stream; max_tokens = [int]$Payload.max_tokens
            }
            $jsonBody = $bodyObject | ConvertTo-Json -Depth 20 -Compress
            $content = New-Object -TypeName System.Net.Http.StringContent -ArgumentList @(
                $jsonBody,
                [System.Text.Encoding]::UTF8,
                "application/json"
            )
            $request.Content = $content
            $request.Headers.Authorization = New-Object -TypeName System.Net.Http.Headers.AuthenticationHeaderValue -ArgumentList @(
                "Bearer",
                $normalizedKey
            )
        } catch {
            if ($_.Exception.Data.Contains("transport_stage")) { throw }
            throw (New-TransportException "request_construction" "http_request_construction_failed" $_.Exception $false $normalizedKey)
        }

        $clock = [Diagnostics.Stopwatch]::StartNew()
        try {
            $networkCalled = $true
            $response = $client.SendAsync(
                $request,
                [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
        } catch {
            $stage = "send_request"
            $code = "send_async_failed"
            $inner = $_.Exception
            while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
            if ($inner -is [System.Net.WebException]) {
                if ($inner.Status -in @(
                    [System.Net.WebExceptionStatus]::NameResolutionFailure,
                    [System.Net.WebExceptionStatus]::ConnectFailure,
                    [System.Net.WebExceptionStatus]::ProxyNameResolutionFailure
                )) { $stage = "dns_or_connect"; $code = "dns_or_connect_failed" }
                elseif ($inner.Status -eq [System.Net.WebExceptionStatus]::TrustFailure) {
                    $stage = "tls_handshake"; $code = "tls_handshake_failed"
                }
            }
            throw (New-TransportException $stage $code $_.Exception $networkCalled $normalizedKey)
        }

        try {
            $contentType = if ($response.Content.Headers.ContentType) {
                $response.Content.Headers.ContentType.ToString()
            } else { $null }
            $requestId = Get-HeaderValue $response.Headers @("x-request-id", "request-id", "x-weimeta-request-id")
            $httpStatus = [int]$response.StatusCode
        } catch {
            throw (New-TransportException "read_headers" "response_headers_read_failed" $_.Exception $networkCalled $normalizedKey)
        }

        try {
            $ttft = $null
            if ([bool]$Payload.stream) {
                $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                $reader = New-Object -TypeName System.IO.StreamReader -ArgumentList @($stream, [System.Text.Encoding]::UTF8)
                $builder = New-Object System.Text.StringBuilder
                try {
                    while (-not $reader.EndOfStream) {
                        $line = $reader.ReadLine()
                        if ($null -eq $ttft -and $line.StartsWith("data:") -and $line -ne "data: [DONE]") {
                            $ttft = $clock.Elapsed.TotalMilliseconds
                        }
                        [void]$builder.AppendLine($line)
                    }
                    $body = $builder.ToString()
                } finally {
                    $reader.Dispose()
                }
            } else {
                $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            }
        } catch {
            throw (New-TransportException "read_body" "response_body_read_failed" $_.Exception $networkCalled $normalizedKey)
        }
        $clock.Stop()
        return [pscustomobject]@{
            status = $httpStatus; content_type = $contentType; request_id = $requestId
            body = $body; latency_ms = $clock.Elapsed.TotalMilliseconds; ttft_ms = $ttft
            network_called = $networkCalled; transport_stage = $null; transport_exception_type = $null
            transport_error_code = $null; safe_transport_message = $null
        }
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        elseif ($null -ne $content) { $content.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}

function Invoke-TransportSelfTest {
    $handler = $null; $client = $null; $request = $null; $content = $null
    $testKey = "SELFTEST_PRINTABLE_ASCII_TOKEN"
    try {
        Initialize-SystemNetHttp
        $normalizedKey = Test-AndNormalizeApiKey $testKey
        Assert-Endpoint $Endpoint
        $handler = New-Object -TypeName System.Net.Http.HttpClientHandler
        $client = New-Object -TypeName System.Net.Http.HttpClient -ArgumentList (,$handler)
        $request = New-Object -TypeName System.Net.Http.HttpRequestMessage -ArgumentList @(
            [System.Net.Http.HttpMethod]::Post,
            $Endpoint
        )
        $jsonBody = ([ordered]@{
            model = "self-test-model"
            messages = @([ordered]@{ role = "user"; content = "self-test" })
            stream = $false
            max_tokens = 1
        } | ConvertTo-Json -Depth 20 -Compress)
        $content = New-Object -TypeName System.Net.Http.StringContent -ArgumentList @(
            $jsonBody,
            [System.Text.Encoding]::UTF8,
            "application/json"
        )
        $request.Content = $content
        $request.Headers.Authorization = New-Object -TypeName System.Net.Http.Headers.AuthenticationHeaderValue -ArgumentList @(
            "Bearer",
            $normalizedKey
        )
        $safe = Get-SafeTransportMessage "Authorization: Bearer $normalizedKey; Cookie: session=secret" $normalizedKey
        if ($safe.Contains($normalizedKey) -or $safe -match "session=secret") { throw "redaction_self_test_failed" }
        if ($request.Method -ne [System.Net.Http.HttpMethod]::Post -or
            $request.RequestUri.AbsoluteUri -ne $Endpoint.AbsoluteUri -or
            $request.Headers.Authorization.Scheme -ne "Bearer" -or
            $request.Content.Headers.ContentType.MediaType -ne "application/json") {
            throw "request_construction_self_test_failed"
        }
        return [pscustomobject]@{
            status = "passed"; assembly = "System.Net.Http"; request_construction = "passed"
            redaction = "passed"; network_called = $false; result_written = $false
        }
    } catch {
        if ($_.Exception.Data.Contains("transport_stage")) { throw }
        throw (New-TransportException "request_construction" "transport_self_test_failed" $_.Exception $false $testKey)
    } finally {
        if ($null -ne $request) { $request.Dispose() }
        elseif ($null -ne $content) { $content.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}

function Parse-ObservedResponse {
    param($Payload, $Transport)
    $actualModel = $null; $responseId = $null; $inputTokens = $null; $outputTokens = $null
    $totalTokens = $null; $knownCost = $null; $finishReason = $null; $sseComplete = $null; $doneReceived = $null
    $parseError = $null; $body = [string]$Transport.body
    try {
        if ([bool]$Payload.stream) {
            $doneReceived = [bool]($body -match '(?m)^data:\s*\[DONE\]\s*$')
            $sseComplete = $doneReceived
            foreach ($line in ($body -split "`r?`n")) {
                if (-not $line.StartsWith("data:")) { continue }
                $data = $line.Substring(5).Trim()
                if (-not $data -or $data -eq "[DONE]") { continue }
                $chunk = $data | ConvertFrom-Json
                if ($chunk.PSObject.Properties.Name -contains "id" -and $chunk.id) { $responseId = [string]$chunk.id }
                if ($chunk.PSObject.Properties.Name -contains "model" -and $chunk.model) { $actualModel = [string]$chunk.model }
                if ($chunk.PSObject.Properties.Name -contains "choices" -and $chunk.choices -and
                    $chunk.choices[0].PSObject.Properties.Name -contains "finish_reason" -and
                    $chunk.choices[0].finish_reason) { $finishReason = [string]$chunk.choices[0].finish_reason }
                if ($chunk.PSObject.Properties.Name -contains "usage" -and $chunk.usage) {
                    $inputTokens = Convert-ToNullableInt (Get-OptionalProperty $chunk.usage "prompt_tokens")
                    $outputTokens = Convert-ToNullableInt (Get-OptionalProperty $chunk.usage "completion_tokens")
                    $totalTokens = Convert-ToNullableInt (Get-OptionalProperty $chunk.usage "total_tokens")
                    $observedUsageCost = Get-ObservedCost $chunk.usage
                    if ($null -ne $observedUsageCost) { $knownCost = $observedUsageCost }
                }
                $observedChunkCost = Get-ObservedCost $chunk
                if ($null -ne $observedChunkCost) { $knownCost = $observedChunkCost }
            }
        } else {
            $json = $body | ConvertFrom-Json
            if ((Get-OptionalProperty $json "id")) { $responseId = [string]$json.id }
            if ((Get-OptionalProperty $json "model")) { $actualModel = [string]$json.model }
            if ((Get-OptionalProperty $json "choices") -and (Get-OptionalProperty $json.choices[0] "finish_reason")) { $finishReason = [string]$json.choices[0].finish_reason }
            if ((Get-OptionalProperty $json "usage")) {
                $inputTokens = Convert-ToNullableInt (Get-OptionalProperty $json.usage "prompt_tokens")
                $outputTokens = Convert-ToNullableInt (Get-OptionalProperty $json.usage "completion_tokens")
                $totalTokens = Convert-ToNullableInt (Get-OptionalProperty $json.usage "total_tokens")
                $knownCost = Get-ObservedCost $json.usage
            }
            $topLevelCost = Get-ObservedCost $json
            if ($null -ne $topLevelCost) { $knownCost = $topLevelCost }
        }
        if ($null -ne $totalTokens -and $null -ne $inputTokens -and $null -ne $outputTokens -and
            $totalTokens -ne ($inputTokens + $outputTokens)) { throw "token_or_cost_anomaly" }
        if ($null -ne $outputTokens -and $outputTokens -gt [int]$Payload.max_tokens) { throw "token_or_cost_anomaly" }
    } catch {
        if ($_.Exception.Message -eq "token_or_cost_anomaly") { throw }
        $parseError = "response_parse_failed"
    }
    return [pscustomobject]@{
        actual_model = $actualModel; response_id = $responseId; input_tokens = $inputTokens
        output_tokens = $outputTokens; total_tokens = $totalTokens; finish_reason = $finishReason
        known_cost = $knownCost; sse_complete = $sseComplete; done_received = $doneReceived; parse_error = $parseError
    }
}

if ($SelfTestTransport) {
    try {
        Invoke-TransportSelfTest | ConvertTo-Json -Depth 10
        exit 0
    } catch {
        $diagnostic = [ordered]@{
            status = "failed"
            transport_stage = if ($_.Exception.Data.Contains("transport_stage")) { $_.Exception.Data["transport_stage"] } else { "request_construction" }
            transport_exception_type = if ($_.Exception.Data.Contains("transport_exception_type")) { $_.Exception.Data["transport_exception_type"] } else { $_.Exception.GetType().FullName }
            transport_error_code = if ($_.Exception.Data.Contains("transport_error_code")) { $_.Exception.Data["transport_error_code"] } else { "transport_self_test_failed" }
            safe_transport_message = Get-SafeTransportMessage $_.Exception.Message $null
            network_called = $false
            result_written = $false
        }
        $diagnostic | ConvertTo-Json -Depth 10
        exit 2
    }
}

Assert-Endpoint $Endpoint
if (-not (Test-Path -LiteralPath $PlanPath) -or -not (Test-Path -LiteralPath $PayloadPath)) {
    throw "unified_routing_v3_inputs_missing"
}
$plans = @(Import-Csv -LiteralPath $PlanPath -Encoding UTF8)
$payloads = Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
$planIds = @{}; foreach ($plan in $plans) { $planIds[$plan.plan_id] = $true }
if ($payloads.Count -ne 60 -or $plans.Count -ne 60) { throw "unified_routing_v3_expected_60_rows" }
foreach ($payload in $payloads) {
    if (-not $planIds.ContainsKey($payload.plan_id)) { throw "payload_plan_mismatch" }
    if ($payload.session_id -ne $SessionId) { continue }
    if ($payload.request_profile_id -eq "P04" -and -not [bool]$payload.stream) { throw "p04_stream_must_be_true" }
    if ($payload.request_profile_id -ne "P04" -and [bool]$payload.stream) { throw "unexpected_stream_profile" }
    if ($payload.endpoint -and [Uri]$payload.endpoint -ne $Endpoint) { throw "endpoint_not_allowed" }
}

$existing = @(Get-ExistingRecords)
$completed = @{}; foreach ($item in $existing) { $completed[$item.plan_id] = $true }
$sessionPayloads = @($payloads | Where-Object { $_.session_id -eq $SessionId } | Sort-Object plan_id)
if ($sessionPayloads.Count -eq 0) { throw "session_not_found" }
$skipped = @($sessionPayloads | Where-Object { $completed.ContainsKey($_.plan_id) -and -not $Amend }).Count
$selected = @($sessionPayloads | Where-Object { -not $completed.ContainsKey($_.plan_id) -or $Amend } | Select-Object -First $MaxRequests)
$profileDistribution = @{}; $streamDistribution = @{}
foreach ($payload in $selected) {
    $profileDistribution[$payload.request_profile_id] = 1 + [int]($profileDistribution[$payload.request_profile_id])
    $streamKey = ([bool]$payload.stream).ToString().ToLowerInvariant()
    $streamDistribution[$streamKey] = 1 + [int]($streamDistribution[$streamKey])
}
$preview = [ordered]@{
    environment = "china_uat"; endpoint = $Endpoint.AbsoluteUri; session = $SessionId
    request_count = $selected.Count; profile_distribution = $profileDistribution
    stream_distribution = $streamDistribution
    maximum_max_tokens = if ($selected.Count) { ($selected | Measure-Object max_tokens -Maximum).Maximum } else { 0 }
    estimated_request_upper_bound = [Math]::Min(20, $MaxRequests)
    delay_seconds = $DelaySeconds; output_directory = $ResolvedOutput
    mode = if ($ConfirmExecute) { "execute" } else { "dry_run" }
}
$preview | ConvertTo-Json -Depth 10

if (-not $ConfirmExecute) {
    [pscustomobject]@{
        planned = $selected.Count; attempted = 0; succeeded = 0; failed = 0; skipped = $skipped
        http_status_distribution = @{}; profile_distribution = $profileDistribution
        stream_success_failed = @{}; average_latency_ms = $null; p50_latency_ms = $null
        p95_latency_ms = $null; total_tokens = 0; known_cost = $null
        stop_reason = "dry_run"; network_called = $false
    } | ConvertTo-Json -Depth 10
    exit 0
}

$apiKey = [Environment]::GetEnvironmentVariable("WEIMETA_CHINA_UAT_API_KEY", "Process")
$useMock = $env:ROUTING_CONSOLE_TEST_MODE -eq "1" -and -not [string]::IsNullOrWhiteSpace($env:WEIMETA_UAT_TEST_TRANSPORT_FILE)
[IO.Directory]::CreateDirectory($ResolvedOutput) | Out-Null
[IO.Directory]::CreateDirectory($EvidenceRoot) | Out-Null

$attempted = 0; $succeeded = 0; $failed = 0; $consecutiveFailures = 0; $stopReason = "completed"
$statuses = @{}; $streamResults = @{}; $latencies = New-Object Collections.Generic.List[double]
$tokenTotal = 0L; $knownCostTotal = 0D; $knownCostCount = 0; $attemptedProfiles = @{}
$anyNetworkCalled = $false
foreach ($payload in $selected) {
    if ($attempted -gt 0) { Start-Sleep -Seconds $DelaySeconds }
    $attempted++; $attemptedProfiles[$payload.request_profile_id] = 1 + [int]($attemptedProfiles[$payload.request_profile_id])
    $started = [DateTimeOffset]::UtcNow
    $executionId = "UAT-V3-{0}-{1}" -f $started.ToString("yyyyMMddTHHmmssfffZ"), ([Guid]::NewGuid().ToString("N").Substring(0, 10))
    $transport = $null; $observed = $null; $errorCategory = $null; $errorSummary = $null
    $transportStage = $null; $transportExceptionType = $null
    $transportErrorCode = $null; $safeTransportMessage = $null
    try {
        $transport = if ($useMock) { Invoke-MockTransport $payload } else { Invoke-UatTransport $payload $apiKey }
        $observed = Parse-ObservedResponse $payload $transport
        if ($transport.status -eq 401) { $errorCategory = "uat_authentication_failed" }
        elseif ($transport.status -eq 429) { $errorCategory = "rate_limited" }
        elseif ($transport.status -ge 500) { $errorCategory = "upstream_server_error" }
        elseif ($transport.status -lt 200 -or $transport.status -ge 300) { $errorCategory = "http_error" }
        elseif ($observed.parse_error) {
            $errorCategory = $observed.parse_error
            $transportStage = "parse_response"
            $transportErrorCode = "response_parse_failed"
            $safeTransportMessage = "Response metadata could not be parsed; response body is retained only in redacted evidence."
        }
    } catch {
        $message = Get-SafeTransportMessage $_.Exception.Message $apiKey
        $errorCategory = if ($message -eq "token_or_cost_anomaly") { $message } else { "transport_error" }
        $transportStage = if ($_.Exception.Data.Contains("transport_stage")) { $_.Exception.Data["transport_stage"] } else { "parse_response" }
        $transportExceptionType = if ($_.Exception.Data.Contains("transport_exception_type")) { $_.Exception.Data["transport_exception_type"] } else { $_.Exception.GetType().FullName }
        $transportErrorCode = if ($_.Exception.Data.Contains("transport_error_code")) { $_.Exception.Data["transport_error_code"] } else { $errorCategory }
        $safeTransportMessage = if ($_.Exception.Data.Contains("safe_transport_message")) {
            Get-SafeTransportMessage $_.Exception.Data["safe_transport_message"] $apiKey
        } else { $message }
        $errorSummary = $safeTransportMessage
        if ($null -eq $transport) {
            $networkCalled = if ($_.Exception.Data.Contains("network_called")) { [bool]$_.Exception.Data["network_called"] } else { $false }
            $transport = [pscustomobject]@{
                status = $null; content_type = $null; request_id = $null; body = ""
                latency_ms = $null; ttft_ms = $null; network_called = $networkCalled
                transport_stage = $transportStage; transport_exception_type = $transportExceptionType
                transport_error_code = $transportErrorCode; safe_transport_message = $safeTransportMessage
            }
        }
        $observed = [pscustomobject]@{ actual_model=$null;response_id=$null;input_tokens=$null;output_tokens=$null;total_tokens=$null;known_cost=$null;finish_reason=$null;sse_complete=$null;done_received=$null;parse_error=$null }
    }
    if ([bool]$transport.network_called) { $anyNetworkCalled = $true }
    $completedAt = [DateTimeOffset]::UtcNow
    $success = $null -eq $errorCategory
    if ($success) { $succeeded++; $consecutiveFailures = 0 } else { $failed++; $consecutiveFailures++ }
    $statusKey = if ($null -eq $transport.status) { "null" } else { "$($transport.status)" }
    $statuses[$statusKey] = 1 + [int]($statuses[$statusKey])
    $streamKey = "{0}_{1}" -f ([bool]$payload.stream).ToString().ToLowerInvariant(), $(if ($success) { "success" } else { "failed" })
    $streamResults[$streamKey] = 1 + [int]($streamResults[$streamKey])
    if ($null -ne $transport.latency_ms) { $latencies.Add([double]$transport.latency_ms) }
    if ($null -ne $observed.total_tokens) { $tokenTotal += [long]$observed.total_tokens }
    if ($null -ne $observed.known_cost) { $knownCostTotal += [decimal]$observed.known_cost; $knownCostCount++ }
    $bodyHash = if ([string]::IsNullOrEmpty([string]$transport.body)) { $null } else { Get-Sha256Text ([string]$transport.body) }
    $record = [pscustomobject][ordered]@{
        plan_id=$payload.plan_id; execution_id=$executionId; session_id=$payload.session_id
        request_profile_id=$payload.request_profile_id; started_at=$started.ToString("o"); completed_at=$completedAt.ToString("o")
        requested_model=$(if ($payload.requested_model) { $payload.requested_model } else { $payload.model })
        actual_model=$observed.actual_model; stream=[bool]$payload.stream; max_tokens=[int]$payload.max_tokens
        network_called=[bool]$transport.network_called; http_status=$transport.status; content_type=$transport.content_type
        latency_ms=$transport.latency_ms; ttft_ms=$transport.ttft_ms; response_id=$observed.response_id
        request_id=$transport.request_id; input_tokens=$observed.input_tokens; output_tokens=$observed.output_tokens
        total_tokens=$observed.total_tokens; finish_reason=$observed.finish_reason
        known_cost=$observed.known_cost
        sse_complete=$observed.sse_complete; done_received=$observed.done_received
        error_category=$errorCategory; error_summary=$errorSummary; response_body_sha256=$bodyHash
        transport_stage=$(if ($transportStage) { $transportStage } else { $transport.transport_stage })
        transport_exception_type=$(if ($transportExceptionType) { $transportExceptionType } else { $transport.transport_exception_type })
        transport_error_code=$(if ($transportErrorCode) { $transportErrorCode } else { $transport.transport_error_code })
        safe_transport_message=$(if ($safeTransportMessage) { $safeTransportMessage } else { $transport.safe_transport_message })
        source_type="measured_unified_uat"; is_mock=[bool]$useMock
        actual_channel=$null; actual_channel_status="pending_platform_log_correlation"
        scheduler_recommendation=$null; scheduler_recommendation_used_as_actual_channel=$false
    }
    Save-Evidence $payload $record ([string]$transport.body)
    Save-Record $record
    if ($transport.status -eq 401) { $stopReason = "uat_authentication_failed"; break }
    if ($transport.status -eq 429) { $stopReason = "rate_limited"; break }
    if ($errorCategory -eq "token_or_cost_anomaly") { $stopReason = "token_or_cost_anomaly"; break }
    if ($consecutiveFailures -ge 3) { $stopReason = "three_consecutive_failures"; break }
}

$sortedLatencies = @($latencies | Sort-Object)
function Get-Percentile([double[]]$Values, [double]$Percentile) {
    if ($Values.Count -eq 0) { return $null }
    $index = [Math]::Ceiling($Percentile * $Values.Count) - 1
    return [Math]::Round($Values[[Math]::Max(0, $index)], 3)
}
[pscustomobject]@{
    planned=$selected.Count; attempted=$attempted; succeeded=$succeeded; failed=$failed; skipped=$skipped
    http_status_distribution=$statuses; profile_distribution=$attemptedProfiles
    stream_success_failed=$streamResults
    average_latency_ms=if ($latencies.Count) { [Math]::Round(($latencies | Measure-Object -Average).Average, 3) } else { $null }
    p50_latency_ms=Get-Percentile $sortedLatencies 0.50; p95_latency_ms=Get-Percentile $sortedLatencies 0.95
    total_tokens=$tokenTotal; known_cost=if ($knownCostCount) { $knownCostTotal } else { $null }
    stop_reason=$stopReason; network_called=$anyNetworkCalled
} | ConvertTo-Json -Depth 10
