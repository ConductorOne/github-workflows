#!/usr/bin/env bash
# setup-buildx-action can download even with install:false when buildx is absent.
# Use only the runner-bundled plugin and fail closed if it is unavailable.
set -euo pipefail
docker buildx version
docker buildx create --name release --use --driver docker-container \
  --driver-opt image=moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea
docker buildx inspect --bootstrap
