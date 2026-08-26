[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if (-not (Test-Path -LiteralPath "compose.generated.yaml" -PathType Leaf)) {
    Write-Host "compose.generated.yaml not found. No runners seem to be running."
    exit 0
}

if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop and switch it to Linux containers mode."
}

docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running or is not ready. Start Docker Desktop, ensure it uses Linux containers, then try again."
}

Write-Host "==> Stopping runner containers..."
docker compose -f compose.generated.yaml down
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Runners stopped."
