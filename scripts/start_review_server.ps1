param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path -LiteralPath $Uvicorn)) {
    throw "uvicorn not found. Please install dependencies first: .\.venv\Scripts\python.exe -m pip install -e `".[api]`""
}

Write-Host "Auto Cut 审核台"
Write-Host "Local URL: http://127.0.0.1:$Port"
Write-Host "LAN URL: http://<your-ip>:$Port"
Write-Host "Press Ctrl+C to stop."

Push-Location $ProjectRoot
try {
    & $Uvicorn autocut.api:app --host $HostAddress --port $Port
}
finally {
    Pop-Location
}
