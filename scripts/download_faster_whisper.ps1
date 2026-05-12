param(
    [ValidateSet("tiny", "base", "small", "medium", "large-v3")]
    [string]$Model = "small",

    [string]$Mirror = "https://hf-mirror.com",

    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $OutputDir) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $OutputDir = Join-Path $ProjectRoot "models\faster-whisper-$Model"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Repo = "Systran/faster-whisper-$Model"
$Files = @("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")

Write-Host "Downloading $Repo from $Mirror"
Write-Host "Output: $OutputDir"

foreach ($File in $Files) {
    $Url = "$Mirror/$Repo/resolve/main/$File"
    $Out = Join-Path $OutputDir $File
    Write-Host "Downloading $File"
    curl.exe -L -C - `
        --retry 50 `
        --retry-delay 5 `
        --connect-timeout 60 `
        --speed-time 300 `
        --speed-limit 1024 `
        -o $Out `
        $Url
}

Write-Host "Done."
Write-Host "Use with:"
Write-Host "  autocut run <video> --asr faster-whisper --asr-model `"$OutputDir`" --asr-device cpu --asr-compute-type int8 --product <product>"
