#!/usr/bin/env bash
# One-command deploy: builds everything, sets up the ocr.dshare.me certificate + DNS in
# Cloudflare (if CLOUDFLARE_API_TOKEN is set) and deploys the CDK stack.
#
#   export AWS_PROFILE=...            # or AWS_ACCESS_KEY_ID/SECRET
#   export AWS_REGION=ap-south-1      # Mumbai (Textract is available there)
#   export SUPERADMIN_EMAIL=you@example.com
#   export CLOUDFLARE_API_TOKEN=...   # optional: Zone > DNS > Edit on dshare.me
#   export DOMAIN=ocr.dshare.me       # optional, this is the default
#   ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${SUPERADMIN_EMAIL:?Set SUPERADMIN_EMAIL to the email that will own the superadmin account}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
export CDK_DEFAULT_REGION="$AWS_REGION"
DOMAIN="${DOMAIN:-ocr.dshare.me}"
STACK="${STACK:-Ocr}"

echo "==> building backend"
./scripts/build-backend.sh

echo "==> building frontend"
(cd frontend && npm ci --silent && npm run build)

echo "==> installing CDK"
(cd infra && npm ci --silent)

CERT_ARN="${CERTIFICATE_ARN:-}"
if [[ -z "$CERT_ARN" && -n "${CLOUDFLARE_API_TOKEN:-}" && -n "$DOMAIN" ]]; then
  echo "==> certificate for $DOMAIN (ACM us-east-1, validated through Cloudflare DNS)"
  python3 -m pip install --quiet boto3
  CERT_ARN=$(python3 scripts/cloudflare_domain.py cert --domain "$DOMAIN" | tee /dev/stderr | sed -n 's/^certificate issued: //p')
fi
if [[ -z "$CERT_ARN" ]]; then
  echo "!! no certificate: the site will be served on its *.cloudfront.net address only"
  DOMAIN=""
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "==> bootstrapping CDK in $ACCOUNT/$AWS_REGION (no-op if already done)"
(cd infra && npx cdk bootstrap "aws://$ACCOUNT/$AWS_REGION" >/dev/null)

echo "==> deploying stack $STACK"
(cd infra && npx cdk deploy "$STACK" --require-approval never \
  -c stackName="$STACK" -c superadminEmail="$SUPERADMIN_EMAIL" -c domainName="$DOMAIN" -c certificateArn="$CERT_ARN" \
  ${ALARM_EMAIL:+-c alarmEmail="$ALARM_EMAIL"} \
  --outputs-file ../cdk-outputs.json)

if [[ -n "$DOMAIN" && -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  echo "==> pointing $DOMAIN at CloudFront"
  python3 scripts/cloudflare_domain.py dns --domain "$DOMAIN" --stack "$STACK" --region "$AWS_REGION"
fi

echo
echo "Done. Outputs saved to cdk-outputs.json"
python3 - <<'PY'
import json
o = json.load(open("cdk-outputs.json"))
for stack in o.values():
    for k in ("SiteUrl", "RazorpaySecretArn", "RazorpayWebhookUrl"):
        if k in stack:
            print(f"  {k}: {stack[k]}")
PY
