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
    # Bind a TcpListener to loopback port 0, call Start() so the OS
    # assigns a real port, then return it. The caller is responsible
    # for calling Stop() when all three listeners can be released.
    param([System.Net.Sockets.TcpListener]$Listener)
    $Listener.Start()
    return ([System.Net.IPEndPoint]$Listener.LocalEndpoint).Port
}

$dockerExe = Find-DockerExe
$dockerDir = Split-Path -Parent $dockerExe
$originalPath = $env:PATH
$env:PATH = "$dockerDir;$originalPath"

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.p1-05-test.yml"
$project = "esragpro-p1-05-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_05_test"
$previousLocation = Get-Location
$pyExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$pytestExe = Join-Path $backendRoot ".venv\Scripts\python.exe"

# --- Reserve three distinct free loopback ports simultaneously -----------------
# Bind all three listeners BEFORE reading any port, so Windows cannot
# reuse the same ephemeral port across sequential bind/release cycles.
$pgListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$redisListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$authRedisListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
try {
    $reservedPgPort = Reserve-FreeLoopbackPort -Listener $pgListener
    $reservedRedisPort = Reserve-FreeLoopbackPort -Listener $redisListener
    $reservedAuthRedisPort = Reserve-FreeLoopbackPort -Listener $authRedisListener

    # Prove all three are non-zero.
    if ($reservedPgPort -eq 0 -or $reservedRedisPort -eq 0 -or $reservedAuthRedisPort -eq 0) {
        throw "Port reservation returned zero: pg=$reservedPgPort redis=$reservedRedisPort authRedis=$reservedAuthRedisPort"
    }
    # Prove all three are pairwise distinct.
    $portsSet = [System.Collections.Generic.HashSet[int]]::new()
    $null = $portsSet.Add($reservedPgPort)
    $null = $portsSet.Add($reservedRedisPort)
    $null = $portsSet.Add($reservedAuthRedisPort)
    if ($portsSet.Count -ne 3) {
        throw "Port reservation returned duplicate ports: pg=$reservedPgPort redis=$reservedRedisPort authRedis=$reservedAuthRedisPort"
    }
} finally {
    $pgListener.Stop()
    $redisListener.Stop()
    $authRedisListener.Stop()
}
Write-Host "Reserved ports: pg=$reservedPgPort redis=$reservedRedisPort authRedis=$reservedAuthRedisPort" -ForegroundColor Cyan

# --- P1-05 allowlist for Ruff ------------------------------------------------
$ruffAllowlist = @(
    "app\memory_core\redis_hot_handler.py",
    "app\memory_core\redis_hot_compose.py",
    "app\memory_core\repositories\redis_hot\__init__.py",
    "app\memory_core\repositories\redis_hot\keys.py",
    "app\memory_core\repositories\redis_hot\script.py",
    "app\memory_core\repositories\redis_hot\writer.py",
    "app\memory_core\repositories\redis_hot\reader.py",
    "app\memory_core\repositories\redis_hot\rebuild.py",
    "tests\unit\test_redis_hot_state.py",
    "tests\integration\p1_05_redis_hot\__init__.py",
    "tests\integration\p1_05_redis_hot\conftest.py",
    "tests\integration\p1_05_redis_hot\test_redis_hot_state_integration.py"
)

# --- Saved previous env values for restoration --------------------------------
$prevPgPort = $env:P1_05_POSTGRES_PORT
$prevRedisPort = $env:P1_05_REDIS_PORT
$prevAuthRedisPort = $env:P1_05_AUTH_REDIS_PORT
$prevMigrationMode = $env:ESRAGPRO_MIGRATION_TEST_MODE
$prevDatabaseUrl = $env:DATABASE_URL
$prevRedisUrl = $env:REDIS_URL
$prevAuthRedisUrl = $env:AUTH_REDIS_URL
$prevDockerExe = $env:P1_05_DOCKER_EXE
$prevComposeFile = $env:P1_05_COMPOSE_FILE
$prevComposeProject = $env:P1_05_COMPOSE_PROJECT

# --- Cleanup function --------------------------------------------------------
$cleanupFailed = $false
function Invoke-Cleanup {
    # Always restore environment variables, PATH, and working directory,
    # even when docker compose down fails. Never throw Write-Error before
    # restoration completes.
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

    # Restore environment variables (always, even if compose down failed).
    if ($null -eq $prevPgPort) { Remove-Item Env:P1_05_POSTGRES_PORT -ErrorAction SilentlyContinue } else { $env:P1_05_POSTGRES_PORT = $prevPgPort }
    if ($null -eq $prevRedisPort) { Remove-Item Env:P1_05_REDIS_PORT -ErrorAction SilentlyContinue } else { $env:P1_05_REDIS_PORT = $prevRedisPort }
    if ($null -eq $prevAuthRedisPort) { Remove-Item Env:P1_05_AUTH_REDIS_PORT -ErrorAction SilentlyContinue } else { $env:P1_05_AUTH_REDIS_PORT = $prevAuthRedisPort }
    if ($null -eq $prevMigrationMode) { Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue } else { $env:ESRAGPRO_MIGRATION_TEST_MODE = $prevMigrationMode }
    if ($null -eq $prevDatabaseUrl) { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue } else { $env:DATABASE_URL = $prevDatabaseUrl }
    if ($null -eq $prevRedisUrl) { Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue } else { $env:REDIS_URL = $prevRedisUrl }
    if ($null -eq $prevAuthRedisUrl) { Remove-Item Env:AUTH_REDIS_URL -ErrorAction SilentlyContinue } else { $env:AUTH_REDIS_URL = $prevAuthRedisUrl }
    if ($null -eq $prevDockerExe) { Remove-Item Env:P1_05_DOCKER_EXE -ErrorAction SilentlyContinue } else { $env:P1_05_DOCKER_EXE = $prevDockerExe }
    if ($null -eq $prevComposeFile) { Remove-Item Env:P1_05_COMPOSE_FILE -ErrorAction SilentlyContinue } else { $env:P1_05_COMPOSE_FILE = $prevComposeFile }
    if ($null -eq $prevComposeProject) { Remove-Item Env:P1_05_COMPOSE_PROJECT -ErrorAction SilentlyContinue } else { $env:P1_05_COMPOSE_PROJECT = $prevComposeProject }

    $env:PATH = $originalPath
    Set-Location -LiteralPath $previousLocation
}

# --- Verify zero Docker resources for project ---------------------------------
function Verify-ZeroResources {
    # Always scan and print counts. Force-remove residual resources
    # that carry the exact randomized Compose project label. Then
    # ALWAYS run a second independent verification scan — even when
    # the first scan was already 0/0/0.
    $containers = @(& $dockerExe ps -a --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    $volumes = @(& $dockerExe volume ls --filter "label=com.docker.compose.project=$project" --format "{{.Name}}" 2>$null)
    $networks = @(& $dockerExe network ls --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    # Filter out empty strings from the arrays.
    $containers = $containers | Where-Object { $_ }
    $volumes = $volumes | Where-Object { $_ }
    $networks = $networks | Where-Object { $_ }
    $cCount = $containers.Count
    $vCount = $volumes.Count
    $nCount = $networks.Count
    Write-Host "Post-cleanup resource counts: containers=$cCount volumes=$vCount networks=$nCount" -ForegroundColor Cyan
    if ($cCount -gt 0) {
        Write-Warning "Force-removing residual containers..."
        # Pass as an array, not one space-joined argument.
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
    # Second independent verification — ALWAYS runs.
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

    # Export reserved ports so Compose binds to them deterministically.
    $env:P1_05_POSTGRES_PORT = [string]$reservedPgPort
    $env:P1_05_REDIS_PORT = [string]$reservedRedisPort
    $env:P1_05_AUTH_REDIS_PORT = [string]$reservedAuthRedisPort

    Write-Host "=== starting isolated PostgreSQL and Redis for P1-05 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-05 test services failed to start" }

    # Validate actual bindings match reserved ports exactly.
    # Capture native docker output into a PowerShell array FIRST, then
    # snapshot $LASTEXITCODE, then select the first non-empty line. Piping
    # a native process directly into Select-Object -First 1 closes the pipe
    # early on Windows PowerShell 5.1 and corrupts $LASTEXITCODE to -1.
    $pgLines = @(& $dockerExe compose -p $project -f $composeFile port postgres 5432)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-05 postgres port (exit=$dockerExit)" }
    $rawPgBinding = ($pgLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawPgBinding) { throw "failed to discover P1-05 postgres port (no binding line)" }
    $rawPgBinding = $rawPgBinding.Trim()
    if ($rawPgBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-05 postgres binding is not loopback: $rawPgBinding"
    }
    $actualPgPort = [int]$Matches.port
    if ($actualPgPort -ne $reservedPgPort) {
        throw "P1-05 postgres bound to $actualPgPort but reserved $reservedPgPort"
    }

    $redisLines = @(& $dockerExe compose -p $project -f $composeFile port redis 6379)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-05 redis port (exit=$dockerExit)" }
    $rawRedisBinding = ($redisLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawRedisBinding) { throw "failed to discover P1-05 redis port (no binding line)" }
    $rawRedisBinding = $rawRedisBinding.Trim()
    if ($rawRedisBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-05 redis binding is not loopback: $rawRedisBinding"
    }
    $actualRedisPort = [int]$Matches.port
    if ($actualRedisPort -ne $reservedRedisPort) {
        throw "P1-05 redis bound to $actualRedisPort but reserved $reservedRedisPort"
    }

    $authRedisLines = @(& $dockerExe compose -p $project -f $composeFile port redis_auth 6379)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-05 auth redis port (exit=$dockerExit)" }
    $rawAuthRedisBinding = ($authRedisLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawAuthRedisBinding) { throw "failed to discover P1-05 auth redis port (no binding line)" }
    $rawAuthRedisBinding = $rawAuthRedisBinding.Trim()
    if ($rawAuthRedisBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-05 auth redis binding is not loopback: $rawAuthRedisBinding"
    }
    $actualAuthRedisPort = [int]$Matches.port
    if ($actualAuthRedisPort -ne $reservedAuthRedisPort) {
        throw "P1-05 auth redis bound to $actualAuthRedisPort but reserved $reservedAuthRedisPort"
    }

    Write-Host "Bindings verified: pg=$actualPgPort redis=$actualRedisPort authRedis=$actualAuthRedisPort" -ForegroundColor Cyan

    $pgIdLines = @(& $dockerExe compose -p $project -f $composeFile ps -q postgres)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-05 postgres container id (exit=$dockerExit)" }
    $containerId = ($pgIdLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $containerId) { throw "failed to discover P1-05 postgres container id (no id line)" }
    $containerId = $containerId.Trim()
    if (-not $containerId) { throw "failed to discover P1-05 postgres container id (empty id)" }
    & $dockerExe exec -i $containerId psql -U postgres -c "SELECT 1 FROM pg_database WHERE datname = '$testDatabase';" | Out-Null
    $dbExists = & $dockerExe exec -i $containerId psql -U postgres -tAc "SELECT COUNT(*) FROM pg_database WHERE datname = '$testDatabase';"
    if ($dbExists.Trim() -eq "0") {
        & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
        if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }
    }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$actualPgPort/$testDatabase"
    $env:REDIS_URL = "redis://127.0.0.1:$actualRedisPort/0"
    $env:AUTH_REDIS_URL = "redis://:p105testpass@127.0.0.1:$actualAuthRedisPort/0"
    $env:P1_05_DOCKER_EXE = $dockerExe
    $env:P1_05_COMPOSE_FILE = $composeFile
    $env:P1_05_COMPOSE_PROJECT = $project

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== Ruff check (P1-05 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff check failed" }

    Write-Host "=== Ruff format check (P1-05 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff format --check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed" }

    Write-Host "=== compileall (P1-05 production + test modules) ===" -ForegroundColor Cyan
    & $pyExe -m compileall -q app/memory_core/redis_hot_handler.py app/memory_core/redis_hot_compose.py app/memory_core/repositories/redis_hot/ tests/unit/test_redis_hot_state.py tests/integration/p1_05_redis_hot/
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

    Write-Host "=== P1-05 unit tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\unit\test_redis_hot_state.py
    if ($LASTEXITCODE -ne 0) { throw "P1-05 unit tests failed" }

    Write-Host "=== Targeted high-risk Docker gates ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v "tests\integration\p1_05_redis_hot\test_redis_hot_state_integration.py::TestConnectionLossRecovery::test_real_docker_stop_start_recovery" "tests\integration\p1_05_redis_hot\test_redis_hot_state_integration.py::TestCompositionRoot::test_preflight_before_any_claim"
    if ($LASTEXITCODE -ne 0) { throw "Targeted high-risk Docker gates failed" }

    Write-Host "=== P1-05 integration tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\integration\p1_05_redis_hot\
    if ($LASTEXITCODE -ne 0) { throw "P1-05 integration tests failed" }

    Write-Host "All P1-05 checks passed." -ForegroundColor Green
} catch {
    $testFailure = $_
    # Do NOT call Write-Error here: $ErrorActionPreference = "Stop"
    # would throw a second exception and mask the original failure.
    Write-Warning "Test failure: $_"
} finally {
    Invoke-Cleanup
    try {
        Verify-ZeroResources
    } catch {
        $script:cleanupFailed = $true
        # Do NOT call Write-Error here: $ErrorActionPreference = "Stop"
        # would throw and mask the original test failure.
        Write-Warning "Cleanup verification threw: $_"
    }
    if ($testFailure -or $cleanupFailed) {
        $messages = @()
        if ($testFailure) { $messages += "test failure: $testFailure" }
        if ($cleanupFailed) { $messages += "cleanup failure (see warnings above)" }
        throw ($messages -join "; ")
    }
}
