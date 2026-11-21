# Self-hosted OCR app: API + web + pipeline workers, with Tesseract 5 and poppler.
# Build from the repo root:  docker compose -f selfhost/docker-compose.yml build
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr poppler-utils \
      tesseract-ocr-eng tesseract-ocr-hin tesseract-ocr-mar tesseract-ocr-guj tesseract-ocr-ben \
      tesseract-ocr-tam tesseract-ocr-tel tesseract-ocr-kan tesseract-ocr-mal tesseract-ocr-pan \
      tesseract-ocr-urd tesseract-ocr-san \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir "boto3>=1.35" "pyjwt[crypto]>=2.9" -r requirements.txt
COPY backend/src/ ./
COPY selfhost/server.py ./
COPY frontend/dist/ ./web/

ENV PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=1 WEB_DIR=/app/web
RUN useradd --system --uid 10001 ocr && mkdir -p /tmp && chown ocr /tmp
USER ocr
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "server.py"]
