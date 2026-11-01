param([string]$StateDirectory = "")
$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$executable = Join-Path $projectRoot ".venv/Scripts/upc-mcp.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Run install.cmd / scripts/setup.ps1 first."
}
$preferences = Join-Path $projectRoot ".local/run-config.json"
if (-not $StateDirectory -and (Test-Path -LiteralPath $preferences)) {
    $StateDirectory = (Get-Content -LiteralPath $preferences -Raw -Encoding UTF8 | ConvertFrom-Json).state_directory
}
$arguments = @()
if ($StateDirectory) { $arguments += @("--state-dir", [System.IO.Path]::GetFullPath($StateDirectory)) }
$arguments += "settings"
& $executable @arguments
exit $LASTEXITCODE
