param([string]$EnvironmentName = 'deep-harness')
$ErrorActionPreference = 'Stop'
if ($EnvironmentName -ne 'deep-harness') {
    throw 'This bootstrap only provisions the dedicated deep-harness environment.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$condaCommand = (Get-Command conda -ErrorAction Stop).Source
$environments = (& $condaCommand env list --json | ConvertFrom-Json).envs
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Conda environments.' }
$environmentPath = $environments | Where-Object {
    (Split-Path -Leaf $_) -eq $EnvironmentName
} | Select-Object -First 1
if (-not $environmentPath) {
    & $condaCommand create --name $EnvironmentName --override-channels --channel conda-forge --solver libmamba python=3.12 pip --yes
    if ($LASTEXITCODE -ne 0) { throw 'Conda environment creation failed.' }
    $environments = (& $condaCommand env list --json | ConvertFrom-Json).envs
    $environmentPath = $environments | Where-Object {
        (Split-Path -Leaf $_) -eq $EnvironmentName
    } | Select-Object -First 1
}
$projectPython = Join-Path $environmentPath 'python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Conda Python not found.' }
$pythonMinor = & $projectPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonMinor -ne '3.12') {
    throw "The deep-harness environment must use Python 3.12; found $pythonMinor."
}
Push-Location $projectRoot
try {
    $installArguments = @('-m', 'pip', 'install', '--disable-pip-version-check')
    if (Test-Path -LiteralPath 'requirements.lock') {
        & $projectPython @installArguments --requirement requirements.lock
        if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    }
    & $projectPython @installArguments -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
    & $projectPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Installed dependencies are inconsistent.' }
    & $projectPython scripts/dependencies.py freeze
    if ($LASTEXITCODE -ne 0) { throw 'Cannot record dependency lock.' }
    Write-Output "Ready: conda activate $EnvironmentName"
} finally {
    Pop-Location
}
