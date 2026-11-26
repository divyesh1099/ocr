# Deploying on Railway

This runs the self-hosted stack (`selfhost/`) on Railway instead of your own machine. There are four services in one Railway project, all built from this repo:

| Service name (must match) | Config file path | Volume | Public domain |
|---|---|---|---|
| `dynamodb` | `railway/dynamodb.railway.json` | `/data` | none |
| `sqs` | `railway/sqs.railway.json` | `/data` | none |
| `s3` | `railway/s3.railway.json` | `/data` | `ocr-files.divyeshvishwakarma.com` → port **9000** |
| `app` | `railway/app.railway.json` | none | `ocr.divyeshvishwakarma.com` → port **8080** |

The services reach each other at `<name>.railway.internal`, so the names must be exactly the ones above. The project has to be **new**, because environments created after Oct 16, 2025 have IPv4 private networking, which ElasticMQ needs.

## 1. Create the services

1. Railway → **New Project** → **Deploy from GitHub repo** → `divyesh1099/ocr`. Rename that service to `dynamodb`.
2. Add the same repo three more times (**+ Create → GitHub Repo**) and name them `sqs`, `s3` and `app`.
3. In each service, go to **Settings → Config-as-code → Config file path** and enter the path from the table.
4. For `dynamodb`, `sqs` and `s3`, right-click the service → **Attach volume** → mount path `/data`.

## 2. Variables

Make two random keys with `openssl rand -hex 24`.

**s3**
```
RUSTFS_ACCESS_KEY=ocr<random hex>
RUSTFS_SECRET_KEY=<random hex>
```

**app**
```
AWS_REGION=ap-south-1
AWS_DEFAULT_REGION=ap-south-1
AWS_ACCESS_KEY_ID=${{s3.RUSTFS_ACCESS_KEY}}
AWS_SECRET_ACCESS_KEY=${{s3.RUSTFS_SECRET_KEY}}
AWS_ENDPOINT_URL_DYNAMODB=http://dynamodb.railway.internal:8000
AWS_ENDPOINT_URL_SQS=http://sqs.railway.internal:9324
AWS_ENDPOINT_URL_S3=http://s3.railway.internal:9000
S3_ADDRESSING_STYLE=path
PUBLIC_S3_ENDPOINT=https://ocr-files.divyeshvishwakarma.com
SITE_ORIGIN=https://ocr.divyeshvishwakarma.com
TABLE_NAME=ocr-main
DATA_BUCKET=ocr-data
SUPERADMIN_EMAIL=<your email>
ACCESS_TEAM_DOMAIN=<same as selfhost/.env>
ACCESS_AUD=<same as selfhost/.env>
TESSERACT_WORKERS=2
STALL_HOURS=6
```
Set `TESSERACT_WORKERS` explicitly. Inside a Railway container, the CPU count is the host's, so the default would start far too many OCR processes. Raise it to suit your plan's RAM, about 300–500 MB per worker.

Click **Deploy** after you set the variables.

## 3. Domains (Cloudflare)

1. In `app` → **Settings → Networking → Custom Domain**, add `ocr.divyeshvishwakarma.com` on port **8080**. In `s3`, add `ocr-files.divyeshvishwakarma.com` on port **9000**.
2. In Cloudflare DNS, delete the two old Tunnel CNAMEs for those names. Then add the **CNAME and the TXT** record that Railway shows for each name. Without the TXT record, Railway returns 404. Keep both names **proxied (orange cloud)**, because Cloudflare Access needs the proxy.
3. Cloudflare → SSL/TLS → set the mode to **Full**, not Full (strict), which Railway doesn't support with the proxy.
4. Your existing Cloudflare Access application on `ocr.divyeshvishwakarma.com` keeps working unchanged. Leave `ocr-files` outside Access, because presigned URLs authenticate those uploads.

## 4. Shut down the local copy

Once the site works on Railway, stop the local stack and the tunnel (`docker compose -f selfhost/docker-compose.yml --profile public down`). Delete the `ocr-selfhost` tunnel in Cloudflare Zero Trust.

Jobs and accounts saved on your machine don't move over automatically. Railway starts with an empty database, and the superadmin account is created again when you sign in with `SUPERADMIN_EMAIL`.
