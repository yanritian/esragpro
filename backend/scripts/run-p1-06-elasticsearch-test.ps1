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
$composeFile = Join-Path $backendRoot "docker-compose.p1-06-test.yml"
$project = "esragpro-p1-06-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_06_test"
$previousLocation = Get-Location
$pyExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$pytestExe = Join-Path $backendRoot ".venv\Scripts\python.exe"

# --- Reserve two distinct free loopback ports simultaneously -------------------
$pgListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$esListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
try {
    $reservedPgPort = Reserve-FreeLoopbackPort -Listener $pgListener
    $reservedEsPort = Reserve-FreeLoopbackPort -Listener $esListener

    if ($reservedPgPort -eq 0 -or $reservedEsPort -eq 0) {
        throw "Port reservation returned zero: pg=$reservedPgPort es=$reservedEsPort"
    }
    $portsSet = [System.Collections.Generic.HashSet[int]]::new()
    $null = $portsSet.Add($reservedPgPort)
    $null = $portsSet.Add($reservedEsPort)
    if ($portsSet.Count -ne 2) {
        throw "Port reservation returned duplicate ports: pg=$reservedPgPort es=$reservedEsPort"
    }
} finally {
    $pgListener.Stop()
    $esListener.Stop()
}
Write-Host "Reserved ports: pg=$reservedPgPort es=$reservedEsPort" -ForegroundColor Cyan

# --- P1-06 allowlist for Ruff ------------------------------------------------
$ruffAllowlist = @(
    "app\memory_core\elasticsearch_handler.py",
    "app\memory_core\elasticsearch_compose.py",
    "app\memory_core\repositories\elasticsearch_projection\__init__.py",
    "app\memory_core\repositories\elasticsearch_projection\mapping.py",
    "app\memory_core\repositories\elasticsearch_projection\models.py",
    "app\memory_core\repositories\elasticsearch_projection\rrf.py",
    "app\memory_core\repositories\elasticsearch_projection\index.py",
    "app\memory_core\repositories\elasticsearch_projection\reader.py",
    "app\memory_core\repositories\elasticsearch_projection\rebuild.py",
    "tests\unit\test_elasticsearch_projection.py",
    "tests\integration\p1_06_elasticsearch\__init__.py",
    "tests\integration\p1_06_elasticsearch\conftest.py",
    "tests\integration\p1_06_elasticsearch\test_elasticsearch_projection_integration.py"
)

# --- Saved previous env values for restoration --------------------------------
$prevPgPort = $env:P1_06_POSTGRES_PORT
$prevEsPort = $env:P1_06_ELASTICSEARCH_PORT
$prevMigrationMode = $env:ESRAGPRO_MIGRATION_TEST_MODE
$prevDatabaseUrl = $env:DATABASE_URL
$prevEsUrl = $env:ELASTICSEARCH_URL
$prevDockerExe = $env:P1_06_DOCKER_EXE
$prevComposeFile = $env:P1_06_COMPOSE_FILE
$prevComposeProject = $env:P1_06_COMPOSE_PROJECT

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

    if ($null -eq $prevPgPort) { Remove-Item Env:P1_06_POSTGRES_PORT -ErrorAction SilentlyContinue } else { $env:P1_06_POSTGRES_PORT = $prevPgPort }
    if ($null -eq $prevEsPort) { Remove-Item Env:P1_06_ELASTICSEARCH_PORT -ErrorAction SilentlyContinue } else { $env:P1_06_ELASTICSEARCH_PORT = $prevEsPort }
    if ($null -eq $prevMigrationMode) { Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue } else { $env:ESRAGPRO_MIGRATION_TEST_MODE = $prevMigrationMode }
    if ($null -eq $prevDatabaseUrl) { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue } else { $env:DATABASE_URL = $prevDatabaseUrl }
    if ($null -eq $prevEsUrl) { Remove-Item Env:ELASTICSEARCH_URL -ErrorAction SilentlyContinue } else { $env:ELASTICSEARCH_URL = $prevEsUrl }
    if ($null -eq $prevDockerExe) { Remove-Item Env:P1_06_DOCKER_EXE -ErrorAction SilentlyContinue } else { $env:P1_06_DOCKER_EXE = $prevDockerExe }
    if ($null -eq $prevComposeFile) { Remove-Item Env:P1_06_COMPOSE_FILE -ErrorAction SilentlyContinue } else { $env:P1_06_COMPOSE_FILE = $prevComposeFile }
    if ($null -eq $prevComposeProject) { Remove-Item Env:P1_06_COMPOSE_PROJECT -ErrorAction SilentlyContinue } else { $env:P1_06_COMPOSE_PROJECT = $prevComposeProject }

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

    $env:P1_06_POSTGRES_PORT = [string]$reservedPgPort
    $env:P1_06_ELASTICSEARCH_PORT = [string]$reservedEsPort

    Write-Host "=== starting isolated PostgreSQL and Elasticsearch for P1-06 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-06 test services failed to start" }

    # Validate actual bindings match reserved ports exactly.
    $pgLines = @(& $dockerExe compose -p $project -f $composeFile port postgres 5432)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-06 postgres port (exit=$dockerExit)" }
    $rawPgBinding = ($pgLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawPgBinding) { throw "failed to discover P1-06 postgres port (no binding line)" }
    $rawPgBinding = $rawPgBinding.Trim()
    if ($rawPgBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-06 postgres binding is not loopback: $rawPgBinding"
    }
    $actualPgPort = [int]$Matches.port
    if ($actualPgPort -ne $reservedPgPort) {
        throw "P1-06 postgres bound to $actualPgPort but reserved $reservedPgPort"
    }

    $esLines = @(& $dockerExe compose -p $project -f $composeFile port elasticsearch 9200)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-06 elasticsearch port (exit=$dockerExit)" }
    $rawEsBinding = ($esLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawEsBinding) { throw "failed to discover P1-06 elasticsearch port (no binding line)" }
    $rawEsBinding = $rawEsBinding.Trim()
    if ($rawEsBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-06 elasticsearch binding is not loopback: $rawEsBinding"
    }
    $actualEsPort = [int]$Matches.port
    if ($actualEsPort -ne $reservedEsPort) {
        throw "P1-06 elasticsearch bound to $actualEsPort but reserved $reservedEsPort"
    }

    Write-Host "Bindings verified: pg=$actualPgPort es=$actualEsPort" -ForegroundColor Cyan

    $pgIdLines = @(& $dockerExe compose -p $project -f $composeFile ps -q postgres)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-06 postgres container id (exit=$dockerExit)" }
    $containerId = ($pgIdLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $containerId) { throw "failed to discover P1-06 postgres container id (no id line)" }
    $containerId = $containerId.Trim()
    if (-not $containerId) { throw "failed to discover P1-06 postgres container id (empty id)" }
    & $dockerExe exec -i $containerId psql -U postgres -c "SELECT 1 FROM pg_database WHERE datname = '$testDatabase';" | Out-Null
    $dbExists = & $dockerExe exec -i $containerId psql -U postgres -tAc "SELECT COUNT(*) FROM pg_database WHERE datname = '$testDatabase';"
    if ($dbExists.Trim() -eq "0") {
        & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
        if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }
    }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$actualPgPort/$testDatabase"
    $env:ELASTICSEARCH_URL = "http://127.0.0.1:$actualEsPort"
    $env:P1_06_DOCKER_EXE = $dockerExe
    $env:P1_06_COMPOSE_FILE = $composeFile
    $env:P1_06_COMPOSE_PROJECT = $project

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== Ruff check (P1-06 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff check failed" }

    Write-Host "=== Ruff format check (P1-06 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff format --check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed" }

    Write-Host "=== compileall (P1-06 production + test modules) ===" -ForegroundColor Cyan
    & $pyExe -m compileall -q app/memory_core/elasticsearch_handler.py app/memory_core/elasticsearch_compose.py app/memory_core/repositories/elasticsearch_projection/ tests/unit/test_elasticsearch_projection.py tests/integration/p1_06_elasticsearch/
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

    Write-Host "=== P1-06 unit tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\unit\test_elasticsearch_projection.py
    if ($LASTEXITCODE -ne 0) { throw "P1-06 unit tests failed" }

    Write-Host "=== Targeted high-risk Docker gates ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v "tests\integration\p1_06_elasticsearch\test_elasticsearch_projection_integration.py::TestConnectionLossRecovery::test_real_docker_stop_start_recovery" "tests\integration\p1_06_elasticsearch\test_elasticsearch_projection_integration.py::TestCompositionRoot::test_preflight_before_any_claim"
    if ($LASTEXITCODE -ne 0) { throw "Targeted high-risk Docker gates failed" }

    Write-Host "=== P1-06 integration tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\integration\p1_06_elasticsearch\
    if ($LASTEXITCODE -ne 0) { throw "P1-06 integration tests failed" }

    Write-Host "All P1-06 checks passed." -ForegroundColor Green
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
