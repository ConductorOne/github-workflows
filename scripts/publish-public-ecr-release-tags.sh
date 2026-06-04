#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: publish-public-ecr-release-tags.sh --repository-name REPO --version-tag TAG --candidate-tag TAG --digest-file FILE [--registry-uri URI]

Promotes a pushed candidate Public ECR image to the immutable release version
tag after checking any existing version tag digest. The latest tag is updated
as mutable convenience metadata and is not consulted by the preflight.
USAGE
}

repository_name=""
version_tag=""
candidate_tag=""
digest_file=""
registry_uri="public.ecr.aws/conductorone"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository-name)
      repository_name="${2:-}"
      shift 2
      ;;
    --version-tag)
      version_tag="${2:-}"
      shift 2
      ;;
    --candidate-tag)
      candidate_tag="${2:-}"
      shift 2
      ;;
    --digest-file)
      digest_file="${2:-}"
      shift 2
      ;;
    --registry-uri)
      registry_uri="${2:-}"
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

if [[ -z "$repository_name" || -z "$version_tag" || -z "$candidate_tag" || -z "$digest_file" ]]; then
  usage
  exit 2
fi

if [[ ! -f "$digest_file" ]]; then
  echo "Digest file not found: $digest_file" >&2
  exit 1
fi

aws_cli="${AWS_CLI:-aws}"
image_base="${registry_uri}/${repository_name}"
candidate_ref="${image_base}:${candidate_tag}"
version_ref="${image_base}:${version_tag}"

normalize_digest() {
  local digest="$1"
  if [[ "$digest" == sha256:* ]]; then
    printf '%s\n' "$digest"
  else
    printf 'sha256:%s\n' "$digest"
  fi
}

describe_image_digest() {
  local tag="$1"
  local describe_err describe_json digest
  describe_err="$(mktemp)"
  if describe_json="$("$aws_cli" ecr-public describe-images \
    --repository-name "$repository_name" \
    --image-ids "imageTag=${tag}" \
    --output json 2>"$describe_err")"; then
    rm -f "$describe_err"
    digest="$(jq -r '.imageDetails[0].imageDigest // empty' <<<"$describe_json")"
    if [[ -n "$digest" ]]; then
      normalize_digest "$digest"
    fi
    return 0
  fi

  if grep -Eq 'ImageNotFound|ImageNotFoundException|RepositoryNotFound|RepositoryNotFoundException' "$describe_err"; then
    rm -f "$describe_err"
    return 0
  fi

  cat "$describe_err" >&2
  rm -f "$describe_err"
  return 1
}

candidate_digest=""
while read -r digest ref _; do
  if [[ "$ref" == "$candidate_ref" ]]; then
    candidate_digest="$(normalize_digest "$digest")"
    break
  fi
done < "$digest_file"

if [[ -z "$candidate_digest" ]]; then
  echo "Could not find candidate ref $candidate_ref in $digest_file" >&2
  cat "$digest_file" >&2
  exit 1
fi

existing_digest="$(describe_image_digest "$version_tag")"
version_tag_exists=false
if [[ -n "$existing_digest" ]]; then
  version_tag_exists=true
fi

if [[ -n "$existing_digest" ]]; then
  if [[ "$existing_digest" != "$candidate_digest" ]]; then
    echo "::error::Public ECR tag ${repository_name}:${version_tag} already points at ${existing_digest}, refusing to replace it with ${candidate_digest}" >&2
    exit 1
  fi
  echo "Public ECR version tag already points at ${candidate_digest}; keeping release idempotent"
else
  echo "Public ECR version tag ${repository_name}:${version_tag} is available"
fi

manifest_json="$("$aws_cli" ecr-public batch-get-image \
  --repository-name "$repository_name" \
  --image-ids "imageDigest=${candidate_digest}" \
  --accepted-media-types \
    "application/vnd.oci.image.index.v1+json" \
    "application/vnd.docker.distribution.manifest.list.v2+json" \
    "application/vnd.oci.image.manifest.v1+json" \
    "application/vnd.docker.distribution.manifest.v2+json" \
  --output json)"
manifest="$(jq -r '.images[0].imageManifest // empty' <<<"$manifest_json")"

if [[ -z "$manifest" || "$manifest" == "null" ]]; then
  echo "Could not fetch manifest for ${repository_name}@${candidate_digest}" >&2
  echo "$manifest_json" >&2
  exit 1
fi

if [[ "$version_tag_exists" == false ]]; then
  "$aws_cli" ecr-public put-image \
    --repository-name "$repository_name" \
    --image-manifest "$manifest" \
    --image-tag "$version_tag" >/dev/null
else
  echo "Skipped Public ECR version tag write because ${repository_name}:${version_tag} already has ${candidate_digest}"
fi

published_digest="$(describe_image_digest "$version_tag")"
if [[ -z "$published_digest" ]]; then
  echo "::error::Public ECR tag ${repository_name}:${version_tag} was not found after publication" >&2
  exit 1
fi
if [[ "$published_digest" != "$candidate_digest" ]]; then
  echo "::error::Public ECR tag ${repository_name}:${version_tag} points at ${published_digest} after publication, expected ${candidate_digest}" >&2
  exit 1
fi

"$aws_cli" ecr-public put-image \
  --repository-name "$repository_name" \
  --image-manifest "$manifest" \
  --image-tag latest >/dev/null

printf '%s  %s\n' "$candidate_digest" "$version_ref" > "$digest_file"

if "$aws_cli" ecr-public batch-delete-image \
  --repository-name "$repository_name" \
  --image-ids "imageTag=${candidate_tag}" >/dev/null 2>&1; then
  echo "Removed temporary Public ECR candidate tag ${candidate_tag}"
else
  echo "::warning::Could not remove temporary Public ECR candidate tag ${candidate_tag}"
fi

echo "Published ${version_ref} at ${candidate_digest}"
