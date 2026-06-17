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

Write-Host "Running optional QuestDB health validation..."
& $Python scripts/check_questdb.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running QuestDB schema validation..."
& $Python scripts/check_questdb_schema.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running QuestDB writer validation..."
& $Python scripts/check_questdb_writer.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running QuestDB analysis validation..."
& $Python scripts/check_questdb_analysis.py
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

Write-Host "Running risk filter validation..."
& $Python scripts/check_risk_filter.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running risk logging validation..."
& $Python scripts/check_risk_logging.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running order manager validation..."
& $Python scripts/check_order_manager.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running position state validation..."
& $Python scripts/check_position_state.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running Alpaca paper validation..."
& $Python scripts/check_alpaca_paper.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running execution metrics validation..."
& $Python scripts/check_execution_metrics.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running fill reconciliation validation..."
& $Python scripts/check_fill_reconciliation.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running fake paper loop validation..."
& $Python scripts/check_fake_paper_loop.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running context state cache validation..."
& $Python scripts/check_context_state_cache.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running yfinance proxy validation..."
& $Python scripts/check_yfinance_proxy.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running pytest..."
& $Python -m pytest
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Local validation passed."
