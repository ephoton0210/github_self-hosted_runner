[CmdletBinding()]
param()

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
            return [PSCustomObject]@{
                Path = $command.Source
                Arguments = $candidate.Arguments
            }
        }
    }

    throw "Python 3 was not found. Install Python 3 and run: python -m pip install -r requirements.txt"
}

if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) {
    throw "'.env' was not found. Copy .env.example to .env and fill in GH_PAT."
}

if (-not (Test-Path -LiteralPath "config/repos.yaml" -PathType Leaf)) {
    throw "'config/repos.yaml' was not found. Copy config/repos.yaml.example to config/repos.yaml and configure target repositories."
}

if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop and switch it to Linux containers mode."
}

$dockerOs = docker version --format '{{.Server.Os}}' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running or is not ready. Start Docker Desktop, ensure it uses Linux containers, then try again."
}
if ($dockerOs.Trim() -ne "linux") {
    throw "Docker Desktop must use Linux containers. Switch Docker Desktop to Linux containers mode, then try again."
}

$pythonCommand = Get-PythonCommand

Write-Host "==> Rendering Docker Compose configuration..."
& $pythonCommand.Path @($pythonCommand.Arguments) "scripts/render-compose.py"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "==> Building and starting runner containers..."
docker compose -f compose.generated.yaml up -d --build
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Runners are up and running!"
Write-Host "- View live logs: docker compose -f compose.generated.yaml logs -f"
Write-Host "- Check status:   docker compose -f compose.generated.yaml ps"
Write-Host "- Stop runners:   .\\stop.ps1"
