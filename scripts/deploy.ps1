# Docker build, push and k8s deploy for agent-feedback
# Image: ghcr.io/uengine-oss/agent-feedback:latest
#
# Usage:
#   .\scripts\deploy.ps1              # build only
#   .\scripts\deploy.ps1 -Push        # build + push to GHCR (requires: docker login ghcr.io)
#   .\scripts\deploy.ps1 -Apply       # build + kubectl apply -f k8s/deployment.yaml
#   .\scripts\deploy.ps1 -Push -Apply # build + push + apply

param(
    [switch]$Push,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$IMAGE = "ghcr.io/uengine-oss/agent-feedback:latest"
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

Set-Location $Root

Write-Host "==> Building $IMAGE" -ForegroundColor Cyan
docker build -t $IMAGE .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Push) {
    Write-Host "==> Pushing $IMAGE" -ForegroundColor Cyan
    docker push $IMAGE
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Apply) {
    Write-Host "==> Applying k8s/deployment.yaml" -ForegroundColor Cyan
    kubectl apply -f k8s/deployment.yaml
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "==> Done. Image: $IMAGE" -ForegroundColor Green
