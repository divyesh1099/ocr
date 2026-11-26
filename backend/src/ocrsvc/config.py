"""Runtime configuration, read from environment variables set by Terraform (infra/)."""
from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


TABLE_NAME = lambda: env("TABLE_NAME")  # noqa: E731
DATA_BUCKET = lambda: env("DATA_BUCKET")  # noqa: E731
INGEST_QUEUE_URL = lambda: env("INGEST_QUEUE_URL", "")  # noqa: E731
PROCESS_QUEUE_URL = lambda: env("PROCESS_QUEUE_URL", "")  # noqa: E731
TEXTRACT_QUEUE_URL = lambda: env("TEXTRACT_QUEUE_URL", "")  # noqa: E731
TESSERACT_QUEUE_URL = lambda: env("TESSERACT_QUEUE_URL", "")  # noqa: E731
ASSEMBLE_QUEUE_URL = lambda: env("ASSEMBLE_QUEUE_URL", "")  # noqa: E731
FINALIZE_QUEUE_URL = lambda: env("FINALIZE_QUEUE_URL", "")  # noqa: E731
# Compression runs only where its tools are installed (selfhost/, Railway); empty = not offered.
COMPRESS_QUEUE_URL = lambda: env("COMPRESS_QUEUE_URL", "")  # noqa: E731
TEXTRACT_SNS_TOPIC_ARN = lambda: env("TEXTRACT_SNS_TOPIC_ARN", "")  # noqa: E731
TEXTRACT_SNS_ROLE_ARN = lambda: env("TEXTRACT_SNS_ROLE_ARN", "")  # noqa: E731
RAZORPAY_SECRET_ARN = lambda: env("RAZORPAY_SECRET_ARN", "")  # noqa: E731
SUPERADMIN_EMAIL = lambda: env("SUPERADMIN_EMAIL", "").strip().lower()  # noqa: E731
APP_NAME = lambda: env("APP_NAME", "OCR")  # noqa: E731
EVENT_BUS_NAME = lambda: env("EVENT_BUS_NAME", "")  # noqa: E731
STALL_HOURS = lambda: float(env("STALL_HOURS", "6"))  # noqa: E731

# S3 layout inside the single data bucket
UPLOAD_PREFIX = "uploads"    # uploads/<sub>/<jobId>/<relPath>
WORK_PREFIX = "work"         # work/<jobId>/<docId>/...   (intermediate chunks + unit outputs)
RESULT_PREFIX = "results"    # results/<jobId>/files/<relPath>.txt|.json|.pdf, results/<jobId>/<jobId>.zip

# Limits
MAX_ZIP_DEPTH = 3
ZIP_BATCH_SIZE = 200               # zip entries extracted per Lambda invocation
TEXTRACT_SYNC_MAX_BYTES = 5 * 1024 * 1024
TEXTRACT_ASYNC_MAX_PAGES = 3000
TEXTRACT_ASYNC_MAX_BYTES = 450 * 1024 * 1024   # stay under the 500 MB hard limit
TEXTRACT_CHUNK_PAGES = 1000
TESSERACT_CHUNK_PAGES = 10
LOCAL_DOWNLOAD_MAX_BYTES = 9 * 1024 * 1024 * 1024  # Lambda ephemeral storage is 10 GB
