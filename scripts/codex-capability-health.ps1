param(
    [switch]$Json
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CodexConfig = Join-Path $env:USERPROFILE ".codex\config.toml"
$GitBashExe = "D:\Git\bin\bash.exe"
$DockerExe = "C:\Users\11239\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
$CargoExe = "E:\rust\.cargo\bin\cargo.exe"
$RustAnalyzerExe = "E:\rust\.cargo\bin\rust-analyzer.exe"
$McpExe = Join-Path $ProjectRoot "esragpro-mcp\target\release\esragpro-mcp.exe"
$CodeGraphDb = Join-Path $ProjectRoot ".codegraph\codegraph.db"
$CodeReviewGraphExe = "E:\01myother\.venv-crg\Scripts\code-review-graph.exe"

$Checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Details = "",
        [bool]$Required = $true
    )

    $script:Checks.Add([pscustomobject]@{
        name = $Name
        ok = $Ok
        required = $Required
        details = $Details
    })
}

function Test-RequiredPath {
    param(
        [string]$Name,
        [string]$Path,
        [bool]$Required = $true
    )

    Add-Check -Name $Name -Ok (Test-Path -LiteralPath $Path) -Details $Path -Required $Required
}

Test-RequiredPath "git_bash.exe" $GitBashExe
Test-RequiredPath "docker.exe" $DockerExe
Test-RequiredPath "cargo.exe" $CargoExe
Test-RequiredPath "rust-analyzer.exe" $RustAnalyzerExe
Test-RequiredPath "esragpro-mcp.exe" $McpExe
Test-RequiredPath "codegraph.db" $CodeGraphDb
Test-RequiredPath "code-review-graph.exe" $CodeReviewGraphExe

if (Test-Path -LiteralPath $CodexConfig) {
    $ConfigText = Get-Content -LiteralPath $CodexConfig -Raw
    Add-Check "codex.config" $true $CodexConfig
    Add-Check "codex.git_bash_enabled" ($ConfigText -match '(?s)\[plugins\."omo@sisyphuslabs"\.mcp_servers\.git_bash\]\s*enabled\s*=\s*true') "OMO git_bash MCP enabled"
    Add-Check "codex.git_bash_path_env" ($ConfigText -match [regex]::Escape("OMO_CODEX_GIT_BASH_PATH = 'D:\Git\bin\bash.exe'")) "OMO_CODEX_GIT_BASH_PATH"
    Add-Check "codex.esragpro_mcp_registered" ($ConfigText -match '\[mcp_servers\.esragpro-memory\]') "mcp_servers.esragpro-memory"
    Add-Check "codex.esragpro_mcp_env" ($ConfigText -match 'ESRAGPRO_BACKEND_URL\s*=\s*"http://localhost:8000/api/v1"') "ESRAGPRO_BACKEND_URL"
} else {
    Add-Check "codex.config" $false $CodexConfig
}

if (Test-Path -LiteralPath $DockerExe) {
    try {
        $DockerVersion = & $DockerExe version --format '{{.Server.Version}}' 2>&1
        Add-Check "docker.engine" ($LASTEXITCODE -eq 0) ($DockerVersion -join "`n")
    } catch {
        Add-Check "docker.engine" $false $_.Exception.Message
    }

    try {
        $Containers = & $DockerExe ps --format '{{.Names}}|{{.Status}}' 2>&1
        $ContainerText = $Containers -join "`n"
        Add-Check "docker.esragpro-es" ($ContainerText -match 'esragpro-es\|Up') $ContainerText
        Add-Check "docker.esragpro-postgres" ($ContainerText -match 'esragpro-postgres\|Up') $ContainerText
    } catch {
        Add-Check "docker.containers" $false $_.Exception.Message
    }

    try {
        $PgReady = & $DockerExe exec esragpro-postgres pg_isready -U postgres -d esragpro 2>&1
        Add-Check "postgres.pg_isready" ($LASTEXITCODE -eq 0) ($PgReady -join "`n")
    } catch {
        Add-Check "postgres.pg_isready" $false $_.Exception.Message
    }
}

try {
    $EsHealth = Invoke-RestMethod -Uri "http://localhost:9200/_cluster/health" -TimeoutSec 5
    $EsOk = $null -ne $EsHealth.status -and @("green", "yellow") -contains [string]$EsHealth.status
    Add-Check "elasticsearch.health" $EsOk ("status=" + $EsHealth.status)
} catch {
    Add-Check "elasticsearch.health" $false $_.Exception.Message
}

try {
    $BackendHealth = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 3
    Add-Check "backend.health" $true ($BackendHealth | ConvertTo-Json -Compress) $false
} catch {
    Add-Check "backend.health" $false $_.Exception.Message $false
}

if (Test-Path -LiteralPath $CargoExe) {
    try {
        $CargoVersion = & $CargoExe --version 2>&1
        Add-Check "cargo.version" ($LASTEXITCODE -eq 0) ($CargoVersion -join "`n")
    } catch {
        Add-Check "cargo.version" $false $_.Exception.Message
    }
}

if (Test-Path -LiteralPath $McpExe) {
    try {
        $McpHelp = & $McpExe --help 2>&1
        Add-Check "esragpro-mcp.help" ($LASTEXITCODE -eq 0 -and (($McpHelp -join "`n") -match 'stdio')) "stdio command available"
    } catch {
        Add-Check "esragpro-mcp.help" $false $_.Exception.Message
    }
}

if (Test-Path -LiteralPath $CodeReviewGraphExe) {
    try {
        $CrgStatusRaw = & $CodeReviewGraphExe status --repo $ProjectRoot --json 2>&1
        $CrgStatus = ($CrgStatusRaw -join "`n") | ConvertFrom-Json
        $CrgOk = $LASTEXITCODE -eq 0 -and [int]$CrgStatus.nodes -gt 0 -and [int]$CrgStatus.edges -gt 0
        Add-Check "code-review-graph.index" $CrgOk ("nodes=" + $CrgStatus.nodes + " edges=" + $CrgStatus.edges + " files=" + $CrgStatus.files)
    } catch {
        Add-Check "code-review-graph.index" $false $_.Exception.Message
    }
}

$RequiredFailures = @($Checks | Where-Object { $_.required -and -not $_.ok })

if ($Json) {
    [pscustomobject]@{
        projectRoot = $ProjectRoot
        checkedAt = (Get-Date).ToUniversalTime().ToString("o")
        ok = ($RequiredFailures.Count -eq 0)
        checks = $Checks
    } | ConvertTo-Json -Depth 5
} else {
    $Checks | Sort-Object required, name -Descending | Format-Table name, ok, required, details -AutoSize
    if ($RequiredFailures.Count -eq 0) {
        Write-Host "OK: required Codex/esRAGPro capabilities are available."
    } else {
        Write-Host "FAILED: required capability checks failed:"
        $RequiredFailures | ForEach-Object { Write-Host ("- " + $_.name + ": " + $_.details) }
    }
}

if ($RequiredFailures.Count -eq 0) {
    exit 0
}

exit 1
