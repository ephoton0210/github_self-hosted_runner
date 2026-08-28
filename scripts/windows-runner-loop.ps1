# Native Windows ephemeral-runner supervisor. A Scheduled Task invokes one
# copy per configured replica; every iteration expands a clean runner
# directory, processes at most one job, deregisters it, and removes that
# directory.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Labels,
    [Parameter(Mandatory = $true)][string]$RunnerName,
    [Parameter(Mandatory = $true)][string]$StateDir,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$RunnerVersion = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ApiVersion = "2022-11-28"
$RetrySeconds = 15
$PinnedRunnerVersion = "2.336.0"
$PinnedSha256 = @{
    "x64"   = "d59123a43003e357b0805b5d0f611d0bd2f65ab67d51bd070dd4e7a0f685c162"
    "arm64" = "b3799e9cf754fe4dfcb3d220c9701c924829737ee815dbeb674f8bd076794504"
}

function Fail {
    param([string]$Message)
    [Console]::Error.WriteLine("Error: $Message")
    exit 1
}

if ($env:OS -ne "Windows_NT") {
    Fail "this supervisor must run on Windows"
}

if (-not $RunnerVersion) {
    $RunnerVersion = if ($env:RUNNER_VERSION) { $env:RUNNER_VERSION } else { $PinnedRunnerVersion }
}
if ($RunnerVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    Fail "RUNNER_VERSION is invalid"
}
if ($RunnerVersion -ne $PinnedRunnerVersion) {
    Fail "RUNNER_VERSION $RunnerVersion is not pinned in this release; update the version and SHA-256 together"
}

switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $RunnerArch = "x64" }
    "ARM64" { $RunnerArch = "arm64" }
    default { Fail "unsupported Windows architecture: $($env:PROCESSOR_ARCHITECTURE)" }
}
$RunnerSha256 = $PinnedSha256[$RunnerArch]

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    Fail "--env-file must point to the existing .env file"
}

function Read-EnvValue {
    param([string]$Key)
    $line = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
    if (-not $line) {
        Fail "$Key is missing from $EnvFile"
    }
    $value = $line.Substring($Key.Length + 1).Trim("`r")
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    if (-not $value) {
        Fail "$Key is empty in $EnvFile"
    }
    return $value
}

$GhPat = Read-EnvValue -Key "GH_PAT"
$ApiBase = "https://api.github.com/repos/$Owner/$Repo"
$AuthHeaders = @{
    "Authorization"        = "Bearer $GhPat"
    "Accept"                = "application/vnd.github+json"
    "X-GitHub-Api-Version" = $ApiVersion
}

function Get-ApiToken {
    param([string]$Endpoint)
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$ApiBase/actions/runners/$Endpoint" -Headers $AuthHeaders
        return $response.token
    } catch {
        Write-Warning "could not request $Endpoint for ${Owner}/${Repo}: $($_.Exception.Message)"
        return $null
    }
}

$script:CurrentRunnerDir = $null
$script:RunnerProcess = $null
$StopFlagPath = Join-Path $StateDir "stop.flag"

function Stop-RunnerProcessTree {
    if ($null -ne $script:RunnerProcess -and -not $script:RunnerProcess.HasExited) {
        $targetPid = $script:RunnerProcess.Id
        try { & taskkill /PID $targetPid /T *>$null } catch {}
        $deadline = (Get-Date).AddSeconds(10)
        while (-not $script:RunnerProcess.HasExited -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
        }
        if (-not $script:RunnerProcess.HasExited) {
            try { & taskkill /PID $targetPid /T /F *>$null } catch {}
        }
        try { $script:RunnerProcess.WaitForExit() } catch {}
    }
    $script:RunnerProcess = $null
}

function Remove-CurrentRunner {
    if ($script:CurrentRunnerDir) {
        $configScript = Join-Path $script:CurrentRunnerDir "config.cmd"
        if (Test-Path -LiteralPath $configScript -PathType Leaf) {
            $removalToken = Get-ApiToken -Endpoint "remove-token"
            if ($removalToken) {
                Push-Location $script:CurrentRunnerDir
                try {
                    & .\config.cmd remove --token $removalToken *>$null
                } catch {
                    Write-Warning "runner removal failed: $($_.Exception.Message)"
                } finally {
                    Pop-Location
                }
            }
        }
        Remove-Item -LiteralPath $script:CurrentRunnerDir -Recurse -Force -ErrorAction SilentlyContinue
        $script:CurrentRunnerDir = $null
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $StateDir "cache") | Out-Null
$Archive = Join-Path $StateDir "cache\actions-runner-win-$RunnerArch-$RunnerVersion.zip"

if (Test-Path -LiteralPath $Archive -PathType Leaf) {
    $actualHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $RunnerSha256) {
        Write-Warning "cached runner archive failed SHA-256 verification; downloading it again."
        Remove-Item -LiteralPath $Archive -Force
    }
}
if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
    $temporaryArchive = "$Archive.$([System.IO.Path]::GetRandomFileName()).tmp"
    Write-Host "==> Downloading actions/runner $RunnerVersion for Windows $RunnerArch..."
    try {
        Invoke-WebRequest -Uri "https://github.com/actions/runner/releases/download/v$RunnerVersion/actions-runner-win-$RunnerArch-$RunnerVersion.zip" `
            -OutFile $temporaryArchive -MaximumRetryCount 3
    } catch {
        Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
        Fail "runner archive download failed: $($_.Exception.Message)"
    }
    $actualHash = (Get-FileHash -LiteralPath $temporaryArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $RunnerSha256) {
        Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
        Fail "runner archive SHA-256 did not match the pinned release"
    }
    Move-Item -LiteralPath $temporaryArchive -Destination $Archive -Force
}

function Test-StopRequested {
    return Test-Path -LiteralPath $StopFlagPath -PathType Leaf
}

try {
    while (-not (Test-StopRequested)) {
        $script:CurrentRunnerDir = Join-Path $StateDir ("runner." + [System.IO.Path]::GetRandomFileName())
        New-Item -ItemType Directory -Force -Path $script:CurrentRunnerDir | Out-Null

        try {
            Expand-Archive -LiteralPath $Archive -DestinationPath $script:CurrentRunnerDir -Force
        } catch {
            Write-Warning "could not extract ${Archive}: $($_.Exception.Message); retrying in ${RetrySeconds}s."
            Remove-CurrentRunner
            Start-Sleep -Seconds $RetrySeconds
            continue
        }

        $registrationToken = Get-ApiToken -Endpoint "registration-token"
        if (-not $registrationToken) {
            Write-Warning "registration token unavailable; retrying in ${RetrySeconds}s."
            Remove-CurrentRunner
            Start-Sleep -Seconds $RetrySeconds
            continue
        }

        Push-Location $script:CurrentRunnerDir
        $configOk = $true
        try {
            & .\config.cmd --unattended `
                --url "https://github.com/$Owner/$Repo" `
                --token $registrationToken `
                --name $RunnerName `
                --labels $Labels `
                --work (Join-Path $script:CurrentRunnerDir "_work") `
                --ephemeral `
                --disableupdate `
                --replace
            if ($LASTEXITCODE -ne 0) { $configOk = $false }
        } catch {
            $configOk = $false
        } finally {
            Pop-Location
        }
        if (-not $configOk) {
            Write-Warning "runner configuration failed; retrying in ${RetrySeconds}s."
            Remove-CurrentRunner
            Start-Sleep -Seconds $RetrySeconds
            continue
        }

        Write-Host "==> $RunnerName is ready for one job."
        $script:RunnerProcess = Start-Process -FilePath (Join-Path $script:CurrentRunnerDir "run.cmd") `
            -WorkingDirectory $script:CurrentRunnerDir -PassThru -WindowStyle Hidden

        while (-not $script:RunnerProcess.HasExited) {
            if (Test-StopRequested) {
                Stop-RunnerProcessTree
                break
            }
            Start-Sleep -Seconds 2
        }
        if ($script:RunnerProcess -and -not $script:RunnerProcess.HasExited) {
            $script:RunnerProcess.WaitForExit()
        }
        $runnerExitCode = if ($script:RunnerProcess) { $script:RunnerProcess.ExitCode } else { 0 }
        $script:RunnerProcess = $null
        Remove-CurrentRunner

        if (Test-StopRequested) {
            break
        }
        if ($runnerExitCode -ne 0) {
            Write-Warning "$RunnerName exited with ${runnerExitCode}; retrying in ${RetrySeconds}s."
            Start-Sleep -Seconds $RetrySeconds
        }
    }
} finally {
    Stop-RunnerProcessTree
    Remove-CurrentRunner
    Remove-Item -LiteralPath $StopFlagPath -Force -ErrorAction SilentlyContinue
}
