param(
  [string]$OutputRoot = "",
  [switch]$AllowDirty,
  [switch]$CommittedUnreviewed,
  [string[]]$ReviewedPath = @()
)
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Create .venv and install Thoth first." }
$arguments = @((Join-Path $PSScriptRoot "public_release.py"), "--repo", $repoRoot)
if ($OutputRoot) { $arguments += @("--output", $OutputRoot) }
if ($AllowDirty) { $arguments += "--allow-dirty" }
if ($CommittedUnreviewed) { $arguments += "--committed-unreviewed" }
foreach ($path in $ReviewedPath) { $arguments += @("--reviewed-path", $path) }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw "Public release validation or export failed." }
