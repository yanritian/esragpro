[CmdletBinding()]
param(
    [Parameter()]
    [string]$Environment = "test"
)

$ErrorActionPreference = "Stop"

$composeFile = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\docker-compose.p1-13-uploads-restore-test.yml")
)

$dockerAvailable = $false
$cleanupRequired = $false
$cleanupFailed = $false
$environmentSnapshotTaken = $false
$projectName = $null
$ownedVolumes = @()
$failureMessage = $null
$exitCode = 0
$proofLines = [System.Collections.Generic.List[string]]::new()
$cleanupMessages = [System.Collections.Generic.List[string]]::new()
$priorEnvironment = @{}
$environmentVariableNames = @(
    "P1_13_UPLOADS_SOURCE_VOLUME",
    "P1_13_UPLOADS_TARGET_VOLUME",
    "P1_13_UPLOADS_BACKUP_VOLUME"
)

function Assert-NotProduction {
    param(
        [Parameter(Mandatory)]
        [string]$RequestedEnvironment
    )

    $blockedEnvironments = @("prod", "production", "live")
    $normalizedEnvironment = $RequestedEnvironment.Trim().ToLowerInvariant()
    if ($blockedEnvironments -contains $normalizedEnvironment) {
        throw "Refusing to run the P1-13 restore drill in production/live environment '$RequestedEnvironment'."
    }

    $environmentSignals = @("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV", "NODE_ENV", "ASPNETCORE_ENVIRONMENT")
    foreach ($signalName in $environmentSignals) {
        $signalValue = [Environment]::GetEnvironmentVariable($signalName, "Process")
        if ($blockedEnvironments -contains ([string]$signalValue).Trim().ToLowerInvariant()) {
            throw "Refusing to run because $signalName='$signalValue' identifies a production/live environment."
        }
    }

    $existingComposeProject = [Environment]::GetEnvironmentVariable("COMPOSE_PROJECT_NAME", "Process")
    if ($existingComposeProject -and $existingComposeProject -match "(?i)(^|[-_])(prod|production|live)([-_]|$)") {
        throw "Refusing to run because COMPOSE_PROJECT_NAME='$existingComposeProject' looks production-scoped."
    }
}

function Invoke-ComposeCommand {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    # All Docker operations use the unique, explicitly selected "docker compose" project.
    $composePrefix = $script:composePrefix
    $output = & $script:dockerExecutable @composePrefix @Arguments 2>&1
    $commandExitCode = $LASTEXITCODE
    $lines = @($output | ForEach-Object { [string]$_ })
    if ($commandExitCode -ne 0) {
        $details = if ($lines.Count -gt 0) { $lines -join [Environment]::NewLine } else { "(no output)" }
        throw "docker compose command failed with exit code $($commandExitCode): $details"
    }

    return $lines
}

function Invoke-DrillContainer {
    param(
        [Parameter(Mandatory)]
        [string]$Command
    )

    $output = @(Invoke-ComposeCommand -Arguments @(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "restore-drill",
            "sh",
            "-ec",
            $Command
        ))
    return @(
        $output | Where-Object {
            $_ -notmatch "^\s*Container\s+\S+\s+(Creating|Created|Attaching|Started|Stopping|Stopped)\s*$"
        }
    )
}

function Assert-SafeRelativePath {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Context
    )

    $normalized = $Path.Replace("\", "/")
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized.IndexOf([char]0) -ge 0) {
        throw "Unsafe empty or NUL-containing path in $Context."
    }
    if ($normalized.StartsWith("/") -or $normalized -match "^[A-Za-z]:") {
        throw "Unsafe absolute path '$Path' in $Context."
    }

    $parts = @($normalized -split "/")
    if ($parts -contains ".." -or $parts -contains ".") {
        throw "Unsafe path traversal component in '$Path' ($Context)."
    }

    return $normalized
}

function Assert-SafeArchiveEntries {
    param(
        [Parameter(Mandatory)]
        [string[]]$Entries
    )

    $safeEntries = [System.Collections.Generic.List[string]]::new()
    foreach ($rawEntry in @($Entries)) {
        $entry = ([string]$rawEntry).Trim()
        if ($entry.Length -eq 0) {
            continue
        }

        $normalized = $entry.Replace("\", "/")
        if ($normalized.StartsWith("/") -or $normalized -match "^[A-Za-z]:") {
            throw "Refusing archive with unsafe absolute path '$entry'."
        }

        # BusyBox tar lists entries with a leading ./; strip only that harmless prefix.
        $relative = [regex]::Replace($normalized, "^(?:\./)+", "")
        if ($relative.Length -eq 0) {
            continue
        }

        [void](Assert-SafeRelativePath -Path $relative -Context "archive entry")
        [void]$safeEntries.Add($relative)
    }

    if ($safeEntries.Count -eq 0) {
        throw "Backup archive contained no safe entries."
    }

    return $safeEntries.ToArray()
}

function ConvertTo-Manifest {
    param(
        [Parameter(Mandatory)]
        [string[]]$Lines,

        [Parameter(Mandatory)]
        [string]$Label
    )

    $manifest = [ordered]@{}
    foreach ($rawLine in @($Lines)) {
        $line = ([string]$rawLine).Trim()
        if ($line.Length -eq 0) {
            continue
        }

        $parts = $line -split [char]9, 3
        if ($parts.Count -ne 3) {
            throw "Malformed $Label manifest line: '$line'."
        }

        $path = Assert-SafeRelativePath -Path $parts[0] -Context "$Label manifest"
        if ($manifest.Contains($path)) {
            throw "Duplicate path '$path' in $Label manifest."
        }

        [long]$size = 0
        $parsed = [long]::TryParse(
            $parts[1],
            [Globalization.NumberStyles]::Integer,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$size
        )
        if (-not $parsed -or $size -lt 0) {
            throw "Invalid byte size '$($parts[1])' for '$path' in $Label manifest."
        }

        $sha256 = $parts[2].ToLowerInvariant()
        if ($sha256 -notmatch "^[0-9a-f]{64}$") {
            throw "Invalid SHA-256 '$sha256' for '$path' in $Label manifest."
        }

        $manifest[$path] = [pscustomobject]@{
            Path   = $path
            Size   = $size
            Sha256 = $sha256
        }
    }

    if ($manifest.Count -eq 0) {
        throw "$Label manifest was empty."
    }

    return ,$manifest
}

function Assert-ExpectedFixturePaths {
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Manifest
    )

    $expectedPaths = @(
        "incoming/2026/09/nested/empty.bin",
        "incoming/2026/09/nested/manifest.csv",
        "teams/platform/assets/schema.json",
        "teams/platform/specs/retention-policy.txt"
    )
    $actualPaths = @($Manifest.Keys | ForEach-Object { [string]$_ } | Sort-Object)
    $sortedExpectedPaths = @($expectedPaths | Sort-Object)
    if (($actualPaths -join [Environment]::NewLine) -cne ($sortedExpectedPaths -join [Environment]::NewLine)) {
        throw "Synthetic fixture paths did not match the expected nested file set."
    }
}

function Compare-Manifests {
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Source,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Target
    )

    $sourcePaths = @($Source.Keys | ForEach-Object { [string]$_ } | Sort-Object)
    $targetPaths = @($Target.Keys | ForEach-Object { [string]$_ } | Sort-Object)
    if (($sourcePaths -join [Environment]::NewLine) -cne ($targetPaths -join [Environment]::NewLine)) {
        throw "Restored file paths differ from the source volume."
    }

    $comparisons = [System.Collections.Generic.List[object]]::new()
    foreach ($path in $sourcePaths) {
        $sourceFile = $Source[$path]
        $targetFile = $Target[$path]
        if ([long]$sourceFile.Size -ne [long]$targetFile.Size) {
            throw "Restored byte size differs for '$path': source=$($sourceFile.Size), target=$($targetFile.Size)."
        }
        if ($sourceFile.Sha256 -cne $targetFile.Sha256) {
            throw "Restored SHA-256 differs for '$path': source=$($sourceFile.Sha256), target=$($targetFile.Sha256)."
        }

        [void]$comparisons.Add([pscustomobject]@{
                Path   = $path
                Size   = $sourceFile.Size
                Sha256 = $sourceFile.Sha256
            })
    }

    return $comparisons.ToArray()
}

try {
    Assert-NotProduction -RequestedEnvironment $Environment

    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
        throw "Compose file not found: $composeFile"
    }

    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $dockerCommand) {
        throw "Docker CLI is required for the guarded P1-13 live drill."
    }
    $script:dockerExecutable = if ($dockerCommand.Source) { $dockerCommand.Source } else { $dockerCommand.Name }
    $dockerAvailable = $true

    $runId = [Guid]::NewGuid().ToString("N")
    $projectName = "p1-13-uploads-restore-$($runId.Substring(0, 12))"
    $sourceVolume = "$projectName-source"
    $targetVolume = "$projectName-target"
    $backupVolume = "$projectName-backup"
    $ownedVolumes = @($sourceVolume, $targetVolume, $backupVolume)
    $script:composePrefix = @(
        "compose",
        "--progress",
        "quiet",
        "--project-name",
        $projectName,
        "--file",
        $composeFile
    )

    foreach ($variableName in $environmentVariableNames) {
        $priorEnvironment[$variableName] = [Environment]::GetEnvironmentVariable($variableName, "Process")
    }
    $environmentSnapshotTaken = $true
    $env:P1_13_UPLOADS_SOURCE_VOLUME = $sourceVolume
    $env:P1_13_UPLOADS_TARGET_VOLUME = $targetVolume
    $env:P1_13_UPLOADS_BACKUP_VOLUME = $backupVolume
    $cleanupRequired = $true

    $null = Invoke-ComposeCommand -Arguments @("config", "--quiet")

    $fixtureCommand = @'
set -eu
clear_dir() {
  root="$1"
  for entry in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    rm -rf "$entry"
  done
}
clear_dir /source
clear_dir /target
clear_dir /backup
mkdir -p /source/incoming/2026/09/nested
mkdir -p /source/teams/platform/assets
mkdir -p /source/teams/platform/specs
printf '%s' 'path,size
incoming/2026/09/nested/manifest.csv,39
' > /source/incoming/2026/09/nested/manifest.csv
: > /source/incoming/2026/09/nested/empty.bin
printf '%s' '{"version":1,"kind":"uploads-data"}
' > /source/teams/platform/assets/schema.json
printf '%s' 'retention-days=365
owner=platform
' > /source/teams/platform/specs/retention-policy.txt
test -f /source/incoming/2026/09/nested/manifest.csv
test -f /source/incoming/2026/09/nested/empty.bin
test -f /source/teams/platform/assets/schema.json
test -f /source/teams/platform/specs/retention-policy.txt
'@
    $null = Invoke-DrillContainer -Command $fixtureCommand

    $sourceManifestCommand = @'
set -eu
find /source -type f | sort | while IFS= read -r file; do
  relative="$(printf '%s' "$file" | sed 's#^/source/##')"
  size="$(wc -c < "$file" | tr -d '[:space:]')"
  sha="$(sha256sum "$file" | awk '{print $1}')"
  printf '%s\t%s\t%s\n' "$relative" "$size" "$sha"
done
'@
    $sourceManifestLines = @(Invoke-DrillContainer -Command $sourceManifestCommand)
    $sourceManifest = ConvertTo-Manifest -Lines $sourceManifestLines -Label "source"
    Assert-ExpectedFixturePaths -Manifest $sourceManifest

    $backupCommand = @'
set -eu
rm -f /backup/uploads-data.tar.gz
tar -czf /backup/uploads-data.tar.gz -C /source .
test -s /backup/uploads-data.tar.gz
'@
    $null = Invoke-DrillContainer -Command $backupCommand

    $archiveListCommand = @'
set -eu
tar -tzf /backup/uploads-data.tar.gz
'@
    $archiveEntries = @(Invoke-DrillContainer -Command $archiveListCommand)
    $safeArchiveEntries = @(Assert-SafeArchiveEntries -Entries $archiveEntries)

    $restoreCommand = @'
set -eu
clear_dir() {
  root="$1"
  for entry in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    rm -rf "$entry"
  done
}
clear_dir /target
tar -xzf /backup/uploads-data.tar.gz -C /target
'@
    $null = Invoke-DrillContainer -Command $restoreCommand

    $targetManifestCommand = @'
set -eu
find /target -type f | sort | while IFS= read -r file; do
  relative="$(printf '%s' "$file" | sed 's#^/target/##')"
  size="$(wc -c < "$file" | tr -d '[:space:]')"
  sha="$(sha256sum "$file" | awk '{print $1}')"
  printf '%s\t%s\t%s\n' "$relative" "$size" "$sha"
done
'@
    $targetManifestLines = @(Invoke-DrillContainer -Command $targetManifestCommand)
    $targetManifest = ConvertTo-Manifest -Lines $targetManifestLines -Label "target"
    $comparisons = @(Compare-Manifests -Source $sourceManifest -Target $targetManifest)

    [void]$proofLines.Add("P1-13 uploads_data restore proof: PASS")
    [void]$proofLines.Add("Compose project: $projectName")
    [void]$proofLines.Add("Source volume: $sourceVolume")
    [void]$proofLines.Add("Target volume: $targetVolume")
    [void]$proofLines.Add("Backup volume: $backupVolume")
    [void]$proofLines.Add("Safe archive entries checked: $($safeArchiveEntries.Count)")
    [void]$proofLines.Add("Files compared by path, size, and SHA-256: $($comparisons.Count)")
    foreach ($comparison in $comparisons) {
        [void]$proofLines.Add(
            ("MATCH path={0} size={1} sha256={2}" -f $comparison.Path, $comparison.Size, $comparison.Sha256)
        )
    }
} catch {
    $exitCode = 1
    $failureMessage = $_.Exception.Message
} finally {
    if ($cleanupRequired -and $dockerAvailable) {
        try {
            $composePrefix = $script:composePrefix
            $downOutput = & $script:dockerExecutable @composePrefix down --volumes --remove-orphans 2>&1
            $downExitCode = $LASTEXITCODE
            if ($downExitCode -eq 0) {
                [void]$cleanupMessages.Add("cleanup: docker compose down --volumes --remove-orphans completed for $projectName")
            } else {
                $cleanupFailed = $true
                [void]$cleanupMessages.Add("cleanup: compose down failed with exit code $downExitCode")
            }
        } catch {
            $cleanupFailed = $true
            [void]$cleanupMessages.Add("cleanup: compose down raised '$($_.Exception.Message)'")
        }

        foreach ($volume in $ownedVolumes) {
            try {
                $null = & $script:dockerExecutable volume inspect $volume 2>$null
                $inspectExitCode = $LASTEXITCODE
                if ($inspectExitCode -eq 0) {
                    $null = & $script:dockerExecutable volume rm -f $volume 2>&1
                    $removeExitCode = $LASTEXITCODE
                    if ($removeExitCode -eq 0) {
                        [void]$cleanupMessages.Add("cleanup: removed owned volume $volume")
                    } else {
                        $cleanupFailed = $true
                        [void]$cleanupMessages.Add("cleanup: failed to remove owned volume $volume (exit code $removeExitCode)")
                    }
                } else {
                    [void]$cleanupMessages.Add("cleanup: owned volume $volume already absent")
                }
            } catch {
                $cleanupFailed = $true
                [void]$cleanupMessages.Add("cleanup: volume check/removal for $volume raised '$($_.Exception.Message)'")
            }
        }
    } elseif (-not $cleanupRequired) {
        [void]$cleanupMessages.Add("cleanup: no owned Docker resources were created")
    } else {
        [void]$cleanupMessages.Add("cleanup: Docker CLI unavailable; no owned Docker resources were created")
    }

    if ($environmentSnapshotTaken) {
        try {
            foreach ($variableName in $environmentVariableNames) {
                $priorValue = $priorEnvironment[$variableName]
                if ($null -eq $priorValue) {
                    Remove-Item -LiteralPath "Env:$variableName" -ErrorAction SilentlyContinue
                } else {
                    Set-Item -LiteralPath "Env:$variableName" -Value $priorValue
                }
            }
            [void]$cleanupMessages.Add("cleanup: restored process-scoped volume environment variables")
        } catch {
            $cleanupFailed = $true
            [void]$cleanupMessages.Add("cleanup: failed to restore process-scoped environment variables")
        }
    }
}

foreach ($cleanupMessage in $cleanupMessages) {
    Write-Output $cleanupMessage
}

if ($exitCode -ne 0 -or $cleanupFailed) {
    if ($failureMessage) {
        Write-Error $failureMessage
    }
    if ($cleanupFailed -and -not $failureMessage) {
        Write-Error "P1-13 restore proof failed because owned-resource cleanup was incomplete."
    }
    exit 1
}

foreach ($proofLine in $proofLines) {
    Write-Output $proofLine
}
