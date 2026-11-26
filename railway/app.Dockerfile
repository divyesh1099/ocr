# Railway "app" service: React site + API + pipeline workers + Tesseract (selfhost/server.py).
# Same image as selfhost/Dockerfile, but builds the frontend itself so Railway can build from git.
FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

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
COPY --from=web /src/frontend/dist/ ./web/
ENV PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=1 WEB_DIR=/app/web
RUN useradd --system --uid 10001 ocr && mkdir -p /tmp && chown ocr /tmp
USER ocr
EXPOSE 8080
CMD ["python", "server.py"]
