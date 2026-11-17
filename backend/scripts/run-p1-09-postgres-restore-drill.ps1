param(
    [switch]$ConfirmIsolatedRestoreTest
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmIsolatedRestoreTest) {
    throw "Pass -ConfirmIsolatedRestoreTest to run the isolated PostgreSQL restore drill."
}
$environmentValues = @($env:ENVIRONMENT, $env:APP_ENV, $env:APP_ENVIRONMENT)
if ($environmentValues | Where-Object { $_ -and $_.Trim() -ieq "production" }) {
    throw "P1-09 restore drill refuses production environments."
}

function Find-DockerExe {
    $candidates = @()
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates += $command.Source }
    $candidates += Join-Path $env:LocalAppData "Docker\wsl\docker.exe"
    $candidates += Join-Path $env:LocalAppData "Programs\DockerDesktop\resources\bin\docker.exe"
    $candidates += Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "docker.exe not found in PATH or Docker Desktop install locations."
}

function Invoke-Psql {
    param(
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$Query
    )
    $result = & $dockerExe exec -i $containerId psql -X -v ON_ERROR_STOP=1 -U postgres -d $Database -tAc $Query
    if ($LASTEXITCODE -ne 0) { throw "Isolated PostgreSQL query failed." }
    return (($result | Out-String).Trim())
}

$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.migration-test.yml"
$project = "esrag-restoretest-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$sourceDatabase = "esragpro_p1_01_test"
$restoreDatabase = "esrag_p1_09_restore_$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$dumpPath = "/tmp/$project.dump"
$previousLocation = Get-Location
$originalPath = $env:PATH
$hadMigrationMode = Test-Path Env:ESRAGPRO_MIGRATION_TEST_MODE
$previousMigrationMode = $env:ESRAGPRO_MIGRATION_TEST_MODE
$hadDatabaseUrl = Test-Path Env:DATABASE_URL
$previousDatabaseUrl = $env:DATABASE_URL
$composeStarted = $false
$restoreDatabaseCreated = $false
$dockerExe = $null
$containerId = $null
$drillSucceeded = $false
$cleanupErrors = [System.Collections.Generic.List[string]]::new()

try {
    $dockerExe = Find-DockerExe
    $env:PATH = "$(Split-Path -Parent $dockerExe);$originalPath"
    if (-not (Test-Path -LiteralPath $composeFile)) { throw "Migration-test Compose file is missing." }
    Set-Location -LiteralPath $backendRoot

    $composeStarted = $true
    & $dockerExe compose -p $project -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "Isolated PostgreSQL service failed to start." }

    $bindingLines = @(& $dockerExe compose -p $project -f $composeFile port postgres 5432)
    if ($LASTEXITCODE -ne 0 -or $bindingLines.Count -ne 1) {
        throw "Could not discover exactly one isolated PostgreSQL port binding."
    }
    $binding = $bindingLines[0].Trim()
    if ($binding -notmatch '^127\.0\.0\.1:(?<port>\d+)$') {
        throw "Isolated PostgreSQL binding is not loopback."
    }
    $port = $Matches.port

    $containerLines = @(& $dockerExe compose -p $project -f $composeFile ps -q postgres)
    if ($LASTEXITCODE -ne 0 -or $containerLines.Count -ne 1 -or -not $containerLines[0].Trim()) {
        throw "Could not identify the single PostgreSQL container owned by this Compose project."
    }
    $containerId = $containerLines[0].Trim()

    $env:ESRAGPRO_MIGRATION_TEST_MODE = "1"
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:$port/$sourceDatabase"
    & .\.venv\Scripts\alembic.exe upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic migration to head failed on the isolated source database." }
    $sourceHead = Invoke-Psql -Database $sourceDatabase -Query "SELECT version_num FROM alembic_version ORDER BY version_num;"
    if (-not $sourceHead) { throw "The isolated source database has no Alembic revision." }

    $seed = "p1-09-restore-sentinel-v1"
    $seedSql = "CREATE TABLE IF NOT EXISTS public.p1_09_restore_sentinel (id integer PRIMARY KEY, payload text NOT NULL); TRUNCATE public.p1_09_restore_sentinel; INSERT INTO public.p1_09_restore_sentinel (id, payload) VALUES (1, '$seed');"
    Invoke-Psql -Database $sourceDatabase -Query $seedSql | Out-Null
    $sourceEvidence = Invoke-Psql -Database $sourceDatabase -Query "SELECT COUNT(*) || ':' || md5(string_agg(id::text || ':' || payload, ',' ORDER BY id)) FROM public.p1_09_restore_sentinel;"

    & $dockerExe exec -i $containerId pg_dump -U postgres --no-owner --no-acl -Fc -f $dumpPath $sourceDatabase
    if ($LASTEXITCODE -ne 0) { throw "Compressed logical PostgreSQL dump failed." }
    Invoke-Psql -Database "postgres" -Query "CREATE DATABASE $restoreDatabase TEMPLATE template0;" | Out-Null
    $restoreDatabaseCreated = $true
    & $dockerExe exec -i $containerId pg_restore -U postgres --exit-on-error --no-owner --no-acl -d $restoreDatabase $dumpPath
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL logical restore failed." }

    $restoredHead = Invoke-Psql -Database $restoreDatabase -Query "SELECT version_num FROM alembic_version ORDER BY version_num;"
    $restoredEvidence = Invoke-Psql -Database $restoreDatabase -Query "SELECT COUNT(*) || ':' || md5(string_agg(id::text || ':' || payload, ',' ORDER BY id)) FROM public.p1_09_restore_sentinel;"
    if ($sourceHead -ne $restoredHead) { throw "Restored Alembic head does not match the source." }
    if ($sourceEvidence -ne $restoredEvidence) { throw "Restored sentinel row count or content hash does not match the source." }

    Write-Host "P1-09 restore integrity verified: head=$sourceHead rows-hash=$sourceEvidence"
    $drillSucceeded = $true
}
finally {
    if ($dockerExe -and $containerId) {
        if ($restoreDatabaseCreated) {
            try {
                $terminateQuery = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$restoreDatabase' AND pid <> pg_backend_pid();"
                Invoke-Psql -Database "postgres" -Query $terminateQuery | Out-Null
                Invoke-Psql -Database "postgres" -Query "DROP DATABASE IF EXISTS $restoreDatabase;" | Out-Null
            } catch { $cleanupErrors.Add("restore database cleanup failed") }
        }
        try {
            & $dockerExe exec -i $containerId rm -f $dumpPath
            if ($LASTEXITCODE -ne 0) { $cleanupErrors.Add("dump cleanup failed") }
        } catch { $cleanupErrors.Add("dump cleanup failed") }
    }
    if ($composeStarted -and $dockerExe) {
        try {
            & $dockerExe compose -p $project -f $composeFile down -v --remove-orphans
            if ($LASTEXITCODE -ne 0) { $cleanupErrors.Add("Compose resource cleanup failed") }
        } catch { $cleanupErrors.Add("Compose resource cleanup failed") }
    }
    if ($hadMigrationMode) { $env:ESRAGPRO_MIGRATION_TEST_MODE = $previousMigrationMode }
    else { Remove-Item Env:ESRAGPRO_MIGRATION_TEST_MODE -ErrorAction SilentlyContinue }
    if ($hadDatabaseUrl) { $env:DATABASE_URL = $previousDatabaseUrl }
    else { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue }
    $env:PATH = $originalPath
    Set-Location -LiteralPath $previousLocation
}

if ($cleanupErrors.Count -gt 0) {
    throw "P1-09 cleanup could not be proven complete: $($cleanupErrors -join ', ')."
}
if (-not $drillSucceeded) { throw "P1-09 PostgreSQL restore drill did not complete successfully." }
