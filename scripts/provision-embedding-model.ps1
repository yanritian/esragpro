[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("acquire", "import", "verify")]
    [string]$Command = "verify",
    [string]$ProjectName,
    [string]$ComposeFile,
    [string]$HostSource,
    [switch]$Secure
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ComposeFile) {
    if ($Secure) {
        $ComposeFile = "docker-compose.secure.yml"
    } else {
        $ComposeFile = "docker-compose.yml"
    }
}
$composePath = Join-Path $repoRoot $ComposeFile
if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
    throw "Compose file not found: $composePath"
}

$composeArgs = @()
if ($ProjectName) { $composeArgs += @("-p", $ProjectName) }
$composeArgs += @("-f", $composePath, "run", "--rm", "--no-deps")
if ($HostSource) {
    $resolvedSource = (Resolve-Path -LiteralPath $HostSource).Path
    $composeArgs += @("-v", "$resolvedSource`:/mnt/model-source:ro")
}
$composeArgs += @(
    "embedding-model-init",
    $Command,
    "--lock",
    "/app/model-locks/BAAI--bge-m3.json",
    "--model-root",
    "/models"
)
if ($Command -eq "import") {
    if (-not $HostSource) { throw "-HostSource is required for import" }
    $composeArgs += @("--source", "/mnt/model-source")
}

Push-Location -LiteralPath $repoRoot
try {
    & docker compose @composeArgs
    if ($LASTEXITCODE -ne 0) { throw "embedding-model-init failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
