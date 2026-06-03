#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: upload-release-artifacts.sh --bucket BUCKET --directory S3_DIRECTORY --base-dir DIR [--include-sbom-documents]

Uploads immutable release artifacts from DIR to s3://BUCKET/S3_DIRECTORY with
If-None-Match: *. Raw *.sbom.json documents are only uploaded when requested.
USAGE
}

bucket=""
directory=""
base_dir=""
include_sbom_documents=false
cache_control="public,max-age=31536000,immutable"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)
      bucket="${2:-}"
      shift 2
      ;;
    --directory)
      directory="${2:-}"
      shift 2
      ;;
    --base-dir)
      base_dir="${2:-}"
      shift 2
      ;;
    --include-sbom-documents)
      include_sbom_documents=true
      shift
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

if [[ -z "$bucket" || -z "$directory" || -z "$base_dir" ]]; then
  usage
  exit 2
fi

if [[ ! -d "$base_dir" ]]; then
  echo "Base directory not found: $base_dir" >&2
  exit 1
fi

content_type_for() {
  local name="$1"
  case "$name" in
    *.tar.gz)
      printf '%s\n' "application/gzip"
      ;;
    *.zip)
      printf '%s\n' "application/zip"
      ;;
    *.msi)
      printf '%s\n' "application/x-msi"
      ;;
    *.sig)
      printf '%s\n' "application/octet-stream"
      ;;
    *.cert)
      printf '%s\n' "application/x-pem-file"
      ;;
    *.sigstore.json)
      printf '%s\n' "application/json"
      ;;
    *.sbom.json)
      if [[ "$include_sbom_documents" == true ]]; then
        printf '%s\n' "application/json"
      fi
      ;;
  esac
  return 0
}

upload_count=0
while IFS= read -r -d '' file; do
  name="$(basename "$file")"
  content_type="$(content_type_for "$name")"
  if [[ -z "$content_type" ]]; then
    continue
  fi

  echo "Uploading $name to S3 without overwrite..."
  "$(dirname "$0")/s3-put-object-if-none-match.sh" \
    --bucket "$bucket" \
    --key "${directory}/${name}" \
    --body "$file" \
    --cache-control "$cache_control" \
    --content-type "$content_type"
  upload_count=$((upload_count + 1))
done < <(find "$base_dir" -maxdepth 1 -type f -print0)

if [[ "$upload_count" -eq 0 ]]; then
  echo "No release artifacts found in $base_dir" >&2
  exit 1
fi

echo "Uploaded ${upload_count} release artifacts to S3"
