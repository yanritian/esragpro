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
$composeFile = Join-Path $backendRoot "docker-compose.p1-08-test.yml"
$project = "esragpro-p1-08-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_08_test"
$previousLocation = Get-Location
$pyExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$pytestExe = Join-Path $backendRoot ".venv\Scripts\python.exe"

# --- Verify minio importable -------------------------------------------------
try {
    & $pyExe -c "import minio; print('minio', minio.__version__)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "minio package not importable; run 'uv lock' and reinstall" }
} catch {
    throw "minio package not importable in .venv. Run: cd backend && uv lock && uv sync"
}

# --- Reserve three distinct free loopback ports simultaneously ----------------
$pgListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$minioApiListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$minioConsoleListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
try {
    $reservedPgPort = Reserve-FreeLoopbackPort -Listener $pgListener
    $reservedMinioApiPort = Reserve-FreeLoopbackPort -Listener $minioApiListener
    $reservedMinioConsolePort = Reserve-FreeLoopbackPort -Listener $minioConsoleListener

    $portsSet = [System.Collections.Generic.HashSet[int]]::new()
    $null = $portsSet.Add($reservedPgPort)
    $null = $portsSet.Add($reservedMinioApiPort)
    $null = $portsSet.Add($reservedMinioConsolePort)
    if ($portsSet.Count -ne 3) {
        throw "Port reservation returned duplicate ports: pg=$reservedPgPort api=$reservedMinioApiPort console=$reservedMinioConsolePort"
    }
} finally {
    $pgListener.Stop()
    $minioApiListener.Stop()
    $minioConsoleListener.Stop()
}
Write-Host "Reserved ports: pg=$reservedPgPort minio-api=$reservedMinioApiPort minio-console=$reservedMinioConsolePort" -ForegroundColor Cyan

# --- P1-08 allowlist for Ruff ------------------------------------------------
$ruffAllowlist = @(
    "app\memory_core\minio_handler.py",
    "app\memory_core\minio_compose.py",
    "app\memory_core\minio_worker_entrypoint.py",
    "app\memory_core\repositories\minio_projection\__init__.py",
    "app\memory_core\repositories\minio_projection\adapter.py",
    "app\memory_core\repositories\minio_projection\models.py",
    "tests\unit\test_minio_projection_payload.py",
    "tests\unit\test_minio_projection_adapter.py",
    "tests\unit\test_minio_projection_handler.py",
    "tests\unit\test_minio_worker_entrypoint.py",
    "tests\integration\p1_08_minio\__init__.py",
    "tests\integration\p1_08_minio\conftest.py",
    "tests\integration\p1_08_minio\test_minio_projection_integration.py"
)

# --- Saved previous env values for restoration --------------------------------
$prevPgPort = $env:P1_08_POSTGRES_PORT
$prevMinioApiPort = $env:P1_08_MINIO_API_PORT
$prevMinioConsolePort = $env:P1_08_MINIO_CONSOLE_PORT
$prevMinioAccessKey = $env:P1_08_MINIO_ACCESS_KEY
$prevMinioSecretKey = $env:P1_08_MINIO_SECRET_KEY
$prevMigrationMode = $env:ESRAGPRO_MIGRATION_TEST_MODE
$prevDatabaseUrl = $env:DATABASE_URL
$prevMinioEndpoint = $env:MINIO_ENDPOINT
$prevMinioAccessKeyEnv = $env:MINIO_ACCESS_KEY
$prevMinioSecretKeyEnv = $env:MINIO_SECRET_KEY
$prevDockerExe = $env:P1_08_DOCKER_EXE
$prevComposeFile = $env:P1_08_COMPOSE_FILE
$prevComposeProject = $env:P1_08_COMPOSE_PROJECT

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

    if ($null -eq $prevPgPort) { Remove-Item Env:P1_08_POSTGRES_PORT -ErrorAction SilentlyContinue } else { $env:P1_08_POSTGRES_PORT = $prevPgPort }
    if ($null -eq $prevMinioApiPort) { Remove-Item Env:P1_08_MINIO_API_PORT -ErrorAction SilentlyContinue } else { $env:P1_08_MINIO_API_PORT = $prevMinioApiPort }
    if ($null -eq $prevMinioConsolePort) { Remove-Item Env:P1_08_MINIO_CONSOLE_PORT -ErrorAction SilentlyContinue } else { $env:P1_08_MINIO_CONSOLE_PORT = $prevMinioConsolePort }
    if ($null -eq $prevMinioAccessKey) { Remove-Item Env:P1_08_MINIO_ACCESS_KEY -ErrorAction SilentlyContinue } else { $env:P1_08_MINIO_ACCESS_KEY = $prevMinioAccessKey }
    if ($null -eq $prevMinioSecretKey) { Remove-Item Env:P1_08_MINIO_SECRET_KEY -ErrorAction SilentlyContinue } else { $env:P1_08_MINIO_SECRET_KEY = $prevMinioSecretKey }
    if ($null -eq $prevMigrationMode) { Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue } else { $env:ESRAGPRO_MIGRATION_TEST_MODE = $prevMigrationMode }
    if ($null -eq $prevDatabaseUrl) { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue } else { $env:DATABASE_URL = $prevDatabaseUrl }
    if ($null -eq $prevMinioEndpoint) { Remove-Item Env:MINIO_ENDPOINT -ErrorAction SilentlyContinue } else { $env:MINIO_ENDPOINT = $prevMinioEndpoint }
    if ($null -eq $prevMinioAccessKeyEnv) { Remove-Item Env:MINIO_ACCESS_KEY -ErrorAction SilentlyContinue } else { $env:MINIO_ACCESS_KEY = $prevMinioAccessKeyEnv }
    if ($null -eq $prevMinioSecretKeyEnv) { Remove-Item Env:MINIO_SECRET_KEY -ErrorAction SilentlyContinue } else { $env:MINIO_SECRET_KEY = $prevMinioSecretKeyEnv }
    if ($null -eq $prevDockerExe) { Remove-Item Env:P1_08_DOCKER_EXE -ErrorAction SilentlyContinue } else { $env:P1_08_DOCKER_EXE = $prevDockerExe }
    if ($null -eq $prevComposeFile) { Remove-Item Env:P1_08_COMPOSE_FILE -ErrorAction SilentlyContinue } else { $env:P1_08_COMPOSE_FILE = $prevComposeFile }
    if ($null -eq $prevComposeProject) { Remove-Item Env:P1_08_COMPOSE_PROJECT -ErrorAction SilentlyContinue } else { $env:P1_08_COMPOSE_PROJECT = $prevComposeProject }

    $env:PATH = $originalPath
    Set-Location -LiteralPath $previousLocation
}

# --- Verify zero Docker resources for project ---------------------------------
function Verify-ZeroResources {
    # FIRST (authoritative) check: inspect resources WITHOUT modifying them.
    # If any project-labeled resources remain, this check records them and
    # fails the gate.  No force-removal happens before the pass/fail decision.
    $containers = @(& $dockerExe ps -a --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    $volumes = @(& $dockerExe volume ls --filter "label=com.docker.compose.project=$project" --format "{{.Name}}" 2>$null)
    $networks = @(& $dockerExe network ls --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null)
    $containers = $containers | Where-Object { $_ }
    $volumes = $volumes | Where-Object { $_ }
    $networks = $networks | Where-Object { $_ }
    $cCount = $containers.Count
    $vCount = $volumes.Count
    $nCount = $networks.Count
    Write-Host "First (authoritative) post-cleanup resource counts: containers=$cCount volumes=$vCount networks=$nCount" -ForegroundColor Cyan
    if ($cCount -gt 0) {
        Write-Host "Residual container IDs:" -ForegroundColor Yellow
        $containers | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    }
    if ($vCount -gt 0) {
        Write-Host "Residual volume names:" -ForegroundColor Yellow
        $volumes | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    }
    if ($nCount -gt 0) {
        Write-Host "Residual network IDs:" -ForegroundColor Yellow
        $networks | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    }

    $leaked = ($cCount -gt 0 -or $vCount -gt 0 -or $nCount -gt 0)

    # Best-effort cleanup AFTER the failure has been latched.
    # This does not mask the first-check result.
    if ($leaked) {
        Write-Warning "Attempting best-effort force-removal of leaked resources..."
        if ($cCount -gt 0) {
            & $dockerExe rm -f @containers 2>$null
        }
        if ($vCount -gt 0) {
            & $dockerExe volume rm -f @volumes 2>$null
        }
        if ($nCount -gt 0) {
            & $dockerExe network rm @networks 2>$null
        }
    }

    # SECOND independent check: evidence of cleanup (not used for pass/fail
    # of the first check, but reported for diagnostic purposes).
    $c2 = @(& $dockerExe ps -a --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null) | Where-Object { $_ }
    $v2 = @(& $dockerExe volume ls --filter "label=com.docker.compose.project=$project" --format "{{.Name}}" 2>$null) | Where-Object { $_ }
    $n2 = @(& $dockerExe network ls --filter "label=com.docker.compose.project=$project" --format "{{.ID}}" 2>$null) | Where-Object { $_ }
    $c2Count = $c2.Count
    $v2Count = $v2.Count
    $n2Count = $n2.Count
    Write-Host "Second independent verification: containers=$c2Count volumes=$v2Count networks=$n2Count" -ForegroundColor Cyan

    # The gate is driven by the FIRST (authoritative) check.
    if ($leaked) {
        throw "Post-cleanup verification FAILED (first check): $cCount containers, $vCount volumes, $nCount networks remain"
    }
    if ($c2Count -ne 0 -or $v2Count -ne 0 -or $n2Count -ne 0) {
        throw "Post-cleanup verification FAILED (second check): $c2Count containers, $v2Count volumes, $n2Count networks remain after force-removal"
    }
}

$testFailure = $null
try {
    Set-Location -LiteralPath $backendRoot

    $env:P1_08_POSTGRES_PORT = [string]$reservedPgPort
    $env:P1_08_MINIO_API_PORT = [string]$reservedMinioApiPort
    $env:P1_08_MINIO_CONSOLE_PORT = [string]$reservedMinioConsolePort
    $env:P1_08_MINIO_ACCESS_KEY = "minioadmin"
    $env:P1_08_MINIO_SECRET_KEY = "minioadmin"

    Write-Host "=== starting isolated PostgreSQL and MinIO for P1-08 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-08 test services failed to start" }

    # Validate actual bindings match reserved ports exactly.
    $pgLines = @(& $dockerExe compose -p $project -f $composeFile port postgres 5432)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-08 postgres port (exit=$dockerExit)" }
    $rawPgBinding = ($pgLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawPgBinding) { throw "failed to discover P1-08 postgres port (no binding line)" }
    $rawPgBinding = $rawPgBinding.Trim()
    if ($rawPgBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-08 postgres binding is not loopback: $rawPgBinding"
    }
    $actualPgPort = [int]$Matches.port
    if ($actualPgPort -ne $reservedPgPort) {
        throw "P1-08 postgres bound to $actualPgPort but reserved $reservedPgPort"
    }

    $minioApiLines = @(& $dockerExe compose -p $project -f $composeFile port minio 9000)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-08 minio API port (exit=$dockerExit)" }
    $rawApiBinding = ($minioApiLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $rawApiBinding) { throw "failed to discover P1-08 minio API port (no binding line)" }
    $rawApiBinding = $rawApiBinding.Trim()
    if ($rawApiBinding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-08 minio API binding is not loopback: $rawApiBinding"
    }
    $actualApiPort = [int]$Matches.port
    if ($actualApiPort -ne $reservedMinioApiPort) {
        throw "P1-08 minio API bound to $actualApiPort but reserved $reservedMinioApiPort"
    }

    Write-Host "Bindings verified: pg=$actualPgPort minio-api=$actualApiPort" -ForegroundColor Cyan

    $pgIdLines = @(& $dockerExe compose -p $project -f $composeFile ps -q postgres)
    $dockerExit = $LASTEXITCODE
    if ($dockerExit -ne 0) { throw "failed to discover P1-08 postgres container id (exit=$dockerExit)" }
    $containerId = ($pgIdLines | Where-Object { $_ } | Select-Object -First 1)
    if (-not $containerId) { throw "failed to discover P1-08 postgres container id (no id line)" }
    $containerId = $containerId.Trim()
    if (-not $containerId) { throw "failed to discover P1-08 postgres container id (empty id)" }
    & $dockerExe exec -i $containerId psql -U postgres -c "SELECT 1 FROM pg_database WHERE datname = '$testDatabase';" | Out-Null
    $dbExists = & $dockerExe exec -i $containerId psql -U postgres -tAc "SELECT COUNT(*) FROM pg_database WHERE datname = '$testDatabase';"
    if ($dbExists.Trim() -eq "0") {
        & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
        if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }
    }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$actualPgPort/$testDatabase"
    $env:MINIO_ENDPOINT = "127.0.0.1:$actualApiPort"
    $env:MINIO_ACCESS_KEY = "minioadmin"
    $env:MINIO_SECRET_KEY = "minioadmin"
    $env:P1_08_DOCKER_EXE = $dockerExe
    $env:P1_08_COMPOSE_FILE = $composeFile
    $env:P1_08_COMPOSE_PROJECT = $project

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== Ruff check (P1-08 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff check failed" }

    Write-Host "=== Ruff format check (P1-08 allowlist) ===" -ForegroundColor Cyan
    & $pyExe -m ruff format --check @ruffAllowlist
    if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed" }

    Write-Host "=== compileall (P1-08 production + test modules) ===" -ForegroundColor Cyan
    & $pyExe -m compileall -q app/memory_core/minio_handler.py app/memory_core/minio_compose.py app/memory_core/minio_worker_entrypoint.py app/memory_core/repositories/minio_projection/ tests/unit/test_minio_projection_payload.py tests/unit/test_minio_projection_adapter.py tests/unit/test_minio_projection_handler.py tests/unit/test_minio_worker_entrypoint.py tests/integration/p1_08_minio/
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

    Write-Host "=== P1-08 unit tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\unit\test_minio_projection_payload.py tests\unit\test_minio_projection_adapter.py tests\unit\test_minio_projection_handler.py tests\unit\test_minio_worker_entrypoint.py
    if ($LASTEXITCODE -ne 0) { throw "P1-08 unit tests failed" }

    Write-Host "=== P1-08 integration tests ===" -ForegroundColor Cyan
    & $pytestExe -m pytest -v tests\integration\p1_08_minio\
    if ($LASTEXITCODE -ne 0) { throw "P1-08 integration tests failed" }

    Write-Host "All P1-08 checks passed." -ForegroundColor Green
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
