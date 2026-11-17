# WP0 Baseline Verification Script
# Purpose: Verify WP0 completion gates
# Exit code: 0=pass, 1=fail

param(
    [string]$CoreRoot = "D:\dingsun\esRAGPro",
    [string]$McpRoot = "D:\dingsun\esRAGPro\esragpro-mcp"
)

$ErrorActionPreference = "Stop"
$exitCode = 0
$errors = @()

Write-Host "=== WP0 Baseline Verification ===" -ForegroundColor Cyan
Write-Host ""

# 1. Check Git repository status
Write-Host "[1/9] Checking Git repository status..." -ForegroundColor Yellow

$coreGit = git -C $CoreRoot rev-parse --is-inside-work-tree 2>$null
$mcpGit = git -C $McpRoot rev-parse --is-inside-work-tree 2>$null

if ($coreGit -eq "true" -and $mcpGit -eq "true") {
    Write-Host "  OK: Both repositories are Git repos" -ForegroundColor Green
    $VC_MODE = "git"
} elseif ($coreGit -ne "true" -and $mcpGit -ne "true") {
    Write-Host "  WARN: Both repositories are NOT Git repos" -ForegroundColor Yellow
    Write-Host "  INFO: VC_MODE = no_git" -ForegroundColor Yellow
    $VC_MODE = "no_git"
} else {
    Write-Host "  FAIL: Git repository status inconsistent" -ForegroundColor Red
    $errors += "Git repository status inconsistent: Core=$coreGit, MCP=$mcpGit"
    $VC_MODE = "invalid"
    $exitCode = 1
}

# 2. Check 6 ADR documents
Write-Host "[2/9] Checking ADR documents..." -ForegroundColor Yellow

$adrFiles = @(
    "$CoreRoot\docs\adr\0001-system-of-record-and-projections.md",
    "$CoreRoot\docs\adr\0002-enterprise-and-edge-delivery-shapes.md",
    "$CoreRoot\docs\adr\0003-identity-acl-and-trust-boundary.md",
    "$CoreRoot\docs\adr\0004-evaluation-policy-and-release-gates.md",
    "$McpRoot\docs\adr\0001-thin-mcp-gateway.md",
    "$McpRoot\docs\adr\0002-mcp-protocol-sdk-and-auth.md"
)

$adrCount = 0
foreach ($file in $adrFiles) {
    if (Test-Path $file) {
        $adrCount++
        Write-Host "  OK: $file" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: $file (missing)" -ForegroundColor Red
        $errors += "ADR missing: $file"
    }
}

if ($adrCount -eq 6) {
    Write-Host "  OK: All ADR documents exist" -ForegroundColor Green
} else {
    Write-Host "  FAIL: ADR documents incomplete: $adrCount/6" -ForegroundColor Red
    $exitCode = 1
}

# 3. Check audit documents and environment manifest
Write-Host "[3/9] Checking audit documents..." -ForegroundColor Yellow

$auditFiles = @(
    "$CoreRoot\docs\audits\baseline-2026-07-10.md",
    "$CoreRoot\docs\audits\environment-manifest-2026-07-10.json"
)

$auditCount = 0
foreach ($file in $auditFiles) {
    if (Test-Path $file) {
        $auditCount++
        Write-Host "  OK: $file" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: $file (missing)" -ForegroundColor Red
        $errors += "Audit document missing: $file"
    }
}

if ($auditCount -eq 2) {
    Write-Host "  OK: Audit documents complete" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Audit documents incomplete: $auditCount/2" -ForegroundColor Red
    $exitCode = 1
}

# 4. Check 3 JSON Schema files
Write-Host "[4/9] Checking JSON Schema..." -ForegroundColor Yellow

$schemaFiles = @(
    "$McpRoot\docs\contracts\domain-api-v1.schema.json",
    "$McpRoot\docs\contracts\mcp-v1.schema.json",
    "$McpRoot\docs\contracts\principal-context-v1.schema.json"
)

$schemaCount = 0
foreach ($file in $schemaFiles) {
    if (Test-Path $file) {
        $schemaCount++
        Write-Host "  OK: $file" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: $file (missing)" -ForegroundColor Red
        $errors += "Schema missing: $file"
    }
}

if ($schemaCount -eq 3) {
    Write-Host "  OK: All Schema files exist" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Schema incomplete: $schemaCount/3" -ForegroundColor Red
    $exitCode = 1
}

# 5. Check SDK spike
Write-Host "[5/9] Checking SDK spike..." -ForegroundColor Yellow

$spikeFiles = @(
    "$McpRoot\docs\spikes\stdio-smoke\",
    "$McpRoot\docs\spikes\streamable-http-smoke\"
)

$spikeCount = 0
foreach ($dir in $spikeFiles) {
    if (Test-Path $dir) {
        # Check if directory has content
        $items = Get-ChildItem -Path $dir -Recurse -File -ErrorAction SilentlyContinue
        if ($items.Count -gt 0) {
            $spikeCount++
            Write-Host "  OK: $dir (has content)" -ForegroundColor Green
        } else {
            Write-Host "  WARN: $dir (empty directory)" -ForegroundColor Yellow
            $errors += "SDK spike directory empty: $dir"
        }
    } else {
        Write-Host "  FAIL: $dir (missing)" -ForegroundColor Red
        $errors += "SDK spike missing: $dir"
    }
}

if ($spikeCount -eq 2) {
    Write-Host "  OK: All SDK spikes exist" -ForegroundColor Green
} else {
    Write-Host "  FAIL: SDK spikes incomplete: $spikeCount/2" -ForegroundColor Red
    $exitCode = 1
}

# 6. Check lock files
Write-Host "[6/9] Checking dependency lock files..." -ForegroundColor Yellow

$lockFiles = @(
    "$CoreRoot\backend\uv.lock",
    "$CoreRoot\frontend\package-lock.json"
)

$lockCount = 0
foreach ($file in $lockFiles) {
    if (Test-Path $file) {
        $lockCount++
        Write-Host "  OK: $file" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: $file (missing)" -ForegroundColor Red
        $errors += "Lock file missing: $file"
    }
}

if ($lockCount -eq 2) {
    Write-Host "  OK: All lock files exist" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Lock files incomplete: $lockCount/2" -ForegroundColor Red
    $exitCode = 1
}

# 7. Check if .env is tracked
Write-Host "[7/9] Checking sensitive files..." -ForegroundColor Yellow

if ($VC_MODE -eq "git") {
    $envTracked = git -C $CoreRoot ls-files --error-unmatch .env 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  FAIL: .env is tracked" -ForegroundColor Red
        $errors += ".env file is tracked by Git"
        $exitCode = 1
    } else {
        Write-Host "  OK: .env not tracked" -ForegroundColor Green
    }

    $configTracked = git -C $CoreRoot ls-files --error-unmatch backend/config.local.py 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  FAIL: backend/config.local.py is tracked" -ForegroundColor Red
        $errors += "backend/config.local.py is tracked by Git"
        $exitCode = 1
    } else {
        Write-Host "  OK: backend/config.local.py not tracked" -ForegroundColor Green
    }
} else {
    Write-Host "  WARN: Git mode not enabled, skipping check" -ForegroundColor Yellow
}

# 8. Check MCP Cargo.lock (if exists)
Write-Host "[8/9] Checking MCP Cargo.lock..." -ForegroundColor Yellow

if (Test-Path "$McpRoot\Cargo.lock") {
    Write-Host "  OK: Cargo.lock exists" -ForegroundColor Green
} else {
    Write-Host "  WARN: Cargo.lock not found" -ForegroundColor Yellow
    Write-Host "  INFO: This may be because cargo check failed" -ForegroundColor Yellow
}

# 9. Summary
Write-Host ""
Write-Host "=== Verification Result ===" -ForegroundColor Cyan

if ($exitCode -eq 0) {
    Write-Host "PASS: All checks passed" -ForegroundColor Green
    Write-Host "PASS: VC_MODE = $VC_MODE" -ForegroundColor Green
} else {
    Write-Host "FAIL: Found $($errors.Count) error(s):" -ForegroundColor Red
    for ($i = 0; $i -lt $errors.Count; $i++) {
        Write-Host "  $($i+1). $($errors[$i])" -ForegroundColor Red
    }
}

Write-Host ""
exit $exitCode