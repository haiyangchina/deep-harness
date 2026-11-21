param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [string]$Bash = 'C:/Program Files/Git/bin/bash.exe',
    [switch]$Package
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$priorPython = $env:Harness_PYTHON
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$env:Harness_PYTHON = $interpreter.Replace('\', '/')

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

Push-Location $root
try {
    Invoke-Checked $interpreter @('-c', 'import pathlib,sys; path=pathlib.Path(sys.prefix); assert sys.version_info[:2]==(3,12) and path.name=="deep-harness" and (path/"conda-meta").is_dir()')
    Invoke-Checked $Bash @('scripts/run_tests.sh', '-q')
    Invoke-Checked $interpreter @('-m', 'ruff', 'check', 'src', 'tests', 'scripts', '--no-cache')
    Push-Location desktop
    try {
        Invoke-Checked 'npm.cmd' @('run', 'typecheck')
        Invoke-Checked 'npm.cmd' @('run', 'build')
        Invoke-Checked 'npm.cmd' @('test')
        if ($Package) { Invoke-Checked 'npm.cmd' @('run', 'dist:win') }
    } finally {
        Pop-Location
    }
} finally {
    Pop-Location
    $env:Harness_PYTHON = $priorPython
}
