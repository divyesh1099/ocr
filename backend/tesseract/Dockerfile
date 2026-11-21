# Tesseract OCR worker for AWS Lambda (container image, arm64).
# Debian ships current Tesseract 5 + language packs; awslambdaric provides the Lambda runtime API.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr poppler-utils \
      tesseract-ocr-eng tesseract-ocr-hin tesseract-ocr-mar tesseract-ocr-guj tesseract-ocr-ben \
      tesseract-ocr-tam tesseract-ocr-tel tesseract-ocr-kan tesseract-ocr-mal tesseract-ocr-pan \
      tesseract-ocr-urd tesseract-ocr-san \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /var/task
COPY requirements.txt ./
RUN pip install --no-cache-dir awslambdaric boto3 -r requirements.txt
COPY src/ ./

# Tesseract is multi-threaded per page; we parallelise across Lambdas instead.
ENV OMP_THREAD_LIMIT=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ocrsvc.handlers.tesseract.handler"]
