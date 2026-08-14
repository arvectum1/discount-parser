$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

Remove-Item -Recurse -Force dist-ui, dist-worker, build-ui, build-worker, delivery -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force delivery\app | Out-Null

pyinstaller --noconfirm --clean --onedir --noconsole `
  --distpath dist-ui `
  --workpath build-ui `
  --name DiscountParser `
  --hidden-import src.web.app `
  --hidden-import src.web.application `
  --hidden-import src.web.management_pages `
  --hidden-import src.web.system_routes `
  --hidden-import src.web.onboarding_routes `
  --hidden-import src.web.source_registry_routes `
  --hidden-import src.web.source_registry_static_routes `
  --collect-submodules src.modules.source_registry `
  --collect-all uvicorn `
  --collect-all python_calamine `
  src/distribution_entry.py

pyinstaller --noconfirm --clean --onefile --console `
  --distpath dist-worker `
  --workpath build-worker `
  --name DiscountParserWorker `
  --collect-submodules src.modules.source_registry `
  --collect-all python_calamine `
  src/worker_entry.py

Copy-Item dist-ui\DiscountParser\* delivery\app -Recurse -Force
Copy-Item dist-worker\DiscountParserWorker.exe delivery\app\DiscountParserWorker.exe -Force
Copy-Item config delivery\app\config -Recurse -Force
Copy-Item migrations delivery\app\migrations -Recurse -Force
Copy-Item alembic.ini delivery\app\alembic.ini -Force
Copy-Item .env.example delivery\app\.env.example -Force
Copy-Item packaging\windows\install.bat delivery\install.bat -Force

Push-Location delivery\app
.\DiscountParserWorker.exe migrate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
.\DiscountParserWorker.exe doctor
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# The frozen smoke database validates migrations only. It must never be
# shipped in the installer, otherwise an update could overwrite or shadow the
# customer's persistent SQLite database in %LOCALAPPDATA%\DiscountParser.
Remove-Item .\discount_parser.db, .\discount_parser.db-wal, .\discount_parser.db-shm -Force -ErrorAction SilentlyContinue
if (Test-Path .\discount_parser.db) { throw "Smoke database must not be packaged" }
Pop-Location

# Strip build/CI artifacts from the staged payload so they never reach the
# installer: bytecode caches, rejected-patch leftovers, and pytest caches.
Get-ChildItem delivery\app -Recurse -Force -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem delivery\app -Recurse -Force -Include *.pyc, *.rej, .pytest_cache -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$IsccCandidates = @(
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe",
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
$IsccPath = if ($Iscc) { $Iscc.Source } else { $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if (-not $IsccPath) {
  throw "Inno Setup 6 not found. Install Inno Setup 6 or place ISCC.exe in one of: $($IsccCandidates -join ', ')"
}

& $IsccPath "packaging\windows\installer.iss"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Copy-Item "packaging\windows\output\DiscountParser-Setup.exe" "delivery\DiscountParser-Setup.exe" -Force

Write-Host "LOCAL WINDOWS DELIVERY BUILD: PASSED"
Write-Host "Installer: delivery\DiscountParser-Setup.exe"
