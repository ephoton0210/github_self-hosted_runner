# Start the local status dashboard for this host's runner fleet.
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787,
    # This host's name in a multi-host fleet view (default: hostname).
    [string]$Label,
    # Other hosts' dashboards to merge in, each "LABEL=URL"
    # (e.g. -Peer "hostb=http://192.168.1.20:8787"); repeatable as an array.
    [string[]]$Peer = @(),
    # Central dashboard(s) to self-register with instead of being hand-added
    # via -Peer there; repeatable as an array. Requires -AdvertiseUrl.
    [string[]]$RegisterTo = @(),
    # URL other hosts should use to reach this dashboard, e.g.
    # http://hostb.internal:8787 — required with -RegisterTo.
    [string]$AdvertiseUrl,
    [double]$RegisterInterval = 20.0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

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

$pythonCommand = Get-PythonCommand
& $pythonCommand.Path @($pythonCommand.Arguments) -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyYAML is required. Run: py -3 -m pip install -r requirements.txt"
}

if ($RegisterTo.Count -gt 0 -and -not $AdvertiseUrl) {
    throw "-RegisterTo requires -AdvertiseUrl."
}

$dashboardArgs = @("scripts/dashboard.py", "--host", $BindHost, "--port", $Port)
if ($Label) {
    $dashboardArgs += @("--label", $Label)
}
foreach ($p in $Peer) {
    $dashboardArgs += @("--peer", $p)
}
foreach ($target in $RegisterTo) {
    $dashboardArgs += @("--register-to", $target)
}
if ($AdvertiseUrl) {
    $dashboardArgs += @("--advertise-url", $AdvertiseUrl, "--register-interval", $RegisterInterval)
}

& $pythonCommand.Path @($pythonCommand.Arguments) @dashboardArgs
