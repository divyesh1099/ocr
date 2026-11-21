#!/usr/bin/env bash
# Builds the Tesseract Lambda image for arm64 and pushes it to ECR.
# Called by Terraform (infra/lambda.tf) whenever the image sources change.
#   IMAGE_URI=<acct>.dkr.ecr.<region>.amazonaws.com/ocr-tesseract:<tag> AWS_REGION=... BACKEND_DIR=... ./push-tesseract-image.sh
set -euo pipefail
: "${IMAGE_URI:?}" "${AWS_REGION:?}" "${BACKEND_DIR:?}"
REGISTRY="${IMAGE_URI%%/*}"

if ! docker buildx inspect --bootstrap 2>/dev/null | grep -q 'linux/arm64'; then
  echo "Docker cannot build arm64 images on this machine. Enable emulation once per boot with:" >&2
  echo "  docker run --privileged --rm tonistiigi/binfmt --install arm64" >&2
  exit 1
fi

echo "==> building $IMAGE_URI (linux/arm64)"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
# --provenance=false: Lambda rejects image indexes that carry attestation manifests.
docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
  -f "$BACKEND_DIR/tesseract/Dockerfile" -t "$IMAGE_URI" --push "$BACKEND_DIR"
