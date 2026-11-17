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
$project = "esragpro-p1-02-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$testDatabase = "esragpro_p1_02_test"
$previousLocation = Get-Location

try {
    Set-Location -LiteralPath $backendRoot

    Write-Host "=== starting isolated PostgreSQL for P1-02 ===" -ForegroundColor Cyan
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-02 repository test postgres failed to start" }

    $rawBinding = & $dockerExe compose -p $project -f $composeFile port postgres 5432
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) { throw "failed to discover P1-02 repository test postgres port" }
    $binding = ($rawBinding | Select-Object -First 1).Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-02 repository test postgres binding is not loopback"
    }

    # Create the P1-02 isolated test database on the shared compose stack.
    $containerId = (& $dockerExe compose -p $project -f $composeFile ps -q postgres | Select-Object -First 1).Trim()
    if (-not $containerId) { throw "failed to discover P1-02 postgres container id" }
    & $dockerExe exec -i $containerId psql -U postgres -c "CREATE DATABASE $testDatabase;"
    if ($LASTEXITCODE -ne 0) { throw "failed to create $testDatabase" }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$($Matches.port)/$testDatabase"

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== P1-02 unit tests (mapping) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\unit\test_postgres_memory_repository.py tests\unit\test_postgres_row_mapping.py
    if ($LASTEXITCODE -ne 0) { throw "P1-02 mapping unit tests failed" }

    Write-Host "=== P1-02 unit tests (time compatibility) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\unit\test_unpermissioned_time.py
    if ($LASTEXITCODE -ne 0) { throw "P1-02 time unit tests failed" }

    Write-Host "=== P1-02 integration tests (write) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_02_repository\test_write.py
    if ($LASTEXITCODE -ne 0) { throw "P1-02 integration write tests failed" }

    Write-Host "=== P1-02 integration tests (query) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_02_repository\test_query.py
    if ($LASTEXITCODE -ne 0) { throw "P1-02 integration query tests failed" }

    Write-Host "=== P1-02 integration tests (update/count) ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\p1_02_repository\test_update_count.py
    if ($LASTEXITCODE -ne 0) { throw "P1-02 integration update/count tests failed" }

    Write-Host "All P1-02 checks passed." -ForegroundColor Green
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
