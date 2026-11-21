#!/usr/bin/env bash
# Creates (or reuses) the Cloudflare Tunnel "ocr-selfhost", points the site and file hostnames at
# it, writes selfhost/cloudflared/config.yml and starts the tunnel container.
# Needs ~/.cloudflared/cert.pem from `cloudflared tunnel login`.
#   SITE=ocr.divyeshvishwakarma.com FILES=ocr-files.divyeshvishwakarma.com ./selfhost/tunnel.sh
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=ocr-selfhost
SITE="${SITE:-ocr.divyeshvishwakarma.com}"
FILES="${FILES:-ocr-files.divyeshvishwakarma.com}"
DIR=selfhost/cloudflared
mkdir -p "$DIR"

ID=$(cloudflared tunnel list --name "$NAME" --output json | python3 -c 'import json,sys;t=json.load(sys.stdin);print(t[0]["id"] if t else "")')
if [[ -z "$ID" ]]; then
  cloudflared tunnel create "$NAME" >/dev/null
  ID=$(cloudflared tunnel list --name "$NAME" --output json | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
fi
cp "$HOME/.cloudflared/$ID.json" "$DIR/credentials.json"
chmod 600 "$DIR/credentials.json"

for host in "$SITE" "$FILES"; do
  cloudflared tunnel route dns --overwrite-dns "$NAME" "$host"
done

cat > "$DIR/config.yml" <<EOT
tunnel: $ID
credentials-file: /etc/cloudflared/credentials.json
ingress:
  - hostname: $SITE
    service: http://app:8080
  - hostname: $FILES
    service: http://s3:9000
    originRequest:
      # Presigned URLs are signed for this host name; pass it through unchanged.
      httpHostHeader: $FILES
  - service: http_status:404
EOT

ENV=selfhost/.env
sed -i "s|^SITE_ORIGIN=.*|SITE_ORIGIN=https://$SITE|; s|^PUBLIC_S3_ENDPOINT=.*|PUBLIC_S3_ENDPOINT=https://$FILES|" "$ENV"
./selfhost/up.sh public
echo "Tunnel $NAME ($ID) serves https://$SITE and https://$FILES"
