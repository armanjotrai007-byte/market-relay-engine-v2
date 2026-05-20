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

Write-Host "Running contract validation..."
& $Python scripts/check_contracts.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running fixture validation..."
& $Python scripts/check_fixtures.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running historical Parquet validation..."
& $Python scripts/check_historical_parquet.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running DBN inspector validation..."
& $Python scripts/check_dbn_inspector.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running feature builder validation..."
& $Python scripts/check_feature_builder.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running feature parity validation..."
& $Python scripts/check_feature_parity.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running cost model validation..."
& $Python scripts/check_cost_model.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running label builder validation..."
& $Python scripts/check_label_builder.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running pytest..."
& $Python -m pytest
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Local validation passed."
