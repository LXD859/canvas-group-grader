$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
    else { throw 'Please install Python 3.11 or newer.' }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create environment.' }
}
$graderPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
& $graderPython -c "import sys; assert sys.version_info >= (3,11)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required.' }
& $graderPython -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Open http://127.0.0.1:8001 . Press Ctrl+C to stop.'
& $graderPython -m uvicorn grader.main:create_app --factory --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
