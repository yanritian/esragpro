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

$dockerExe = Find-DockerExe
$dockerDir = Split-Path -Parent $dockerExe
$originalPath = $env:PATH
$env:PATH = "$dockerDir;$originalPath"

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.migration-test.yml"
$project = "esragpro-p1-04-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_04_test"
$previousLocation = Get-Location

try {
    Set-Location -LiteralPath $backendRoot

    Write-Host "=== starting isolated PostgreSQL for P1-04 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-04 worker test postgres failed to start" }

    $rawBinding = & $dockerExe compose -p $project -f $composeFile port postgres 5432
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) { throw "failed to discover P1-04 worker test postgres port" }
    $binding = ($rawBinding | Select-Object -First 1).Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-04 worker test postgres binding is not loopback"
    }

    # Create the P1-04 isolated test database
    $containerId = (& $dockerExe compose -p $project -f $composeFile ps -q postgres | Select-Object -First 1).Trim()
    if (-not $containerId) { throw "failed to discover P1-04 postgres container id" }
    & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
    if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$($Matches.port)/$testDatabase"

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== alembic current ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe current
    if ($LASTEXITCODE -ne 0) { throw "alembic current failed" }

    Write-Host "=== P1-04 unit tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v `
        tests\unit\test_projection_worker_contracts.py `
        tests\unit\test_projection_worker_retry.py `
        tests\unit\test_projection_worker_retry_edge_cases.py `
        tests\unit\test_worker_constructor_validation.py `
        tests\unit\test_worker_runonce_behavior.py `
        tests\unit\test_payload_immutability.py `
        tests\unit\test_worker_success_completion.py `
        tests\unit\test_worker_failures_retry.py `
        tests\unit\test_worker_lease_errors.py `
        tests\unit\test_worker_concurrency_advanced.py `
        tests\unit\test_worker_invariants.py
    if ($LASTEXITCODE -ne 0) { throw "P1-04 unit tests failed" }

    Write-Host "=== P1-04 integration tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_04_worker\
    if ($LASTEXITCODE -ne 0) { throw "P1-04 integration tests failed" }

    Write-Host "=== P1-03 outbox regression ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_03_outbox\
    if ($LASTEXITCODE -ne 0) { throw "P1-03 outbox regression failed" }

    Write-Host "=== P1-03 schema inspection regression ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\test_p1_03_memory_outbox_schema.py tests\integration\test_p1_03_outbox_schema_inspection.py
    if ($LASTEXITCODE -ne 0) { throw "P1-03 schema inspection regression failed" }

    Write-Host "=== P1-02 repository regression ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_02_repository\
    if ($LASTEXITCODE -ne 0) { throw "P1-02 repository regression failed" }

    Write-Host "All P1-04 checks passed." -ForegroundColor Green
}
finally {
    try {
        & $dockerExe compose -p $project -f $composeFile down -v --remove-orphans
    } catch {
        Write-Warning "Cleanup compose down failed: $_"
    }
    Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    $env:PATH = $originalPath
    Set-Location -LiteralPath $previousLocation
}
