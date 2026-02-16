# test_all.ps1
Write-Host "Testing Server..." -ForegroundColor Cyan
uv run --package server pytest -vv

if ($LASTEXITCODE -eq 0) {
    Write-Host "Testing Client..." -ForegroundColor Cyan
    uv run --package client pytest -vv
}
