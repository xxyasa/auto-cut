# 启动 auto-cut 业务自助成片服务（前台运行，Ctrl+C 退出）
# 用法：在独立 PowerShell 窗口运行
#   cd F:\workCode\auto-cut
#   .\scripts\start_business_server.ps1
#
# 启动后浏览器访问：
#   http://127.0.0.1:8765/business.html
# 浏览器顶部 token 输入框填写下方打印的 token。

[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Token = $null,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path $PSScriptRoot -Parent)

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

Import-DotEnv -Path ".env"

if ($Token) {
    $env:AUTOCUT_API_TOKEN = $Token
} elseif ($env:AUTOCUT_API_TOKEN) {
    $Token = $env:AUTOCUT_API_TOKEN
} else {
    if (Test-Path .dev-token) {
        $Token = (Get-Content .dev-token -Raw).Trim()
    } else {
        $Token = "devtoken-" + ([Guid]::NewGuid().ToString("N").Substring(0, 12))
        Set-Content -Path .dev-token -Value $Token -NoNewline
    }
    $env:AUTOCUT_API_TOKEN = $Token
}

$env:PYTHONUNBUFFERED = "1"
if (-not $env:AUTOCUT_RUNS_DIR) {
    $env:AUTOCUT_RUNS_DIR = (Resolve-Path .).Path + "\data\runs"
}

New-Item -ItemType Directory -Path "data\runs", "data\uploads" -Force | Out-Null

# 检测端口占用
$occupied = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($occupied) {
    $occPid = $occupied.OwningProcess
    Write-Host ""
    Write-Host "[WARN] Port $Port is already in use by PID $occPid" -ForegroundColor Red
    Write-Host "       Run to inspect: Get-Process -Id $occPid" -ForegroundColor Yellow
    Write-Host "       Run to kill   : Stop-Process -Id $occPid -Force" -ForegroundColor Yellow
    Write-Host "       Or pick another port: .\scripts\start_business_server.ps1 -Port 8770" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " Auto Cut Business Server" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " URL  : http://${BindHost}:${Port}/business.html" -ForegroundColor Green
Write-Host " API  : http://${BindHost}:${Port}/api/business/" -ForegroundColor Green
Write-Host " Token: $Token" -ForegroundColor Yellow
Write-Host " Runs : $env:AUTOCUT_RUNS_DIR" -ForegroundColor Gray
if ($env:AUTOCUT_ASR_API_URL) {
    $asrUri = [Uri]$env:AUTOCUT_ASR_API_URL
    $fallback = if ($env:AUTOCUT_ASR_HTTP_FALLBACK) { $env:AUTOCUT_ASR_HTTP_FALLBACK } else { "off" }
    $uploadFormat = if ($env:AUTOCUT_ASR_UPLOAD_FORMAT) { $env:AUTOCUT_ASR_UPLOAD_FORMAT } else { "wav" }
    Write-Host " ASR  : $($asrUri.Scheme)://$($asrUri.Host)  http_fallback=$fallback  upload=$uploadFormat" -ForegroundColor Gray
    if ($asrUri.Host -in @("http", "https")) {
        Write-Host "       WARN: ASR URL host looks invalid; check for duplicated scheme like https://https://" -ForegroundColor Yellow
    }
}
if ($env:AUTOCUT_LLM_API_URL) {
    $llmUri = [Uri]$env:AUTOCUT_LLM_API_URL
    $fallback = if ($env:AUTOCUT_LLM_HTTP_FALLBACK) { $env:AUTOCUT_LLM_HTTP_FALLBACK } else { "off" }
    Write-Host " LLM  : $($llmUri.Scheme)://$($llmUri.Host)  http_fallback=$fallback" -ForegroundColor Gray
    if ($llmUri.Host -in @("http", "https")) {
        Write-Host "       WARN: LLM URL host looks invalid; check for duplicated scheme like https://https://" -ForegroundColor Yellow
    }
}
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

$args = @("-m", "uvicorn", "autocut.api:app", "--host", $BindHost, "--port", $Port, "--workers", "1")
if ($Reload) { $args += "--reload" }

& .venv\Scripts\python.exe @args
