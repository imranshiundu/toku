#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
GO_BIN="${GO:-go}"
if ! command -v "$GO_BIN" >/dev/null 2>&1; then
  echo "tiny: go toolchain not found — build skipped (declared)" >&2
  exit 1
fi

mkdir -p dist
CGO_ENABLED=0 "$GO_BIN" build -trimpath -ldflags="-s -w" -o dist/toku-linux-amd64 .
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 "$GO_BIN" build -trimpath -ldflags="-s -w" -o dist/toku-linux-arm64 .
CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=6 "$GO_BIN" build -trimpath -ldflags="-s -w" -o dist/toku-linux-armv6 .
ls -lh dist/
