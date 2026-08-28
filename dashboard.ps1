# Start the local status dashboard for this host's runner fleet.
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787
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

& $pythonCommand.Path @($pythonCommand.Arguments) "scripts/dashboard.py" "--host" $BindHost "--port" $Port
