#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

fake_aws="${tmp_dir}/aws"
cat > "$fake_aws" <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$AWS_CALL_LOG"

case "$1 $2" in
  "ecr-public describe-images")
    case "$AWS_FAKE_MODE" in
      absent)
        echo "ImageNotFoundException: image not found" >&2
        exit 254
        ;;
      same)
        printf '{"imageDetails":[{"imageDigest":"sha256:abc123"}]}\n'
        ;;
      different)
        printf '{"imageDetails":[{"imageDigest":"sha256:def456"}]}\n'
        ;;
    esac
    ;;
  "ecr-public batch-get-image")
    printf '{"images":[{"imageManifest":"manifest-json"}]}\n'
    ;;
  "ecr-public put-image")
    printf '{"image":{}}\n'
    ;;
  "ecr-public batch-delete-image")
    printf '{}\n'
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 1
    ;;
esac
FAKE_AWS
chmod +x "$fake_aws"

run_publish() {
  local mode="$1"
  local digest_file="$2"
  local log_file="$3"
  AWS_CLI="$fake_aws" AWS_FAKE_MODE="$mode" AWS_CALL_LOG="$log_file" \
    "${ROOT_DIR}/scripts/publish-public-ecr-release-tags.sh" \
    --repository-name bridge-client \
    --version-tag 1.2.3 \
    --candidate-tag release-candidate-123-1 \
    --digest-file "$digest_file"
}

digest_file="${tmp_dir}/digests.txt"
log_file="${tmp_dir}/calls.log"
printf 'abc123  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n' > "$digest_file"
run_publish absent "$digest_file" "$log_file"

grep -q -- "describe-images .*imageTag=1.2.3" "$log_file"
grep -q -- "put-image .*--image-tag 1.2.3" "$log_file"
grep -q -- "put-image .*--image-tag latest" "$log_file"
if grep -q -- "describe-images .*latest" "$log_file"; then
  echo "latest must not be part of the ECR release preflight" >&2
  exit 1
fi
grep -qx -- "sha256:abc123  public.ecr.aws/conductorone/bridge-client:1.2.3" "$digest_file"

: > "$log_file"
printf 'sha256:abc123  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n' > "$digest_file"
run_publish same "$digest_file" "$log_file"
if grep -q -- "put-image .*--image-tag 1.2.3" "$log_file"; then
  echo "same digest should not rewrite the version tag" >&2
  exit 1
fi
grep -q -- "put-image .*--image-tag latest" "$log_file"

: > "$log_file"
printf 'sha256:abc123  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n' > "$digest_file"
if run_publish different "$digest_file" "$log_file" >"${tmp_dir}/different.out" 2>&1; then
  echo "different existing digest should fail" >&2
  exit 1
fi
grep -q -- "already points at sha256:def456" "${tmp_dir}/different.out"
if grep -q -- "put-image" "$log_file"; then
  echo "different digest must fail before tag publication" >&2
  exit 1
fi

echo "public ecr release tag tests passed"
