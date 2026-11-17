param(
    [switch]$ConfirmIsolatedRestoreTest
)

$ErrorActionPreference = "Stop"

if (-not $ConfirmIsolatedRestoreTest) {
    throw "Pass -ConfirmIsolatedRestoreTest to run the isolated Elasticsearch restore drill."
}

$environmentValues = @(
    $env:ENVIRONMENT,
    $env:APP_ENV,
    $env:APP_ENVIRONMENT,
    $env:ESRAGPRO_ENVIRONMENT,
    $env:NODE_ENV
)
if ($environmentValues | Where-Object { $_ -and $_.Trim() -match "^(?i:production|prod)$" }) {
    throw "P1-10 restore drill refuses production environments."
}

function Find-DockerExe {
    $candidates = @()
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates += $command.Source }
    $candidates += Join-Path $env:LocalAppData "Docker\wsl\docker.exe"
    $candidates += Join-Path $env:LocalAppData "Programs\DockerDesktop\resources\bin\docker.exe"
    $candidates += Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    $candidates += Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\resources\bin\docker.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "docker.exe not found in PATH or Docker Desktop install locations."
}

function Reserve-FreeLoopbackPort {
    param([System.Net.Sockets.TcpListener]$Listener)
    $Listener.Start()
    return ([System.Net.IPEndPoint]$Listener.LocalEndpoint).Port
}

function Invoke-EsJson {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("GET", "PUT", "POST")][string]$Method,
        [string]$Body
    )

    $request = @{
        Uri         = "$BaseUrl$Path"
        Method      = $Method
        ErrorAction = "Stop"
        TimeoutSec  = 90
        Headers     = @{ Accept = "application/json" }
    }
    if ($PSBoundParameters.ContainsKey("Body")) {
        $request.ContentType = "application/json"
        $request.Body = $Body
    }

    try {
        return Invoke-RestMethod @request
    } catch {
        throw "Elasticsearch request failed ($Method $Path): $($_.Exception.Message)"
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $digest = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Get-IndexEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Index
    )

    $search = Invoke-EsJson -BaseUrl $BaseUrl -Path "/$Index/_search?size=1000&track_total_hits=true" -Method GET
    $reportedTotal = $search.hits.total
    if ($reportedTotal -is [System.Management.Automation.PSCustomObject]) {
        $count = [int]$reportedTotal.value
    } else {
        $count = [int]$reportedTotal
    }

    $hits = @($search.hits.hits)
    if ($count -ne $hits.Count) {
        throw "Index $Index reported $count hits but returned $($hits.Count) documents."
    }

    $rows = @()
    $sortedHits = @($hits | Sort-Object -Property @{ Expression = { [string]$($_._id) } })
    foreach ($hit in $sortedHits) {
        $source = $hit._source
        $rows += [ordered]@{
            id      = [string]$hit._id
            doc_id  = [string]$source.doc_id
            ordinal = [int]$source.ordinal
            content = [string]$source.content
        }
    }

    $canonical = if ($rows.Count -eq 0) { "[]" } else { $rows | ConvertTo-Json -Compress -Depth 10 }
    $ids = @($rows | ForEach-Object { [string]$_.id })
    return [pscustomobject]@{
        Count     = $count
        Ids       = $ids
        Canonical = $canonical
        Hash      = Get-Sha256Hex -Value $canonical
    }
}

function Restore-EnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$WasPresent,
        [AllowNull()][string]$PreviousValue
    )
    if ($WasPresent) {
        Set-Item -Path "Env:$Name" -Value $PreviousValue
    } else {
        Remove-Item -Path "Env:$Name" -ErrorAction SilentlyContinue
    }
}

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.p1-10-restore-test.yml"
$project = "esrag-restoretest-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$repository = "p1_10_shared_snapshot_repository"
$snapshot = "p1_10_sentinel_snapshot"
$index = "p1_10_restore_sentinel"
$previousLocation = (Get-Location).Path
$originalPath = $env:PATH
$hadSourcePort = Test-Path Env:P1_10_SOURCE_ELASTICSEARCH_PORT
$previousSourcePort = $env:P1_10_SOURCE_ELASTICSEARCH_PORT
$hadTargetPort = Test-Path Env:P1_10_TARGET_ELASTICSEARCH_PORT
$previousTargetPort = $env:P1_10_TARGET_ELASTICSEARCH_PORT
$dockerExe = $null
$composeStarted = $false
$drillSucceeded = $false
$cleanupErrors = [System.Collections.Generic.List[string]]::new()

$sourceListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$targetListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$reservedSourcePort = $null
$reservedTargetPort = $null

$sentinelDocuments = @(
    [ordered]@{
        doc_id  = "p1-10-sentinel-001"
        ordinal = 1
        content = "p1-10 deterministic restore sentinel alpha"
    },
    [ordered]@{
        doc_id  = "p1-10-sentinel-002"
        ordinal = 2
        content = "p1-10 deterministic restore sentinel beta"
    },
    [ordered]@{
        doc_id  = "p1-10-sentinel-003"
        ordinal = 3
        content = "p1-10 deterministic restore sentinel gamma"
    }
)

try {
    $reservedSourcePort = Reserve-FreeLoopbackPort -Listener $sourceListener
    $reservedTargetPort = Reserve-FreeLoopbackPort -Listener $targetListener
    if ($reservedSourcePort -eq $reservedTargetPort) {
        throw "Source and target port reservation returned the same port."
    }
} finally {
    $sourceListener.Stop()
    $targetListener.Stop()
}

try {
    $dockerExe = Find-DockerExe
    $env:PATH = "$(Split-Path -Parent $dockerExe);$originalPath"
    if (-not (Test-Path -LiteralPath $composeFile)) {
        throw "P1-10 restore-test Compose file is missing."
    }

    Set-Location -LiteralPath $backendRoot
    $env:P1_10_SOURCE_ELASTICSEARCH_PORT = [string]$reservedSourcePort
    $env:P1_10_TARGET_ELASTICSEARCH_PORT = [string]$reservedTargetPort

    Write-Host "Starting isolated Elasticsearch source and target containers." -ForegroundColor Cyan
    $composeStarted = $true
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) {
        throw "Isolated Elasticsearch services failed to start."
    }

    $sourceBindingLines = @(& $dockerExe compose -p $project -f $composeFile port source_elasticsearch 9200)
    if ($LASTEXITCODE -ne 0 -or $sourceBindingLines.Count -ne 1) {
        throw "Could not discover exactly one source Elasticsearch port binding."
    }
    $sourceBinding = $sourceBindingLines[0].Trim()
    if ($sourceBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "Source Elasticsearch binding is not loopback."
    }
    $sourcePort = [int]$Matches.port
    if ($sourcePort -ne $reservedSourcePort) {
        throw "Source Elasticsearch bound to an unexpected port."
    }

    $targetBindingLines = @(& $dockerExe compose -p $project -f $composeFile port target_elasticsearch 9200)
    if ($LASTEXITCODE -ne 0 -or $targetBindingLines.Count -ne 1) {
        throw "Could not discover exactly one target Elasticsearch port binding."
    }
    $targetBinding = $targetBindingLines[0].Trim()
    if ($targetBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "Target Elasticsearch binding is not loopback."
    }
    $targetPort = [int]$Matches.port
    if ($targetPort -ne $reservedTargetPort) {
        throw "Target Elasticsearch bound to an unexpected port."
    }

    $sourceContainerLines = @(& $dockerExe compose -p $project -f $composeFile ps -q source_elasticsearch)
    $sourceContainerExit = $LASTEXITCODE
    $targetContainerLines = @(& $dockerExe compose -p $project -f $composeFile ps -q target_elasticsearch)
    $targetContainerExit = $LASTEXITCODE
    $sourceContainerId = ($sourceContainerLines | Where-Object { $_ } | Select-Object -First 1)
    $targetContainerId = ($targetContainerLines | Where-Object { $_ } | Select-Object -First 1)
    if ($sourceContainerExit -ne 0 -or $targetContainerExit -ne 0 -or -not $sourceContainerId -or -not $targetContainerId) {
        throw "Could not identify both isolated Elasticsearch containers."
    }
    $sourceContainerId = $sourceContainerId.Trim()
    $targetContainerId = $targetContainerId.Trim()
    if ($sourceContainerId -eq $targetContainerId) {
        throw "Source and target Elasticsearch must be separate containers."
    }

    $sourceUrl = "http://127.0.0.1:$sourcePort"
    $targetUrl = "http://127.0.0.1:$targetPort"
    $healthPath = "/_cluster/health?wait_for_status=yellow&timeout=60s"
    Invoke-EsJson -BaseUrl $sourceUrl -Path $healthPath -Method GET | Out-Null
    Invoke-EsJson -BaseUrl $targetUrl -Path $healthPath -Method GET | Out-Null

    $repositoryBody = ([ordered]@{
        type     = "fs"
        settings = [ordered]@{
            location = "/snapshots"
            compress = $true
            readonly = $false
        }
    } | ConvertTo-Json -Compress -Depth 10)
    Invoke-EsJson -BaseUrl $sourceUrl -Path "/_snapshot/${repository}?verify=true" -Method PUT -Body $repositoryBody | Out-Null

    $mappingBody = ([ordered]@{
        mappings = [ordered]@{
            properties = [ordered]@{
                doc_id  = [ordered]@{ type = "keyword" }
                ordinal = [ordered]@{ type = "integer" }
                content = [ordered]@{ type = "text" }
            }
        }
    } | ConvertTo-Json -Compress -Depth 10)
    Invoke-EsJson -BaseUrl $sourceUrl -Path "/$index" -Method PUT -Body $mappingBody | Out-Null

    foreach ($document in $sentinelDocuments) {
        $documentBody = $document | ConvertTo-Json -Compress -Depth 10
        Invoke-EsJson -BaseUrl $sourceUrl -Path "/$index/_doc/$($document.doc_id)?refresh=true" -Method PUT -Body $documentBody | Out-Null
    }

    $sourceEvidence = Get-IndexEvidence -BaseUrl $sourceUrl -Index $index
    if ($sourceEvidence.Count -ne $sentinelDocuments.Count) {
        throw "Source sentinel count is not deterministic: expected $($sentinelDocuments.Count), got $($sourceEvidence.Count)."
    }
    if (($sourceEvidence.Ids -join ",") -ne (($sentinelDocuments | ForEach-Object { $_.doc_id }) -join ",")) {
        throw "Source sentinel IDs do not match the deterministic fixture."
    }

    $snapshotBody = ([ordered]@{
        indices            = $index
        include_global_state = $false
    } | ConvertTo-Json -Compress -Depth 10)
    $snapshotResult = Invoke-EsJson -BaseUrl $sourceUrl -Path "/_snapshot/${repository}/${snapshot}?wait_for_completion=true" -Method PUT -Body $snapshotBody
    $snapshotState = [string]$snapshotResult.snapshot.state
    if (-not $snapshotState -and $snapshotResult.snapshots) {
        $snapshotState = [string]$snapshotResult.snapshots[0].state
    }
    if ($snapshotState -ne "SUCCESS") {
        throw "Elasticsearch snapshot did not complete successfully (state=$snapshotState)."
    }

    $targetRepositoryBody = ([ordered]@{
        type     = "fs"
        settings = [ordered]@{
            location = "/snapshots"
            readonly = $true
        }
    } | ConvertTo-Json -Compress -Depth 10)
    Invoke-EsJson -BaseUrl $targetUrl -Path "/_snapshot/${repository}?verify=false" -Method PUT -Body $targetRepositoryBody | Out-Null
    $restoreBody = ([ordered]@{
        indices            = $index
        include_global_state = $false
        index_settings     = [ordered]@{ "index.number_of_replicas" = 0 }
    } | ConvertTo-Json -Compress -Depth 10)
    Invoke-EsJson -BaseUrl $targetUrl -Path "/_snapshot/${repository}/${snapshot}/_restore?wait_for_completion=true" -Method POST -Body $restoreBody | Out-Null

    Invoke-EsJson -BaseUrl $targetUrl -Path $healthPath -Method GET | Out-Null
    $restoredEvidence = Get-IndexEvidence -BaseUrl $targetUrl -Index $index
    if ($restoredEvidence.Count -ne $sourceEvidence.Count) {
        throw "Restored index count does not match the source."
    }
    if (($restoredEvidence.Ids -join ",") -ne ($sourceEvidence.Ids -join ",")) {
        throw "Restored index IDs do not match the source."
    }
    if ($restoredEvidence.Hash -ne $sourceEvidence.Hash) {
        throw "Restored canonical content hash does not match the source."
    }

    $probe = Invoke-EsJson -BaseUrl $targetUrl -Path "/$index/_search?size=1&track_total_hits=true" -Method GET
    if ([int]$probe.hits.total.value -lt 1) {
        throw "Restored index search returned no usable documents."
    }
    $queryBody = ([ordered]@{
        query = [ordered]@{
            term = [ordered]@{ doc_id = "p1-10-sentinel-002" }
        }
    } | ConvertTo-Json -Compress -Depth 10)
    $queryResult = Invoke-EsJson -BaseUrl $targetUrl -Path "/$index/_search" -Method POST -Body $queryBody
    if ([int]$queryResult.hits.total.value -ne 1 -or [string]$queryResult.hits.hits[0]._id -ne "p1-10-sentinel-002") {
        throw "Restored index term query did not return the expected sentinel document."
    }

    Write-Host "P1-10 Elasticsearch restore integrity verified: count=$($sourceEvidence.Count) ids=$($sourceEvidence.Ids -join ',') source-sha256=$($sourceEvidence.Hash) restored-sha256=$($restoredEvidence.Hash)" -ForegroundColor Green
    $drillSucceeded = $true
} finally {
    if ($dockerExe) {
        try {
            & $dockerExe compose -p $project -f $composeFile down -v --remove-orphans
            if ($LASTEXITCODE -ne 0) {
                $cleanupErrors.Add("Compose resource cleanup failed")
            }
        } catch {
            $cleanupErrors.Add("Compose resource cleanup failed")
        }
    }

    try {
        Restore-EnvironmentValue -Name "P1_10_SOURCE_ELASTICSEARCH_PORT" -WasPresent $hadSourcePort -PreviousValue $previousSourcePort
        Restore-EnvironmentValue -Name "P1_10_TARGET_ELASTICSEARCH_PORT" -WasPresent $hadTargetPort -PreviousValue $previousTargetPort
        $env:PATH = $originalPath
    } catch {
        $cleanupErrors.Add("environment restoration failed")
    }
    try {
        Set-Location -LiteralPath $previousLocation
    } catch {
        $cleanupErrors.Add("working-directory restoration failed")
    }
}

if ($cleanupErrors.Count -gt 0) {
    throw "P1-10 cleanup could not be proven complete: $($cleanupErrors -join ', ')."
}
if (-not $drillSucceeded) {
    throw "P1-10 Elasticsearch restore drill did not complete successfully."
}
