#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: s3-put-object-if-none-match.sh --bucket BUCKET --key KEY --body FILE --content-type TYPE [--cache-control VALUE]

Uploads one immutable S3 object through s3api PutObject with If-None-Match: *.
If an object already exists with matching sha256 metadata, the upload is skipped.
USAGE
}

bucket=""
key=""
body=""
content_type=""
cache_control="public,max-age=31536000,immutable"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)
      bucket="${2:-}"
      shift 2
      ;;
    --key)
      key="${2:-}"
      shift 2
      ;;
    --body)
      body="${2:-}"
      shift 2
      ;;
    --content-type)
      content_type="${2:-}"
      shift 2
      ;;
    --cache-control)
      cache_control="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$bucket" || -z "$key" || -z "$body" || -z "$content_type" ]]; then
  usage
  exit 2
fi

if [[ ! -f "$body" ]]; then
  echo "Body file not found: $body" >&2
  exit 1
fi

aws_cli="${AWS_CLI:-aws}"

compute_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
    return
  fi
  shasum -a 256 "$1" | awk '{print $1}'
}

body_sha256="$(compute_sha256 "$body")"

if existing="$("$aws_cli" s3api head-object --bucket "$bucket" --key "$key" 2>/dev/null)"; then
  existing_sha256="$(printf '%s' "$existing" | jq -r '.Metadata.sha256 // .Metadata.Sha256 // empty')"
  if [[ "$existing_sha256" == "$body_sha256" ]]; then
    echo "S3 object already exists with matching sha256 metadata, skipping: s3://${bucket}/${key}"
    exit 0
  fi
  echo "::error::S3 object already exists with different or missing sha256 metadata: s3://${bucket}/${key}" >&2
  exit 1
fi

"$aws_cli" s3api put-object \
  --bucket "$bucket" \
  --key "$key" \
  --body "$body" \
  --cache-control "$cache_control" \
  --content-type "$content_type" \
  --metadata "sha256=$body_sha256" \
  --if-none-match "*" >/dev/null
