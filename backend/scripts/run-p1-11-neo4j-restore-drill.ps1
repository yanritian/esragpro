param(
    [switch]$ConfirmIsolatedRestoreTest
)

$ErrorActionPreference = "Stop"

if (-not $ConfirmIsolatedRestoreTest) {
    throw "Pass -ConfirmIsolatedRestoreTest to run the isolated Neo4j restore drill."
}

$environmentValues = @(
    $env:ENVIRONMENT,
    $env:APP_ENV,
    $env:APP_ENVIRONMENT,
    $env:ESRAGPRO_ENVIRONMENT,
    $env:NODE_ENV
)
if ($environmentValues | Where-Object { $_ -and $_.Trim() -match "^(?i:production|prod)$" }) {
    throw "P1-11 restore drill refuses production environments."
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

function Get-SafeComposeErrorText {
    param(
        [AllowEmptyString()][AllowNull()][string]$ErrorText
    )

    $safeText = if ($null -eq $ErrorText) { "" } else { $ErrorText.Trim() }
    $secrets = @(
        [string]$neo4jPassword,
        [string]$env:P1_11_NEO4J_PASSWORD
    ) | Where-Object { $_ -and $_.Length -gt 0 } | Select-Object -Unique
    foreach ($secret in $secrets) {
        $safeText = $safeText.Replace($secret, "<redacted>")
    }
    $safeText = [regex]::Replace($safeText, '(?i)(--password(?:=|\s+))([^\s]+)', '$1<redacted>')
    $safeText = [regex]::Replace($safeText, '(?i)(NEO4J_AUTH\s*=\s*[^/\s]+/)([^\s]+)', '$1<redacted>')
    if ([string]::IsNullOrWhiteSpace($safeText)) {
        return "no stderr output"
    }
    return $safeText
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $operationLabel = [regex]::Replace($Operation, '[^A-Za-z0-9_. -]', '_').Trim()
    if ([string]::IsNullOrWhiteSpace($operationLabel)) {
        $operationLabel = "unknown"
    }
    $stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) "esrag-p1-11-compose-$([guid]::NewGuid().ToString('N')).stderr"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $dockerExe compose @Arguments 2> $stderrPath)
        $exitCode = $LASTEXITCODE
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            [System.IO.File]::ReadAllText($stderrPath)
        } else {
            ""
        }
        if ($exitCode -ne 0) {
            $safeStderr = Get-SafeComposeErrorText -ErrorText $stderr
            throw "Docker Compose operation '$operationLabel' failed (exit code $exitCode): $safeStderr"
        }
        return $output
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Neo4jCypher {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Query
    )

    $arguments = @(
        "-p", $project,
        "-f", $composeFile,
        "exec",
        "-T",
        "--user", "neo4j",
        $Service,
        "cypher-shell",
        "--non-interactive",
        "--format", "plain",
        "--username", "neo4j",
        "--password", $Password,
        $Query
    )
    return @(Invoke-Compose -Operation "cypher-shell $Service" -Arguments $arguments)
}

function Wait-Neo4jReady {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $null = Invoke-Neo4jCypher -Service $Service -Password $Password -Query "RETURN 1 AS ready"
            return
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -eq 60) {
                throw "Neo4j service $Service did not become ready. Last compose error: $(Get-SafeComposeErrorText -ErrorText $lastError)"
            }
            Start-Sleep -Seconds 2
        }
    }
}

function Get-ComposeLoopbackPort {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][int]$ContainerPort
    )

    $lines = @(Invoke-Compose -Operation "discover $Service loopback port $ContainerPort" -Arguments @(
            "-p", $project,
            "-f", $composeFile,
            "port",
            $Service,
            [string]$ContainerPort
        ))
    $bindings = @($lines | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })
    if ($bindings.Count -ne 1) {
        throw "Could not discover exactly one $Service port binding."
    }

    $binding = $bindings[0].Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "$Service port binding is not loopback."
    }
    return [int]$Matches.port
}

function Convert-CypherPlainRows {
    param(
        [Parameter(Mandatory = $true)][string[]]$Output,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $lines = @($Output | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) {
        throw "$Description query returned no rows."
    }

    try {
        return @(ConvertFrom-Csv -InputObject ($lines -join [Environment]::NewLine))
    } catch {
        throw "$Description query returned invalid plain output."
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

function Get-GraphEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $nodeOutput = @(Invoke-Neo4jCypher -Service $Service -Password $Password -Query @"
MATCH (node:RestoreSentinel)
RETURN node.id AS id, node.ordinal AS ordinal, node.payload AS payload
ORDER BY id
"@)
    $edgeOutput = @(Invoke-Neo4jCypher -Service $Service -Password $Password -Query @"
MATCH (source)-[edge:P1_11_EDGE]->(target)
RETURN edge.id AS id, source.id AS source_id, target.id AS target_id, edge.ordinal AS ordinal
ORDER BY id
"@)
    $nodeRows = @(Convert-CypherPlainRows -Output $nodeOutput -Description "$Service node")
    $edgeRows = @(Convert-CypherPlainRows -Output $edgeOutput -Description "$Service edge")

    $canonicalRows = @(
        $nodeRows | ForEach-Object {
            [ordered]@{
                kind    = "node"
                id      = [string]$_.id
                ordinal = [int]$_.ordinal
                payload = [string]$_.payload
            }
        }
        $edgeRows | ForEach-Object {
            [ordered]@{
                kind      = "edge"
                id        = [string]$_.id
                ordinal   = [int]$_.ordinal
                source_id = [string]$_.source_id
                target_id = [string]$_.target_id
            }
        }
    )
    $canonical = $canonicalRows | ConvertTo-Json -Compress -Depth 10

    return [pscustomobject]@{
        NodeRows = $nodeRows
        EdgeRows = $edgeRows
        NodeIds  = @($nodeRows | ForEach-Object { [string]$_.id })
        EdgeIds  = @($edgeRows | ForEach-Object { [string]$_.id })
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
$composeFile = Join-Path $backendRoot "docker-compose.p1-11-neo4j-restore-test.yml"
$project = "esrag-restoretest-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$previousLocation = (Get-Location).Path
$originalPath = $env:PATH
$dockerExe = $null
$compose_started = $false
$drillSucceeded = $false
$cleanupErrors = [System.Collections.Generic.List[string]]::new()

$environmentNames = @(
    "P1_11_SOURCE_NEO4J_HTTP_PORT",
    "P1_11_SOURCE_NEO4J_BOLT_PORT",
    "P1_11_TARGET_NEO4J_HTTP_PORT",
    "P1_11_TARGET_NEO4J_BOLT_PORT",
    "P1_11_NEO4J_PASSWORD"
)
$previousEnvironment = @{}
$hadEnvironment = @{}
foreach ($name in $environmentNames) {
    $hadEnvironment[$name] = Test-Path "Env:$name"
    $previousEnvironment[$name] = (Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value
}

$neo4jPassword = if ($hadEnvironment["P1_11_NEO4J_PASSWORD"] -and -not [string]::IsNullOrWhiteSpace($previousEnvironment["P1_11_NEO4J_PASSWORD"])) {
    [string]$previousEnvironment["P1_11_NEO4J_PASSWORD"]
} else {
    "p1-11-restore-only"
}

$portListeners = @(
    (New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)),
    (New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)),
    (New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)),
    (New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0))
)
$reservedPorts = @()
try {
    foreach ($listener in $portListeners) {
        $reservedPorts += Reserve-FreeLoopbackPort -Listener $listener
    }
    if (($reservedPorts | Select-Object -Unique).Count -ne 4) {
        throw "Loopback port reservation returned duplicate ports."
    }
} finally {
    foreach ($listener in $portListeners) {
        $listener.Stop()
    }
}

try {
    $dockerExe = Find-DockerExe
    $env:PATH = "$(Split-Path -Parent $dockerExe);$originalPath"
    if (-not (Test-Path -LiteralPath $composeFile)) {
        throw "P1-11 Neo4j restore-test Compose file is missing."
    }

    Set-Location -LiteralPath $backendRoot
    $env:P1_11_SOURCE_NEO4J_HTTP_PORT = [string]$reservedPorts[0]
    $env:P1_11_SOURCE_NEO4J_BOLT_PORT = [string]$reservedPorts[1]
    $env:P1_11_TARGET_NEO4J_HTTP_PORT = [string]$reservedPorts[2]
    $env:P1_11_TARGET_NEO4J_BOLT_PORT = [string]$reservedPorts[3]
    $env:P1_11_NEO4J_PASSWORD = $neo4jPassword

    Write-Host "Starting isolated Neo4j Community source and target containers." -ForegroundColor Cyan
    $compose_started = $true
    $null = Invoke-Compose -Operation "start isolated Neo4j services" -Arguments @("-p", $project, "-f", $composeFile, "up", "-d")
    Wait-Neo4jReady -Service "source_neo4j" -Password $neo4jPassword
    Wait-Neo4jReady -Service "target_neo4j" -Password $neo4jPassword

    $sourceHttpPort = Get-ComposeLoopbackPort -Service "source_neo4j" -ContainerPort 7474
    $sourceBoltPort = Get-ComposeLoopbackPort -Service "source_neo4j" -ContainerPort 7687
    $targetHttpPort = Get-ComposeLoopbackPort -Service "target_neo4j" -ContainerPort 7474
    $targetBoltPort = Get-ComposeLoopbackPort -Service "target_neo4j" -ContainerPort 7687
    if ($sourceHttpPort -ne $reservedPorts[0] -or $sourceBoltPort -ne $reservedPorts[1]) {
        throw "Source Neo4j ports did not match the reserved loopback ports."
    }
    if ($targetHttpPort -ne $reservedPorts[2] -or $targetBoltPort -ne $reservedPorts[3]) {
        throw "Target Neo4j ports did not match the reserved loopback ports."
    }

    $createQuery = @"
CREATE
  (n1:RestoreSentinel {id: 'p1-11-node-001', ordinal: 1, payload: 'deterministic-alpha'}),
  (n2:RestoreSentinel {id: 'p1-11-node-002', ordinal: 2, payload: 'deterministic-beta'}),
  (n3:RestoreSentinel {id: 'p1-11-node-003', ordinal: 3, payload: 'deterministic-gamma'}),
  (n1)-[r1:P1_11_EDGE {id: 'p1-11-edge-001', ordinal: 1}]->(n2),
  (n2)-[r2:P1_11_EDGE {id: 'p1-11-edge-002', ordinal: 2}]->(n3)
RETURN count(n1) + count(n2) + count(n3) AS node_count,
       count(r1) + count(r2) AS edge_count
"@
    $null = Invoke-Neo4jCypher -Service "source_neo4j" -Password $neo4jPassword -Query $createQuery

    $sourceEvidence = Get-GraphEvidence -Service "source_neo4j" -Password $neo4jPassword
    $source_node_rows = @($sourceEvidence.NodeRows)
    $source_edge_rows = @($sourceEvidence.EdgeRows)
    if ($source_node_rows.Count -ne 3 -or $source_edge_rows.Count -ne 2) {
        throw "Source graph fixture has unexpected counts."
    }
    if (($sourceEvidence.NodeIds -join ",") -ne "p1-11-node-001,p1-11-node-002,p1-11-node-003") {
        throw "Source graph node IDs do not match the deterministic fixture."
    }
    if (($sourceEvidence.EdgeIds -join ",") -ne "p1-11-edge-001,p1-11-edge-002") {
        throw "Source graph edge IDs do not match the deterministic fixture."
    }

    $null = Invoke-Compose -Operation "stop Neo4j services for offline restore" -Arguments @(
        "-p", $project, "-f", $composeFile, "stop", "source_neo4j", "target_neo4j"
    )

    $null = Invoke-Compose -Operation "offline Neo4j database dump" -Arguments @(
        "-p", $project, "-f", $composeFile, "run", "--rm", "--no-deps", "-T", "--user", "neo4j",
        "source_neo4j", "neo4j-admin", "database", "dump", "neo4j", "--to-path=/var/lib/neo4j/import"
    )

    $null = Invoke-Compose -Operation "offline Neo4j database load" -Arguments @(
        "-p", $project, "-f", $composeFile, "run", "--rm", "--no-deps", "-T", "--user", "neo4j",
        "target_neo4j", "neo4j-admin", "database", "load", "--from-path=/var/lib/neo4j/import", "neo4j", "--overwrite-destination=true"
    )

    $null = Invoke-Compose -Operation "restart restored Neo4j target" -Arguments @(
        "-p", $project, "-f", $composeFile, "start", "target_neo4j"
    )
    Wait-Neo4jReady -Service "target_neo4j" -Password $neo4jPassword

    $targetEvidence = Get-GraphEvidence -Service "target_neo4j" -Password $neo4jPassword
    $target_node_rows = @($targetEvidence.NodeRows)
    $target_edge_rows = @($targetEvidence.EdgeRows)
    if ($target_node_rows.Count -ne $source_node_rows.Count -or $target_edge_rows.Count -ne $source_edge_rows.Count) {
        throw "Restored Neo4j graph counts do not match the source."
    }
    if (($targetEvidence.NodeIds -join ",") -ne ($sourceEvidence.NodeIds -join ",")) {
        throw "Restored Neo4j node IDs do not match the source."
    }
    if (($targetEvidence.EdgeIds -join ",") -ne ($sourceEvidence.EdgeIds -join ",")) {
        throw "Restored Neo4j edge IDs do not match the source."
    }
    if ($targetEvidence.Hash -ne $sourceEvidence.Hash) {
        throw "Restored Neo4j canonical SHA-256 does not match the source."
    }

    $restoredQueryRows = @(Convert-CypherPlainRows -Description "restored relationship" -Output @(Invoke-Neo4jCypher -Service "target_neo4j" -Password $neo4jPassword -Query @"
MATCH (source:RestoreSentinel {id: 'p1-11-node-002'})-[edge:P1_11_EDGE {id: 'p1-11-edge-002'}]->(target:RestoreSentinel {id: 'p1-11-node-003'})
RETURN source.id AS source_id, edge.id AS edge_id, target.id AS target_id
"@))
    if ($restoredQueryRows.Count -ne 1 -or
        [string]$restoredQueryRows[0].source_id -ne "p1-11-node-002" -or
        [string]$restoredQueryRows[0].edge_id -ne "p1-11-edge-002" -or
        [string]$restoredQueryRows[0].target_id -ne "p1-11-node-003") {
        throw "Restored Neo4j relationship query did not return the expected sentinel edge."
    }

    Write-Host "P1-11 Neo4j restore integrity verified: nodes=$($source_node_rows.Count) edges=$($source_edge_rows.Count) node-ids=$($sourceEvidence.NodeIds -join ',') edge-ids=$($sourceEvidence.EdgeIds -join ',') source-sha256=$($sourceEvidence.Hash) restored-sha256=$($targetEvidence.Hash)" -ForegroundColor Green
    $drillSucceeded = $true
} finally {
    if ($dockerExe -and $compose_started) {
        try {
            $null = Invoke-Compose -Operation "cleanup isolated Compose project" -Arguments @(
                "-p", $project, "-f", $composeFile, "down", "-v", "--remove-orphans"
            )
        } catch {
            $cleanupErrors.Add("Compose resource cleanup failed: $($_.Exception.Message)")
        }
    }

    try {
        foreach ($name in $environmentNames) {
            Restore-EnvironmentValue -Name $name -WasPresent $hadEnvironment[$name] -PreviousValue $previousEnvironment[$name]
        }
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
    throw "P1-11 cleanup could not be proven complete: $($cleanupErrors -join ', ')."
}
if (-not $drillSucceeded) {
    throw "P1-11 Neo4j restore drill did not complete successfully."
}
