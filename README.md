# OCR

A website for OCR at any scale, from one photo to a 20 GB folder of scanned PDFs. Upload PDFs, images, TIFFs, zip archives (nested zips too) or whole folders. You get back plain text, JSON with line positions and confidence scores, or searchable PDFs. Everything runs serverless on AWS and is served at **https://ocr.dshare.me** (DNS in Cloudflare).

## What it does

- **Huge inputs.** The browser uploads straight to S3 using presigned multipart uploads (6 parts in parallel, retried on failure). The API never touches file bytes. Zips are read with ranged S3 requests, so a multi-GB archive is never downloaded in full. Its entries are split into batches that many Lambdas unpack in parallel.
- **Two OCR engines.**
  - **AWS Textract.** Best accuracy. PDFs and TIFFs go through the async API. PDFs over 3,000 pages or 450 MB are split into chunks automatically.
  - **Tesseract 5.** Cheap bulk tier running in a container Lambda. Supports English plus 11 Indian languages and can produce searchable PDFs. PDFs are split into 10-page units and OCR'd in parallel, up to 50 Lambdas at once by default.
- **Results.** One file per input (`.txt`, `.json`, `.pdf`) that keeps the original folder and zip structure, plus a single zip of everything with a `manifest.csv` listing what succeeded, failed or was skipped and why.
- **Accounts.** Cognito email sign-in. The superadmin chooses one of three access modes:
  - **Invite only.** Only invited emails can register.
  - **Approval.** Anyone can register, but each account waits until you approve it.
  - **Open.** Anyone can register and buy credits.
- **Billing.** Page credits bought through Razorpay (INR, UPI, cards). Credits are reserved when a file starts, and pages that fail are refunded. Billing can be switched off for a private deployment.
- **Admin page.** Access mode, prices, credit packs, enabled engines, approving or suspending users, granting credits, invitations, and a "force finish" button for a job that gets stuck.

## Architecture

```
Browser ──► CloudFront (ocr.dshare.me) ──► S3 (React app)
   │                └── /api/* ──► API Gateway (HTTP, Cognito JWT) ──► API Lambda ──► DynamoDB
   │
   └── presigned multipart PUT ──► S3 data bucket  uploads/<user>/<job>/...

submit ─► [ingest SQS] scan uploads ─► classify file ─► zip? fan out batches ─► extract entries
                                  │
                                  ▼
          [process SQS] count pages · reserve credits · split into units
                 │                                   │
                 ▼                                   ▼
        [textract SQS] start Textract        [tesseract SQS] container Lambda
                 │  (async ─► SNS ─► [textract-done SQS] collect)      │
                 └──────────────────────┬────────────────────────────┘
                                        ▼
                    [assemble SQS] merge units ─► results/<job>/files/<path>.txt|json|pdf
                                        ▼
                    [finalize SQS] stream everything into results/<job>/<job>.zip

Every queue has a DLQ ─► DLQ Lambda marks that work failed so the job still finishes,
                        plus a CloudWatch alarm that emails you.
```

**Completion tracking.** Each job has one `pending` counter in DynamoDB. A task always adds slots for the work it creates before it releases its own slot, and both steps are guarded by idempotency markers. So SQS redeliveries never double count, and the job finishes exactly once, when the counter reaches zero. `backend/src/ocrsvc/jobs.py` explains the details.

| Path | What |
|---|---|
| `backend/src/ocrsvc/` | Python 3.12 Lambda code (API, Cognito triggers, pipeline workers) |
| `backend/tesseract/Dockerfile` | Tesseract + poppler container image (arm64) |
| `backend/tests/` | pytest + moto: full pipeline incl. nested zips, credits, access modes, real Tesseract OCR |
| `infra/` | AWS CDK (TypeScript) stack |
| `frontend/` | React + Vite app (Amplify UI for sign-in) |
| `scripts/deploy.sh` | One-command build + deploy |
| `scripts/cloudflare_domain.py` | ACM certificate + Cloudflare DNS for `ocr.dshare.me` |

## Deploy

**You need:** an AWS account with the CLI configured, Node 22, Python 3.12, Docker running (it builds the arm64 Tesseract image), and a Cloudflare API token with **Zone → DNS → Edit** on `dshare.me`.

```bash
export AWS_REGION=ap-south-1                 # Mumbai; Textract is available there
export SUPERADMIN_EMAIL=you@example.com      # this email becomes the superadmin when it signs up
export CLOUDFLARE_API_TOKEN=...              # optional, for ocr.dshare.me
./scripts/deploy.sh
```

The script:
1. Builds the backend and frontend.
2. Requests an ACM certificate for `ocr.dshare.me` in us-east-1 and adds the validation record in Cloudflare.
3. Runs `cdk bootstrap` and `cdk deploy`.
4. Points `ocr.dshare.me` at CloudFront with a DNS-only CNAME (not proxied, because CloudFront already handles TLS and caching).

Without a Cloudflare token, the site is served on its `*.cloudfront.net` address instead.

Then:
1. **Sign up** on the site with `SUPERADMIN_EMAIL`. That account becomes the superadmin automatically.
2. Open **Admin** and choose the access mode. The default is *invite only* with billing off.
3. **Payments (optional):**
   - Put your Razorpay keys in the secret named in the `RazorpaySecretArn` output: `{"keyId":"rzp_live_…","keySecret":"…","webhookSecret":"…"}`.
   - In the Razorpay dashboard, add a webhook to the `RazorpayWebhookUrl` output for the events `payment.captured` and `order.paid`, using the same webhook secret.
   - Turn on **Charge credits** in Admin.

### Deploying from GitHub Actions

`.github/workflows/deploy.yml` runs the same script using GitHub OIDC, so there are no long-lived AWS keys. To set it up:
1. Create an IAM role that trusts `token.actions.githubusercontent.com` for `repo:divyesh1099/ocr:*`.
2. Add the role ARN as the `AWS_DEPLOY_ROLE_ARN` secret and your Cloudflare token as `CLOUDFLARE_API_TOKEN`.
3. Add the repo variables `SUPERADMIN_EMAIL` (and optionally `AWS_REGION`, `ALARM_EMAIL`, `DOMAIN`).
4. Run the workflow manually.

`ci.yml` runs lint, tests, the frontend build and `cdk synth` on every push.

### Before you send real volume

These AWS quotas are low on new accounts. Request increases under **Service Quotas**:
- **Lambda concurrent executions.** Some new accounts get only 10; ask for 1,000.
- **Textract** `StartDocumentTextDetection` TPS and concurrent async jobs. Then raise `textractConcurrency` in `infra/cdk.json`.
- Tesseract parallelism is set by `tesseractConcurrency` (default 50).

### Settings you can change

`infra/cdk.json` context (or `-c key=value` on deploy):

| Key | Default | Meaning |
|---|---|---|
| `superadminEmail` | — | Email that becomes the superadmin (required) |
| `domainName` | `ocr.dshare.me` | Custom domain (needs `certificateArn`, which `deploy.sh` fills in) |
| `alarmEmail` | superadmin | Where DLQ alarms are emailed |
| `textractConcurrency` | 5 | Max parallel Textract-start Lambdas |
| `tesseractConcurrency` | 50 | Max parallel Tesseract Lambdas |
| `uploadRetentionDays` | 7 | Uploaded originals are deleted after this many days |
| `resultRetentionDays` | 30 | Results are deleted after this many days |

## Costs (rough, ap-south-1)

- **Textract text detection:** about $1.50 per 1,000 pages.
- **Tesseract:** about $0.03–0.06 per 1,000 pages in Lambda compute (arm64, 4 GB, roughly 2–4 s per page).
- **Idle cost:** close to zero. Everything is pay-per-use (S3, DynamoDB on-demand, SQS, Lambda, CloudFront).

Credit prices are set in Admin. The defaults are 2 credits per page for Textract and 1 for Tesseract, with packs from ₹199 for 1,000 credits.

## Local development

```bash
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest
cd frontend && cp public/config.example.json public/config.json   # fill in values from cdk-outputs.json
npm install && npm run dev   # http://localhost:5173 talks to the deployed API (CORS allows localhost)
```

For local dev, set `apiBase` in `config.json` to the `ApiDirectUrl` output followed by `/api`.

## API

Every route is under `/api`. All routes need a Cognito ID token (`Authorization: Bearer …`) except `/api/public/*` and `/api/webhooks/*`.

| Method | Path | |
|---|---|---|
| GET | `/public/config` | Access mode, prices, packs |
| GET | `/me` | Current user + settings |
| GET/POST | `/jobs` | List / create (`{name, engine, language, outputs}`) |
| GET/DELETE | `/jobs/{id}` | Job status / delete everything |
| POST | `/jobs/{id}/uploads` | Presign up to 200 files (`{files:[{path,size}]}`) |
| POST | `/jobs/{id}/uploads/complete` | Finish a multipart upload |
| POST | `/jobs/{id}/submit` | Start processing |
| GET | `/jobs/{id}/documents` | Per-file status (paged) |
| GET | `/jobs/{id}/documents/{docId}/download?format=txt\|json\|pdf` | Presigned download |
| GET | `/jobs/{id}/download` | Presigned URL of the results zip |
| POST | `/billing/orders`, `/billing/verify` | Razorpay checkout |
| GET/PUT | `/admin/settings` | Superadmin |
| GET/PATCH | `/admin/users[/{sub}]` | Approve / suspend / credits |
| GET/POST/DELETE | `/admin/invites[/{email}]` | Invitations |
| POST | `/admin/jobs/{id}/finalize` | Force-finish a stuck job |
