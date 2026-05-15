$ErrorActionPreference = "Stop"

$LocalPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (Test-Path $LocalPython) {
    $Python = $LocalPython
} else {
    $Python = "python"
}

Write-Host "Current directory: $(Get-Location)"
Write-Host "Python version:"
& $Python --version
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running environment health check..."
& $Python scripts/check_environment.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running config validation..."
& $Python scripts/check_config.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running pytest..."
& $Python -m pytest
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Local validation passed."
