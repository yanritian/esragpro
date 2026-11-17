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
# Prepend Docker directory so docker-credential-desktop is discoverable.
$env:PATH = "$dockerDir;$originalPath"

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.migration-test.yml"
$project = "esragpro-p1-01-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$previousLocation = Get-Location

try {
    Set-Location -LiteralPath $backendRoot

    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "P1-01 migration test postgres failed to start" }

    $rawBinding = & $dockerExe compose -p $project -f $composeFile port postgres 5432
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) { throw "failed to discover P1-01 migration test postgres port" }
    $binding = ($rawBinding | Select-Object -First 1).Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "P1-01 migration test postgres binding is not loopback"
    }

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$($Matches.port)/esragpro_p1_01_test"

    Write-Host "=== alembic heads ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe heads
    if ($LASTEXITCODE -ne 0) { throw "alembic heads failed" }

    Write-Host "=== alembic upgrade head ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

    Write-Host "=== alembic current ===" -ForegroundColor Cyan
    & .\.venv\Scripts\alembic.exe current
    if ($LASTEXITCODE -ne 0) { throw "alembic current failed" }

    Write-Host "=== pytest P1-01 schema tests ===" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pytest -v tests\integration\test_p1_01_memory_schema.py
    if ($LASTEXITCODE -ne 0) { throw "P1-01 migration schema tests failed" }

    Write-Host "All P1-01 checks passed." -ForegroundColor Green
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
