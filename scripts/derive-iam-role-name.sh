#!/usr/bin/env bash
set -euo pipefail

iam_role_name_max_length=64
iam_role_name_hash_length=8

usage() {
  echo "usage: derive-iam-role-name.sh --prefix PREFIX --suffix SUFFIX --output-name NAME" >&2
}

prefix=""
suffix=""
output_name="iam_role_name"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      prefix="${2:-}"
      shift 2
      ;;
    --suffix)
      suffix="${2:-}"
      shift 2
      ;;
    --output-name)
      output_name="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$prefix" ] || [ -z "$suffix" ] || [ -z "$output_name" ]; then
  usage
  echo "prefix, suffix, and output-name are required" >&2
  exit 2
fi

if [[ ! "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "output-name must be a safe GitHub output key" >&2
  exit 2
fi

role_name="${prefix}${suffix}"
if [ "${#role_name}" -gt "$iam_role_name_max_length" ]; then
  if command -v shasum >/dev/null 2>&1; then
    hash_output="$(printf '%s' "$role_name" | shasum -a 256)"
  elif command -v sha256sum >/dev/null 2>&1; then
    hash_output="$(printf '%s' "$role_name" | sha256sum)"
  else
    echo "shasum or sha256sum is required" >&2
    exit 1
  fi
  hash="${hash_output%% *}"
  hash="${hash:0:$iam_role_name_hash_length}"
  keep=$((iam_role_name_max_length - iam_role_name_hash_length - 1))
  role_name="${role_name:0:$keep}-${hash}"
fi

printf '%s=%s\n' "$output_name" "$role_name"
