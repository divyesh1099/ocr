# OCR

A website for OCR at any scale, from one photo to a 20 GB folder of scanned PDFs. Upload PDFs, images, TIFFs, zip archives (nested zips too) or whole folders. You get back plain text, JSON with line positions and confidence scores, or searchable PDFs. Everything runs serverless on AWS and is served at **https://ocr.divyeshvishwakarma.com** (DNS in Cloudflare).

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
- **Compression** (self-hosted and Railway deployments). After sign-in the home page asks *OCR or Compress?*. Compress jobs shrink PDFs (the three passes from [compressPDF](https://github.com/divyesh1099/compressPDF): PyMuPDF image re-encoding, pikepdf/qpdf, Ghostscript), photos (Pillow, optional resize or WebP/JPEG conversion), video (ffmpeg H.264), audio, and Office/EPUB files (images inside are recompressed). Anything else is zipped. You choose *Smallest file*, *Balanced* or *Best quality*. A file that can't be made smaller comes back unchanged. Code: `backend/src/ocrsvc/handlers/compress.py`, worker queue `ocr-compress`. Compression is free and uses no credits. It isn't offered on the Lambda deployment, which lacks ffmpeg and Ghostscript.
- **Admin page.** Access mode, prices, credit packs, enabled engines, approving or suspending users, granting credits, invitations, and a "force finish" button for a job that gets stuck.

## Architecture

```
Browser ──► CloudFront (ocr.divyeshvishwakarma.com) ──► S3 (React app)
   │                └── /api/* ──► API Gateway (HTTP, Cognito JWT) ──► API Lambda ──► DynamoDB
   │
   └── presigned multipart PUT ──► S3 data bucket  uploads/<user>/<job>/...

WORK (SQS: ordered retries, back-pressure, one DLQ per queue)

submit ─► [ingest] scan uploads ─► classify file ─► zip? fan out batches ─► extract entries
                                  │
                                  ▼
          [process] count pages · reserve credits · split into units
                 │                                   │
                 ▼                                   ▼
        [textract] start Textract            [tesseract] container Lambda (arm64)
                 │  (async ─► SNS ─► [textract-done] collect)          │
                 └──────────────────────┬──────────────────────────────┘
                                        ▼
                    [assemble] merge units ─► results/<job>/files/<path>.txt|json|pdf
                                        ▼
                    [finalize] stream everything into results/<job>/<job>.zip

Every queue has a DLQ ─► DLQ Lambda marks that work failed so the job still finishes,
                        plus a CloudWatch alarm that emails you.

LIFECYCLE (EventBridge)

backend ─PutEvents─► bus "ocr-pipeline"   Job Submitted · Job Completed · Job Failed · Job Stalled · Document Failed
                       ├─ rule ─► CloudWatch Logs /aws/events/ocr-pipeline (audit trail, 90 days)
                       ├─ archive (30 days, replayable)
                       └─ rule (Job Failed | Job Stalled) ─► SNS ─► email

EventBridge Scheduler ─ every 15 min ─► sweeper Lambda
    re-runs the completion check · re-sends a stuck finalize · alerts once on a job running > stall_hours
```

SQS moves the **work**, because it needs ordered retries, back-pressure and per-stage DLQs. EventBridge carries the **lifecycle**: who did what and when, alerts, and future integrations such as webhooks or a "your job is done" email. Publishing an event is best effort and never fails a task.

**Completion tracking.** Each job has one `pending` counter in DynamoDB. A task always adds slots for the work it creates before it releases its own slot, and both steps are guarded by idempotency markers. So SQS redeliveries never double count, and the job finishes exactly once, when the counter reaches zero. `backend/src/ocrsvc/jobs.py` explains the details. Active jobs also sit in a sparse index (`GSI2`), so the sweeper reads only those and costs nothing when the site is idle.

| Path | What |
|---|---|
| `backend/src/ocrsvc/` | Python 3.12 Lambda code (API, Cognito triggers, pipeline workers, sweeper, events) |
| `backend/tesseract/Dockerfile` | Tesseract + poppler container image (arm64) |
| `backend/tests/` | pytest + moto: full pipeline incl. nested zips, credits, access modes, sweeper, real Tesseract OCR |
| `infra/` | Terraform: `storage.tf`, `queues.tf`, `lambda.tf`, `api.tf`, `auth.tf`, `cdn.tf` (ACM + Cloudflare), `events.tf`, `monitoring.tf`, `github.tf` |
| `infra/modules/` | `lambda` (function + least-privilege role + logs), `queue` (queue + DLQ + alarm) |
| `scripts/deploy.sh` | One-command build + `terraform apply` + site upload |
| `scripts/push-tesseract-image.sh` | Builds and pushes the arm64 image (Terraform runs it when the sources change) |
| `.github/workflows/` | `ci.yml` (lint, tests, build, terraform validate), `deploy.yml` (plan + apply through OIDC) |

## Deploy

**You need:**
- An AWS account that has finished activation, with the CLI signed in (`aws login`).
- Terraform 1.10 or newer.
- Node 22 or newer, and Python 3.12.
- Docker with arm64 emulation.
- A Cloudflare API token with **Zone → DNS → Edit** on `divyeshvishwakarma.com`.

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64   # once per boot (x86 machines)
aws login --region ap-south-1
export SUPERADMIN_EMAIL=you@example.com      # this email becomes the superadmin when it signs up
export CLOUDFLARE_API_TOKEN=...              # or DOMAIN= to use the *.cloudfront.net address
./scripts/deploy.sh                          # shows the plan, asks, applies
```

The script:
1. Checks your tools, AWS credentials, Docker and arm64 support.
2. Builds the backend zip and the frontend.
3. Creates the Terraform state bucket on the first run: `ocr-tfstate-<account>-<region>`, versioned, encrypted, with native S3 locking.
4. Runs `terraform plan`, then `terraform apply` once you approve. The apply:
   - Builds and pushes the Tesseract image.
   - Issues the ACM certificate and validates it through Cloudflare.
   - Creates everything else.
   - Points `ocr.divyeshvishwakarma.com` at CloudFront with a DNS-only CNAME.
5. Uploads the site with its `config.json` and invalidates CloudFront.

`PLAN_ONLY=1 ./scripts/deploy.sh` shows what would change without applying anything.

**Faster deploys:**
- `FRONTEND_ONLY=1 ./scripts/deploy.sh` rebuilds and uploads only the website. It skips Terraform and takes about 15 s.
- `FAST=1 ./scripts/deploy.sh` skips Terraform's refresh of existing resources. Use it for code-only changes, and not after you've changed anything by hand in the AWS console.
- The Tesseract image is rebuilt only when its sources change. Docker layer caching cuts a code-only rebuild to about 1 s.
- Terraform doesn't wait for CloudFront to reach every edge location, so CloudFront changes go live a few minutes after `apply` returns.

Then:
1. Confirm the **SNS subscription email** so you receive alarms and job alerts.
2. **Sign up** on the site with `SUPERADMIN_EMAIL`. That account becomes the superadmin automatically.
3. Open **Admin** and choose the access mode. The default is *invite only* with billing off.
4. **Payments (optional):**
   - Put your Razorpay keys in the secret named by `terraform output razorpay_secret_arn`: `{"keyId":"rzp_live_…","keySecret":"…","webhookSecret":"…"}`. Terraform never overwrites this secret.
   - In Razorpay, add a webhook to `razorpay_webhook_url` for the events `payment.captured` and `order.paid`.
   - Turn on **Charge credits** in Admin.

### Deploying from GitHub Actions

The first local deploy creates an IAM role that GitHub can assume through OIDC, so there are no long-lived AWS keys. Only the `production` environment of `divyesh1099/ocr` can assume it. To set it up:
1. Add the secret `AWS_DEPLOY_ROLE_ARN` with the value of `terraform output github_deploy_role_arn`.
2. Add the secret `CLOUDFLARE_API_TOKEN`.
3. Add the repo variable `SUPERADMIN_EMAIL`. Optionally add `AWS_REGION`, `ALARM_EMAIL` and `DOMAIN`.
4. Create the `production` environment. Add required reviewers there if you want a manual approval gate.
5. Set the repo variable `DEPLOY_ENABLED=true`.

From then on, every push to `main` runs CI (lint, tests, frontend build, `terraform validate`) and then `terraform apply`, with only one deploy running at a time. You can also run **Deploy** manually from the Actions tab.

### Before you send real volume

These AWS quotas are low on new accounts. Request increases under **Service Quotas**:
- **Lambda concurrent executions.** Some new accounts get only 10; ask for 1,000.
- **Textract** `StartDocumentTextDetection` TPS and concurrent async jobs. Then raise `textract_concurrency`.
- Tesseract parallelism is set by `tesseract_concurrency` (default 50).

### Settings you can change

Terraform variables (`infra/terraform.tfvars`, or `TF_VAR_<name>`; see `terraform.tfvars.example`):

| Variable | Default | Meaning |
|---|---|---|
| `superadmin_email` | — | Email that becomes the superadmin (required; `deploy.sh` sets it from `SUPERADMIN_EMAIL`) |
| `domain_name` | `ocr.divyeshvishwakarma.com` | Custom domain in Cloudflare (`""` = cloudfront.net address) |
| `alarm_email` | superadmin | Where alarms and job failure/stall alerts are emailed |
| `textract_concurrency` | 5 | Max parallel Textract-start Lambdas |
| `tesseract_concurrency` | 50 | Max parallel Tesseract Lambdas |
| `upload_retention_days` | 7 | Uploaded originals are deleted after this many days |
| `result_retention_days` | 30 | Results are deleted after this many days |
| `stall_hours` | 6 | A job still processing after this many hours sends one "Job Stalled" alert |
| `github_repository` | `divyesh1099/ocr` | Repository allowed to deploy through OIDC (`""` = no role) |

### Observing it

- **Dashboard.** `terraform output dashboard_url` opens a CloudWatch dashboard with queue depth, DLQs, Lambda concurrency, errors and throttles, API traffic, and the latest lifecycle events.
- **Audit trail.** The log group `/aws/events/ocr-pipeline` holds every lifecycle event. The EventBridge archive `ocr-pipeline` can replay them.
- **Alarms by email.** You get an email when a DLQ is not empty, when the API has errors or 5xx responses, and when a job fails or stalls.
- **Tracing.** Every function has X-Ray tracing turned on.

## Costs (rough, ap-south-1)

- **Textract text detection:** about $1.50 per 1,000 pages.
- **Tesseract:** about $0.03–0.06 per 1,000 pages in Lambda compute (arm64, 4 GB, roughly 2–4 s per page).
- **Idle cost:** close to zero. Everything is pay-per-use (S3, DynamoDB on-demand, SQS, Lambda, CloudFront).

Credit prices are set in Admin. The defaults are 2 credits per page for Textract and 1 for Tesseract, with packs from ₹199 for 1,000 credits.

## Local development

```bash
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest
cd frontend && (cd ../infra && terraform output -json frontend_config) > public/config.json
npm install && npm run dev   # http://localhost:5173 talks to the deployed API (CORS allows localhost)
```

For local dev, set `apiBase` in `config.json` to `terraform output -raw api_direct_url` followed by `/api`.

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
