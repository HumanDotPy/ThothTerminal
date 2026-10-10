param()

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  throw "Missing project interpreter at .venv\Scripts\python.exe. Create it first and install the desktop extra."
}

$icon = Join-Path $repoRoot "thoth_terminal\web\static\icons\thoth-terminal.ico"
if (-not (Test-Path $icon)) {
  throw "Missing desktop icon at $icon."
}

& $python -m pip install -e ".[desktop]" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed." }
& $python -c "import _thoth_html; print('native backend:', _thoth_html.IMPLEMENTATION)"
if ($LASTEXITCODE -ne 0) { throw "Build/install the required Rust extension with scripts/build-native-html.ps1 -Install first." }
& $python -m pip install -U pyinstaller | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

& $python -m PyInstaller `
  --noconfirm `
  --clean `
  --name ThothTerminal `
  --windowed `
  --icon $icon `
  --specpath build `
  --collect-data thoth_terminal `
  --collect-submodules PySide6 `
  --collect-submodules PySide6.QtWebEngineCore `
  --collect-submodules PySide6.QtWebEngineWidgets `
  --hidden-import thoth_terminal.desktop.app `
  --hidden-import _thoth_html `
  --copy-metadata thoth-terminal `
  --paths $repoRoot `
  thoth_terminal\desktop\__main__.py
if ($LASTEXITCODE -ne 0) { throw "Desktop bundle build failed." }

Write-Host "Desktop build complete: dist\ThothTerminal" -ForegroundColor Green
