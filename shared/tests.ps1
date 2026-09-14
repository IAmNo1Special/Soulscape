$RepoRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Testing Server..." -ForegroundColor Cyan
uv run --package server -- python -m pytest "$RepoRoot/server/tests" -vv

if ($LASTEXITCODE -eq 0) {
    Write-Host "Testing Client..." -ForegroundColor Cyan
    uv run --package client -- python -m pytest "$RepoRoot/client/tests" -vv
}
exit $LASTEXITCODE
