#!/usr/bin/env bash
# Builds backend/build/: our package + Linux arm64 wheels for the Lambda zip functions.
set -euo pipefail
cd "$(dirname "$0")/../backend"
rm -rf build && mkdir -p build
python3 -m pip install --quiet --upgrade \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all: \
  --target build -r requirements.txt
cp -r src/ocrsvc build/
find build -name '__pycache__' -prune -exec rm -rf {} +
echo "backend/build ready ($(du -sh build | cut -f1))"
