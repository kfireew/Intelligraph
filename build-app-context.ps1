$ErrorActionPreference = "Stop"

$outputTar = "intelligraph-app-context.tar"

Write-Host "Creating small app context tar (excludes models, data, wheels, caches, logs)..."

tar -cf $outputTar --exclude="backend/models" --exclude="backend/data" --exclude="backend/wheels" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="*.pyc" --exclude="server.err" --exclude="server.log" backend dist Dockerfile

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: tar creation failed." -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $outputTar).Length / 1MB, 2)
Write-Host ""
Write-Host "Done. $outputTar is $sizeMB MB" -ForegroundColor Green
Write-Host ""
Write-Host "Closed network deployment:"
Write-Host "  1. Copy $outputTar to the closed network machine"
Write-Host "  2. Extract: tar -xf $outputTar"
Write-Host "  3. Build: docker build -t intelligraph:latest ."
Write-Host "  4. Run:   docker run -p 5050:5050 intelligraph:latest"
Write-Host ""
Write-Host "Prerequisite: intelligraph-base:latest must already be loaded:"
Write-Host "  docker load -i intelligraph-base.tar"
