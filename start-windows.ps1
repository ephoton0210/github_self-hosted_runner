# Render and register native, ephemeral Windows runner Scheduled Tasks for
# configured repos.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if ($env:OS -ne "Windows_NT") {
    throw "start-windows.ps1 must be run on a Windows host."
}

if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) {
    throw "'.env' was not found. Copy .env.example to .env and fill in GH_PAT."
}

if (-not (Test-Path -LiteralPath "config/repos.yaml" -PathType Leaf)) {
    throw "'config/repos.yaml' was not found. Copy config/repos.yaml.example and configure a windows: section."
}

function Get-PythonCommand {
    foreach ($candidate in @(
            [PSCustomObject]@{ Name = "py"; Arguments = @("-3") }
            [PSCustomObject]@{ Name = "python"; Arguments = @() }
        )) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return [PSCustomObject]@{ Path = $command.Source; Arguments = $candidate.Arguments }
        }
    }
    throw "Python 3 was not found. Install Python 3 and run: py -3 -m pip install -r requirements.txt"
}

if ($null -eq (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    throw "PowerShell 7+ (pwsh.exe) was not found; it runs the scheduled runner-loop task. Install it from https://aka.ms/powershell."
}

$pythonCommand = Get-PythonCommand
& $pythonCommand.Path @($pythonCommand.Arguments) -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyYAML is required. Run: py -3 -m pip install -r requirements.txt"
}

$RunnerVersion = "2.336.0"
$TaskFolder = "\GitHubSelfHostedRunner\"
$taskDir = ".runner-windows\scheduled-tasks"
$slotsDir = ".runner-windows\slots"

function Wait-TaskStopped {
    param([string]$Name)
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        $task = Get-ScheduledTask -TaskName $Name -TaskPath $TaskFolder -ErrorAction SilentlyContinue
        if ($null -eq $task -or $task.State -ne "Running") {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Remove-OldTask {
    param([string]$Name)
    $slotDir = Join-Path $slotsDir $Name
    if (Test-Path -LiteralPath $slotDir) {
        New-Item -ItemType File -Force -Path (Join-Path $slotDir "stop.flag") | Out-Null
    }
    if (-not (Wait-TaskStopped -Name $Name)) {
        Write-Warning "$Name did not stop within 30 seconds; unregistering anyway."
    }
    Unregister-ScheduledTask -TaskName $Name -TaskPath $TaskFolder -Confirm:$false -ErrorAction SilentlyContinue
}

# A start after removing a windows: section must also unregister the old task.
# Read names before rendering, so a malformed/new config cannot stop a working fleet.
$oldManifest = Join-Path $taskDir "manifest.txt"
$oldNames = @()
if (Test-Path -LiteralPath $oldManifest -PathType Leaf) {
    $oldNames = @(Get-Content -LiteralPath $oldManifest | Where-Object { $_ -match '^[A-Za-z0-9_-]+$' })
}

Write-Host "==> Rendering Windows Scheduled Task configuration..."
& $pythonCommand.Path @($pythonCommand.Arguments) "scripts/render-windows-scheduled-task.py" "--runner-version" $RunnerVersion
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path ".runner-windows\logs" | Out-Null

if ($oldNames.Count -gt 0) {
    Write-Host "==> Stopping previous Windows runner tasks..."
    foreach ($name in $oldNames) {
        Remove-OldTask -Name $name
    }
}

$xmlFiles = Get-ChildItem -LiteralPath $taskDir -Filter "*.xml" -ErrorAction SilentlyContinue
if (-not $xmlFiles) {
    throw "No Windows Scheduled Tasks were rendered."
}

Write-Host "==> Registering native Windows runner tasks..."
foreach ($xmlFile in $xmlFiles) {
    $name = $xmlFile.BaseName
    # A previous run may have left this exact task registered (unchanged config).
    Remove-OldTask -Name $name
    $xmlContent = Get-Content -LiteralPath $xmlFile.FullName -Raw
    Register-ScheduledTask -TaskName $name -TaskPath $TaskFolder -Xml $xmlContent -Force | Out-Null
    Start-ScheduledTask -TaskName $name -TaskPath $TaskFolder
}

Write-Host ""
Write-Host "Windows runners are registered and started."
Write-Host "- View status:  Get-ScheduledTask -TaskPath '$TaskFolder'"
Write-Host "- Stop runners: .\stop-windows.ps1"
