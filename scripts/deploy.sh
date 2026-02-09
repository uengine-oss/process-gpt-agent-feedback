#!/usr/bin/env bash
# Docker build, push and k8s deploy for agent-feedback
# Image: ghcr.io/uengine-oss/agent-feedback:latest
#
# Usage:
#   ./scripts/deploy.sh              # build only
#   ./scripts/deploy.sh --push       # build + push to GHCR (requires: docker login ghcr.io)
#   ./scripts/deploy.sh --apply      # build + kubectl apply -f k8s/deployment.yaml
#   ./scripts/deploy.sh --push --apply

set -e
IMAGE="ghcr.io/uengine-oss/agent-feedback:latest"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Building $IMAGE"
docker build -t "$IMAGE" .

for arg in "$@"; do
  case $arg in
    --push)
      echo "==> Pushing $IMAGE"
      docker push "$IMAGE"
      ;;
    --apply)
      echo "==> Applying k8s/deployment.yaml"
      kubectl apply -f k8s/deployment.yaml
      ;;
  esac
done

echo "==> Done. Image: $IMAGE"
