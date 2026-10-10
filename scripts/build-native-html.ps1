$Install = $false
if ($args -contains "-Install") { $Install = $true }
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

if (-not (Test-Path $python)) {
    throw "Thoth virtual environment not found: $python"
}
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust/Cargo is unavailable. Install Rustup for the current user first."
}

Push-Location $repoRoot
try {
    $outDir = Join-Path $repoRoot "dist\native"
    & $python -m maturin build --release --manifest-path native\thoth_html\Cargo.toml --out $outDir
    if ($LASTEXITCODE -ne 0) { throw "Native wheel build failed; no wheel was installed." }
    if ($Install) {
        $wheel = Get-ChildItem $outDir -Filter "thoth_html_native-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $wheel) { throw "Native wheel was not produced in $outDir" }
        & $python -m pip install --force-reinstall --no-deps $wheel.FullName
        if ($LASTEXITCODE -ne 0) { throw "Native wheel installation failed." }
        & $python -c "import _thoth_html; print('native backend:', _thoth_html.IMPLEMENTATION)"
        if ($LASTEXITCODE -ne 0) { throw "Native wheel import failed." }
    } else {
        Write-Host "Native wheel built in $outDir. Install with -Install while Thoth is stopped." -ForegroundColor Green
    }
}
finally {
    Pop-Location
}
