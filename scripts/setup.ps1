param(
    [string]$StateDirectory = "",
    [string]$PythonExecutable = "",
    [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $projectRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $candidates = @()
    if ($PythonExecutable) {
        $candidates += @{ Command = $PythonExecutable; Prefix = @() }
    } else {
        $candidates += @{ Command = "py.exe"; Prefix = @("-3.13") }
        $candidates += @{ Command = "py.exe"; Prefix = @("-3.12") }
        $candidates += @{ Command = "python.exe"; Prefix = @() }
    }
    $chosen = $null
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
        $command = $candidate.Command
        $prefix = $candidate.Prefix
        & $command @prefix -c "import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $chosen = $candidate; break }
    }
    if (-not $chosen) { throw "Python 3.12+ is required: https://www.python.org/downloads/windows/" }
    $command = $chosen.Command
    $prefix = $chosen.Prefix
    & $command @prefix -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}
& $venvPython -c "import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "The existing .venv needs Python 3.12+." }
& $venvPython -m pip install --disable-pip-version-check $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
$executable = Join-Path $projectRoot ".venv/Scripts/upc-mcp.exe"
$arguments = @()
if ($StateDirectory) {
    $statePath = [System.IO.Path]::GetFullPath($StateDirectory)
    $localDirectory = Join-Path $projectRoot ".local"
    New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
    @{ state_directory = $statePath } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $localDirectory "run-config.json") -Encoding UTF8
    $arguments += @("--state-dir", $statePath)
} else {
    $preferences = Join-Path $projectRoot ".local/run-config.json"
    if (Test-Path -LiteralPath $preferences) {
        $statePath = (Get-Content -LiteralPath $preferences -Raw -Encoding UTF8 | ConvertFrom-Json).state_directory
        $arguments += @("--state-dir", [System.IO.Path]::GetFullPath($statePath))
    }
}
Write-Host "Installed. MCP client configuration:"
& $executable @arguments mcp-config
if ($LASTEXITCODE -ne 0) { throw "Configuration generation failed." }
if (-not $NoBrowser) {
    & $executable @arguments settings
    if ($LASTEXITCODE -ne 0) { throw "Settings page could not be started." }
}
