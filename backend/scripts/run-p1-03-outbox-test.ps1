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
$project = "esragpro-p1-03-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_03_test"
$previousLocation = Get-Location

try {
    Set-Location -LiteralPath $backendRoot

    Write-Host "=== starting isolated PostgreSQL for P1-03 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-03 outbox test postgres failed to start" }

    $rawBinding = & $dockerExe compose -p $project -f $composeFile port postgres 5432
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) { throw "failed to discover P1-03 outbox test postgres port" }
    $binding = ($rawBinding | Select-Object -First 1).Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-03 outbox test postgres binding is not loopback"
    }

    # Create the P1-03 isolated test database on the shared compose stack.
    $containerId = (& $dockerExe compose -p $project -f $composeFile ps -q postgres | Select-Object -First 1).Trim()
    if (-not $containerId) { throw "failed to discover P1-03 postgres container id" }
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

    Write-Host "=== P1-03 unit tests (outbox helper) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\unit\test_postgres_outbox.py tests\unit\test_postgres_outbox_payload.py
    if ($LASTEXITCODE -ne 0) { throw "P1-03 outbox unit tests failed" }

    Write-Host "=== P1-03 schema integration tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\test_p1_03_memory_outbox_schema.py tests\integration\test_p1_03_outbox_schema_inspection.py
    if ($LASTEXITCODE -ne 0) { throw "P1-03 schema tests failed" }

    Write-Host "=== P1-03 transactional remember tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_03_outbox\test_transactional_remember.py
    if ($LASTEXITCODE -ne 0) { throw "P1-03 transactional remember tests failed" }

    Write-Host "=== P1-03 outbox contract tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_03_outbox\test_outbox_contract.py
    if ($LASTEXITCODE -ne 0) { throw "P1-03 outbox contract tests failed" }

    Write-Host "=== P1-02 repository regression ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_02_repository\
    if ($LASTEXITCODE -ne 0) { throw "P1-02 repository regression failed" }

    Write-Host "All P1-03 checks passed." -ForegroundColor Green
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
