#!/usr/bin/env bash
# One-command deploy with Terraform.
#
#   aws login --region ap-south-1        # or AWS_PROFILE / access keys / GitHub OIDC
#   export SUPERADMIN_EMAIL=you@example.com
#   export CLOUDFLARE_API_TOKEN=...      # Zone > DNS > Edit on the domain's zone
#   ./scripts/deploy.sh                  # shows the plan and asks before applying
#
# Optional: AWS_REGION (ap-south-1), DOMAIN (ocr.divyeshvishwakarma.com; "" = cloudfront.net only),
#           ALARM_EMAIL, AUTO_APPROVE=1 (CI), PLAN_ONLY=1, TF_STATE_BUCKET.
#
# Faster modes:
#   FRONTEND_ONLY=1  rebuild + upload the website only (no Terraform, ~15 s)
#   FAST=1           skip Terraform's refresh of all existing resources (code-only changes;
#                    don't use it after changing things by hand in the AWS console)
#
# Steps: checks -> build backend + frontend -> state bucket -> terraform apply (which also builds
# and pushes the arm64 Tesseract image and sets up ACM + Cloudflare DNS) -> upload the site.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

: "${SUPERADMIN_EMAIL:?Set SUPERADMIN_EMAIL to the email that will own the superadmin account}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
DOMAIN="${DOMAIN-ocr.divyeshvishwakarma.com}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
say "checking tools and credentials"
for tool in aws terraform docker npm python3; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || die "AWS CLI has no working credentials. Run 'aws login --region $AWS_REGION' (or 'aws configure') first."
echo "AWS account $ACCOUNT, region $AWS_REGION"
if [[ -n "$DOMAIN" && -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  die "DOMAIN=$DOMAIN needs CLOUDFLARE_API_TOKEN (Zone > DNS > Edit). Or run with DOMAIN= to use the cloudfront.net address."
fi
if [[ -z "${FRONTEND_ONLY:-}" ]]; then
  docker info >/dev/null 2>&1 || die "Docker is not running"
  docker buildx inspect --bootstrap 2>/dev/null | grep -q 'linux/arm64' \
    || die "Docker cannot build arm64 images. Run once per boot: docker run --privileged --rm tonistiigi/binfmt --install arm64"
fi

# ---------------------------------------------------------------- build
if [[ -z "${FRONTEND_ONLY:-}" ]]; then
  say "building backend"
  ./scripts/build-backend.sh
fi

say "building frontend"
(cd frontend && npm ci --silent --prefer-offline --no-audit --no-fund && npm run build)

# ---------------------------------------------------------------- terraform state bucket
STATE_BUCKET="${TF_STATE_BUCKET:-ocr-tfstate-$ACCOUNT-$AWS_REGION}"
if ! aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  say "creating Terraform state bucket $STATE_BUCKET"
  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
fi

# ---------------------------------------------------------------- terraform
cd infra
say "terraform init"
terraform init -input=false -reconfigure \
  -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="key=ocr/terraform.tfstate" \
  -backend-config="region=$AWS_REGION" >/dev/null

export TF_VAR_superadmin_email="$SUPERADMIN_EMAIL"
export TF_VAR_aws_region="$AWS_REGION"
export TF_VAR_domain_name="$DOMAIN"
[[ -n "${ALARM_EMAIL:-}" ]] && export TF_VAR_alarm_email="$ALARM_EMAIL"
# The Cloudflare provider is always configured; give it a placeholder when no domain is used.
export CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-unused-no-custom-domain-000000000000}"

if [[ -z "${FRONTEND_ONLY:-}" ]]; then
  # 163 resources: read and create them 30 at a time instead of Terraform's default 10.
  TF_FLAGS=(-input=false -parallelism=30)
  [[ -n "${FAST:-}" ]] && TF_FLAGS+=(-refresh=false)

  say "terraform plan${FAST:+ (FAST: no refresh)}"
  terraform plan "${TF_FLAGS[@]}" -out=tfplan
  if [[ -n "${PLAN_ONLY:-}" ]]; then
    echo "PLAN_ONLY set: stopping before apply."
    exit 0
  fi
  if [[ -z "${AUTO_APPROVE:-}" ]]; then
    read -r -p "Apply this plan? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || die "aborted"
  fi

  say "terraform apply"
  terraform apply -input=false -parallelism=30 tfplan
  rm -f tfplan
fi

# ---------------------------------------------------------------- website
say "uploading the website"
WEB_BUCKET=$(terraform output -raw web_bucket)
DIST_ID=$(terraform output -raw cloudfront_distribution_id)
terraform output -json frontend_config > "$ROOT/frontend/dist/config.json"
cd "$ROOT/frontend/dist"
# Hashed assets are immutable; everything else is revalidated on every load.
aws s3 sync assets "s3://$WEB_BUCKET/assets" --delete --cache-control "public,max-age=31536000,immutable" --only-show-errors
aws s3 sync . "s3://$WEB_BUCKET" --delete --exclude "assets/*" --cache-control "no-cache" --only-show-errors
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/index.html" "/config.json" "/" >/dev/null

cd "$ROOT/infra"
say "done"
for k in site_url dashboard_url razorpay_secret_arn razorpay_webhook_url github_deploy_role_arn; do
  printf '  %-24s %s\n' "$k" "$(terraform output -raw "$k")"
done
cat <<EOF

Next:
  1. Confirm the "AWS Notification - Subscription Confirmation" email (alarms + job alerts).
  2. Sign up at the site with $SUPERADMIN_EMAIL - that account becomes the superadmin.
EOF
