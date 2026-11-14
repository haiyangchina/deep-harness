param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$desktopDir = Join-Path $PSScriptRoot '..\desktop'
$pythonOutput = & conda run --no-capture-output -n deep-harness python -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Run scripts/bootstrap.ps1 first to prepare the deep-harness Conda environment.' }
$pythonPath = $pythonOutput | Where-Object { $_ -match 'python\.exe\s*$' } | Select-Object -Last 1
if (-not $pythonPath) { throw 'Conda did not return the deep-harness Python executable path.' }
$pythonPath = $pythonPath.Trim()
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'The deep-harness Python executable was not found.' }
$previousPrefix = $env:CONDA_PREFIX
try {
    $env:CONDA_PREFIX = Split-Path -Parent $pythonPath
    Push-Location $desktopDir
    try {
        if (-not $SkipInstall) {
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
        }
        & npm.cmd start
        if ($LASTEXITCODE -ne 0) { throw 'deep-harness Desktop exited with an error.' }
    } finally { Pop-Location }
} finally { $env:CONDA_PREFIX = $previousPrefix }
