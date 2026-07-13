$ErrorActionPreference = "Stop"

$imageTag = "intelligraph-optimised:latest"
$tarName = "intelligraph-optimised.tar"

Write-Host "Building $imageTag from Dockerfile.base..."
Write-Host "(CPU-only torch — first run takes ~5-10 min)"
Write-Host ""

docker build -f Dockerfile.base -t $imageTag .

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Base image build failed." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Saving to $tarName..."
docker save -o $tarName $imageTag

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker save failed." -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $tarName).Length / 1MB, 2)
Write-Host ""
Write-Host "Done. $tarName is $sizeMB MB" -ForegroundColor Green
Write-Host ""
Write-Host "Closed network setup:"
Write-Host "  1. Copy $tarName to the closed network machine"
Write-Host "  2. Run: docker load -i $tarName"
Write-Host "  3. Then run build-app-context.ps1 to create the small app context tar"
