$ErrorActionPreference = "Stop"

$backendRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$previousLocation = Get-Location

$runnerNames = @(
    "run-p1-01-migration-test.ps1",
    "run-p1-02-repository-test.ps1",
    "run-p1-03-outbox-test.ps1",
    "run-p1-04-worker-test.ps1",
    "run-p1-05-redis-hot-state-test.ps1",
    "run-p1-06-elasticsearch-test.ps1",
    "run-p1-07-neo4j-test.ps1",
    "run-p1-08-minio-test.ps1"
)

$runnerPaths = @($runnerNames | ForEach-Object { Join-Path $PSScriptRoot $_ })

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "backend Python executable not found: $pythonExe"
}

foreach ($runner in $runnerPaths) {
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw "required phase runner not found: $runner"
    }
}

$powerShellCommand = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue
if (-not $powerShellCommand) {
    $powerShellCommand = Get-Command powershell -CommandType Application -ErrorAction SilentlyContinue
}
if (-not $powerShellCommand) {
    throw "PowerShell executable not found; cannot isolate phase runners"
}
$powerShellExe = $powerShellCommand.Source
if (-not $powerShellExe) {
    $powerShellExe = $powerShellCommand.Path
}

try {
    Set-Location -LiteralPath $backendRoot

    Write-Host "=== backend unit/API lane ===" -ForegroundColor Cyan
    & $pythonExe -m pytest -v tests\unit tests\api
    if ($LASTEXITCODE -ne 0) { throw "backend unit/API lane failed with exit code $LASTEXITCODE" }

    foreach ($runner in $runnerPaths) {
        $phaseName = [System.IO.Path]::GetFileNameWithoutExtension($runner)
        Write-Host "=== $phaseName (isolated PowerShell process) ===" -ForegroundColor Cyan
        & $powerShellExe -NoProfile -ExecutionPolicy Bypass -File $runner
        if ($LASTEXITCODE -ne 0) { throw "$phaseName failed with exit code $LASTEXITCODE" }
    }

    Write-Host "Backend release gate passed." -ForegroundColor Green
}
finally {
    Set-Location -LiteralPath $previousLocation
}
