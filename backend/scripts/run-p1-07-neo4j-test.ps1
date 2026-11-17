$ErrorActionPreference = "Stop"

# --- Resolve docker.exe from known Docker Desktop install locations -----------
function Find-DockerExe {
    $candidates = @()
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }
    $candidates += Join-Path $env:LocalAppData "Docker\wsl\docker.exe"
    $candidates += Join-Path $env:LocalAppData "Programs\DockerDesktop\resources\bin\docker.exe"
    $candidates += Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    $candidates += Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\resources\bin\docker.exe"
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw "docker.exe not found in PATH or Docker Desktop install locations"
}

function Reserve-FreeLoopbackPort {
    param([System.Net.Sockets.TcpListener]$Listener)
    $Listener.Start()
    return ([System.Net.IPEndPoint]$Listener.LocalEndpoint).Port
}

$dockerExe = Find-DockerExe
$dockerDir = Split-Path -Parent $dockerExe
$originalPath = $env:PATH
$env:PATH = "$dockerDir;$originalPath"

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.p1-07-test.yml"
$project = "esragpro-p1-07-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_07_test"
$previousLocation = Get-Location
$pyExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$pytestExe = Join-Path $backendRoot ".venv\Scripts\python.exe"

# --- Reserve four distinct free loopback ports simultaneously ------------------
$pgListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$neo4jHttpListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$neo4jBoltListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
try {
    $reservedPgPort = Reserve-FreeLoopbackPort -Listener $pgListener
    $reservedNeo4jHttpPort = Reserve-FreeLoopbackPort -Listener $neo4jHttpListener
    $reservedNeo4jBoltPort = Reserve-FreeLoopbackPort -Listener $neo4jBoltListener

    $portsSet = [System.Collections.Generic.HashSet[int]]::new()
    $null = $portsSet.Add($reservedPgPort)
    $null = $portsSet.Add($reservedNeo4jHttpPort)
    $null = $portsSet.Add($reservedNeo4jBoltPort)
    if ($portsSet.Count -ne 3) {
        throw "Port reservation returned duplicate ports: pg=$reservedPgPort http=$reservedNeo4jHttpPort bolt=$reservedNeo4jBoltPort"
    }
} finally {
    $pgListener.Stop()
    $neo4jHttpListener.Stop()
    $neo4jBoltListener.Stop()
}
Write-Host "Reserved ports: pg=$reservedPgPort neo4j-http=$reservedNeo4jHttpPort neo4j-bolt=$reservedNeo4jBoltPort" -ForegroundColor Cyan

# --- P1-07 allowlist for Ruff ------------------------------------------------
$ruffAllowlist = @(
    "app\memory_core\neo4j_handler.py",
    "app\memory_core\neo4j_compose.py",
    "app\memory_core\repositories\neo4j_projection\__init__.py",
    "app\memory_core\repositories\neo4j_projection\adapter.py",
    "app\memory_core\repositories\neo4j_projection\models.py",
    "tests\unit\test_neo4j_projection_payload.py",
    "tests\unit\test_neo4j_projection_adapter.py",
    "tests\unit\test_neo4j_projection_handler.py",
    "tests\integration\p1_07_neo4j\__init__.py",
    "tests\integration\p1_07_neo4j\conftest.py",
    "tests\integration\p1_07_neo4j\test_neo4j_projection_integration.py"
)

# --- Saved previous env values for restoration --------------------------------
$prevPgPort = $env:P1_07_POSTGRES_PORT
$prevNeo4jHttpPort = $env:P1_07_NEO4J_HTTP_PORT
$prevNeo4jBoltPort = $env:P1_07_NEO4J_BOLT_PORT
$prevNeo4jPassword = $env:P1_07_NEO4J_PASSWORD
$prevMigrationMode = $env:ESRAGPRO_MIGRATION_TEST_MODE
$prevDatabaseUrl = $env:DATABASE_URL
$prevNeo4jUri = $env:NEO4J_URI
$prevNeo4jUser = $env:NEO4J_USER
$prevNeo4jPasswordEnv = $env:NEO4J_PASSWORD
$prevDockerExe = $env:P1_07_DOCKER_EXE
$prevComposeFile = $env:P1_07_COMPOSE_FILE
$prevComposeProject = $env:P1_07_COMPOSE_PROJECT

# --- Cleanup function --------------------------------------------------------
$cleanupFailed = $false
function Invoke-Cleanup {
    try {
        & $dockerExe compose -p $project -f $composeFile down -v --remove-orphans --timeout 30
        if ($LASTEXITCODE -ne 0) {
            $script:cleanupFailed = $true
            Write-Warning "Cleanup compose down returned non-zero exit code $LASTEXITCODE"
        }
    } catch {
        $script:cleanupFailed = $true
        Write-Warning "Cleanup compose down threw: $_"
    }

    if ($null -eq $prevPgPort) { Remove-Item Env:P1_07_POSTGRES_PORT -ErrorAction SilentlyContinue } else { $env:P1_07_POSTGRES_PORT = $prevPgPort }
    if ($null -eq $prevNeo4jHttpPort) { Remove-Item Env:P1_07_NEO4J_HTTP_PORT -ErrorAction SilentlyContinue } else { $env:P1_07_NEO4J_HTTP_PORT = $prevNeo4jHttpPort }
    if ($null -eq $prevNeo4jBoltPort) { Remove-Item Env:P1_07_NEO4J_BOLT_PORT -ErrorAction SilentlyContinue } else { $env:P1_07_NEO4J_BOLT_PORT = $prevNeo4jBoltPort }
    if ($null -eq $prevNeo4jPassword) { Remove-Item Env:P1_07_NEO4J_PASSWORD -ErrorAction SilentlyContinue } else { $env:P1_07_NEO4J_PASSWORD = $prevNeo4jPassword }
    if ($null -eq $prevMigrationMode) { Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue } else { $env:ESRAGPRO_MIGRATION_TEST_MODE = $prevMigrationMode }
    if ($null -eq $prevDatabaseUrl) { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue } else { $env:DATABASE_URL = $prevDatabaseUrl }
    if ($null -eq $prevNeo4jUri) { Remove-Item Env:NEO4J_URI -ErrorAction SilentlyContinue } else { $env:NEO4J_URI = $prevNeo4jUri }
    if ($null -eq $prevNeo4jUser) { Remove-Item Env:NEO4J_USER -ErrorAction SilentlyContinue } else { $env:NEO4J_USER = $prevNeo4jUser }
    if ($null -eq $prevNeo4jPasswordEnv) { Remove-Item Env:NEO4J_PASSWORD -ErrorAction SilentlyContinue } else { $env:NEO4J_PASSWORD = $prevNeo4jPasswordEnv }
    if ($null -eq $prevDockerExe) { Remove-Item Env:P1_07_DOCKER_EXE -ErrorAction SilentlyContinue } else { $env:P1_07_DOCKER_EXE = $prevDockerExe }
    if ($null -eq $prevComposeFile) { Remove-Item Env:P1_07_COMPOSE_FILE -ErrorAction SilentlyContinue } else { $env:P1_07_COMPOSE_FILE = $prevComposeFile }
    if ($null -eq $prevComposeProject) { Remove-Item Env:P1_07_COMPOSE_PROJECT -ErrorAction SilentlyContinue } else { $env:P1_07_COMPOSE_PROJECT = $prevComposeProject }

    $env:PATH = $originalPath
    Set-Location -LiteralPath $previousLocation
}

# --- Verify zero Docker resources for project ---------------------------------
function Verify-ZeroResources {
    $containers = @(& $dockerExe ps -a --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    $volumes = @(& $dockerExe volume ls --filter "label=com.docker.compose.project=$project" --format "{{.Name}}" 2>$null)
    $networks = @(& $dockerExe network ls --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    $containers = $containers | Where-Object { $_ }
    $volumes = $volumes | Where-Object { $_ }
    $networks = $networks | Where-Object { $_ }
    $cCount = $containers.Count
    $vCount = $volumes.Count
    $nCount = $networks.Count
    Write-Host "Post-cleanup resource counts: containers=$cCount volumes=$vCount networks=$nCount" -ForegroundColor Cyan
    if ($cCount -gt 0) {
        Write-Warning "Force-removing residual containers..."
        & $dockerExe rm -f @containers 2>$null
    }
    if ($vCount -gt 0) {
        Write-Warning "Force-removing residual volumes..."
        & $dockerExe volume rm -f @volumes 2>$null
    }
    if ($nCount -gt 0) {
        Write-Warning "Force-removing residual networks..."
        & $dockerExe network rm @networks 2>$null
    }
    $c2 = @(& $dockerExe ps -a --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null) | Where-Object { $_ }
    $v2 = @(& $dockerExe volume ls --filter "label=com.docker.compose.project=$project" --format "{{.Name}}" 2>$null) | Where-Object { $_ }
    $n2 = @(& $dockerExe network ls --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null) | Where-Object { $_ }
    $c2Count = $c2.Count
    $v2Count = $v2.Count
    $n2Count = $n2.Count
    Write-Host "Second independent verification: containers=$c2Count volumes=$v2Count networks=$n2Count" -ForegroundColor Cyan
    if ($c2Count -ne 0 -or $v2Count -ne 0 -or $n2Count -ne 0) {
        throw "Post-cleanup verification FAILED: $c2Count containers, $v2Count volumes, $n2Count networks remain"
    }
}

$testFailure = $null
try {
    Set-Location -LiteralPath $backendRoot

    $env:P1_07_POSTGRES_PORT = [string]$reservedPgPort
    $env:P1_07_NEO4J_HTTP_PORT = [string]$reservedNeo4jHttpPort
    $env:P1_07_NEO4J_BOLT_PORT = [string]$reservedNeo4jBoltPort
    $env:P1_07_NEO4J_PASSWORD = "esragpro_p1_07_test"

    Write-Host "=== starting isolated PostgreSQL and Neo4j for P1-07 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-07 test services failed to start" }

    # Validate actual bindings match reserved ports exactly.
    $pgLines = @(& $dockerExe compose -p $project -f $composeFile port postgres 5432)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-07 postgres port (exit=$dockerExit)" }
    $rawPgBinding = ($pgLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawPgBinding) { throw "failed to discover P1-07 postgres port (no binding line)" }
    $rawPgBinding = $rawPgBinding.Trim()
    if ($rawPgBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-07 postgres binding is not loopback: $rawPgBinding"
    }
    $actualPgPort = [int]$Matches.port
    if ($actualPgPort -ne $reservedPgPort) {
        throw "P1-07 postgres bound to $actualPgPort but reserved $reservedPgPort"
    }

    $neo4jBoltLines = @(& $dockerExe compose -p $project -f $composeFile port neo4j 7687)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-07 neo4j bolt port (exit=$dockerExit)" }
    $rawBoltBinding = ($neo4jBoltLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawBoltBinding) { throw "failed to discover P1-07 neo4j bolt port (no binding line)" }
    $rawBoltBinding = $rawBoltBinding.Trim()
    if ($rawBoltBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-07 neo4j bolt binding is not loopback: $rawBoltBinding"
    }
    $actualBoltPort = [int]$Matches.port
    if ($actualBoltPort -ne $reservedNeo4jBoltPort) {
        throw "P1-07 neo4j bolt bound to $actualBoltPort but reserved $reservedNeo4jBoltPort"
    }

    Write-Host "Bindings verified: pg=$actualPgPort neo4j-bolt=$actualBoltPort" -ForegroundColor Cyan

    $pgIdLines = @(& $dockerExe compose -p $project -f $composeFile ps -q postgres)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-07 postgres container id (exit=$dockerExit)" }
    $containerId = ($pgIdLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $containerId) { throw "failed to discover P1-07 postgres container id (no id line)" }
    $containerId = $containerId.Trim()
    if (-not $containerId) { throw "failed to discover P1-07 postgres container id (empty id)" }
    & $dockerExe exec -i $containerId psql -U postgres -c "SELECT 1 FROM pg_database WHERE datname = '$testDatabase';" | Out-Null
    $dbExists = & $dockerExe exec -i $containerId psql -U postgres -tAc "SELECT COUNT(*) FROM pg_database WHERE datname = '$testDatabase';"
    if ($dbExists.Trim() -eq "0") {
        & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
        if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }
    }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$actualPgPort/$testDatabase"
    $env:NEO4J_URI = "bolt://127.0.0.1:$actualBoltPort"
    $env:NEO4J_USER = "neo4j"
    $env:NEO4J_PASSWORD = "esragpro_p1_07_test"
    $env:P1_07_DOCKER_EXE = $dockerExe
    $env:P1_07_COMPOSE_FILE = $composeFile
    $env:P1_07_COMPOSE_PROJECT = $project

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== Ruff check (P1-07 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff check failed" }

    Write-Host "=== Ruff format check (P1-07 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff format --check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed" }

    Write-Host "=== compileall (P1-07 production + test modules) ===" -ForegroundColor Cyan
    & $pyExe -m compileall -q app/memory_core/neo4j_handler.py app/memory_core/neo4j_compose.py app/memory_core/repositories/neo4j_projection/ tests/unit/test_neo4j_projection_payload.py tests/unit/test_neo4j_projection_adapter.py tests/unit/test_neo4j_projection_handler.py tests/integration/p1_07_neo4j/
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

    Write-Host "=== P1-07 unit tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\unit\test_neo4j_projection_payload.py tests\unit\test_neo4j_projection_adapter.py tests\unit\test_neo4j_projection_handler.py
    if ($LASTEXITCODE -ne 0) { throw "P1-07 unit tests failed" }

    Write-Host "=== P1-07 integration tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\integration\p1_07_neo4j\
    if ($LASTEXITCODE -ne 0) { throw "P1-07 integration tests failed" }

    Write-Host "=== Existing Neo4j claim-projector regression ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\unit\test_neo4j_projector.py
    if ($LASTEXITCODE -ne 0) { throw "Neo4j claim-projector regression failed" }

    Write-Host "All P1-07 checks passed." -ForegroundColor Green
} catch {
    $testFailure = $_
    Write-Warning "Test failure: $_"
} finally {
    Invoke-Cleanup
    try {
        Verify-ZeroResources
    } catch {
        $script:cleanupFailed = $true
        Write-Warning "Cleanup verification threw: $_"
    }
    if ($testFailure -or $cleanupFailed) {
        $messages = @()
        if ($testFailure) { $messages += "test failure: $testFailure" }
        if ($cleanupFailed) { $messages += "cleanup failure (see warnings above)" }
        throw ($messages -join "; ")
    }
}
