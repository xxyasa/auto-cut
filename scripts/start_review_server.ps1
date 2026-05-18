param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        $match = [regex]::Match($line, '^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
        if (-not $match.Success) {
            return
        }

        $name = $match.Groups[1].Value
        $value = $match.Groups[2].Value.Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if (-not (Test-Path -LiteralPath $Uvicorn)) {
    throw "uvicorn not found. Please install dependencies first: .\.venv\Scripts\python.exe -m pip install -e `".[api]`""
}

Write-Host "Auto Cut 审核台"
Write-Host "Local URL: http://127.0.0.1:$Port"
Write-Host "LAN URL: http://<your-ip>:$Port"
Write-Host "Press Ctrl+C to stop."

Push-Location $ProjectRoot
try {
    Import-DotEnv -Path ".env"
    & $Uvicorn autocut.api:app --host $HostAddress --port $Port
}
finally {
    Pop-Location
}
