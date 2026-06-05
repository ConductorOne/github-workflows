#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

fake_aws="${tmp_dir}/aws"
args_log="${tmp_dir}/aws-args.log"
cat > "$fake_aws" <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$AWS_ARGS_LOG"
if [[ "${1:-}" == "s3api" && "${2:-}" == "head-object" ]]; then
  if [[ -n "${FAKE_HEAD_OBJECT_JSON:-}" ]]; then
    printf '%s\n' "$FAKE_HEAD_OBJECT_JSON"
    exit 0
  fi
  exit 255
fi
FAKE_AWS
chmod +x "$fake_aws"

body="${tmp_dir}/artifact.txt"
printf 'artifact\n' > "$body"

AWS_CLI="$fake_aws" AWS_ARGS_LOG="$args_log" \
  "${ROOT_DIR}/scripts/s3-put-object-if-none-match.sh" \
  --bucket release-bucket \
  --key releases/ConductorOne/example/v1.2.3/artifact.txt \
  --body "$body" \
  --content-type text/plain

grep -Fqx -- "s3api" "$args_log"
grep -Fqx -- "put-object" "$args_log"
grep -Fqx -- "--bucket" "$args_log"
grep -Fqx -- "release-bucket" "$args_log"
grep -Fqx -- "--key" "$args_log"
grep -Fqx -- "releases/ConductorOne/example/v1.2.3/artifact.txt" "$args_log"
grep -Fqx -- "--if-none-match" "$args_log"
grep -Fqx -- "*" "$args_log"
grep -Fqx -- "--metadata" "$args_log"

if command -v sha256sum >/dev/null 2>&1; then
  body_sha256="$(sha256sum "$body" | awk '{print $1}')"
else
  body_sha256="$(shasum -a 256 "$body" | awk '{print $1}')"
fi
: > "$args_log"
FAKE_HEAD_OBJECT_JSON="{\"Metadata\":{\"sha256\":\"${body_sha256}\"}}" \
  AWS_CLI="$fake_aws" AWS_ARGS_LOG="$args_log" \
  "${ROOT_DIR}/scripts/s3-put-object-if-none-match.sh" \
  --bucket release-bucket \
  --key releases/ConductorOne/example/v1.2.3/artifact.txt \
  --body "$body" \
  --content-type text/plain

grep -Fqx -- "head-object" "$args_log"
if grep -Fqx -- "put-object" "$args_log"; then
  echo "matching existing object should not be uploaded" >&2
  exit 1
fi

dist_dir="${tmp_dir}/dist"
mkdir -p "$dist_dir"
printf 'zip\n' > "${dist_dir}/example.zip"
printf 'sig\n' > "${dist_dir}/example.zip.sig"
printf 'cert\n' > "${dist_dir}/example.zip.cert"
printf '{}\n' > "${dist_dir}/example.zip.sbom.json"
printf '{}\n' > "${dist_dir}/example.zip.sbom.sigstore.json"
printf 'ignore\n' > "${dist_dir}/ignore.txt"
: > "$args_log"

AWS_CLI="$fake_aws" AWS_ARGS_LOG="$args_log" \
  "${ROOT_DIR}/scripts/upload-release-artifacts.sh" \
  --bucket release-bucket \
  --directory releases/ConductorOne/example/v1.2.3 \
  --base-dir "$dist_dir" \
  --include-sbom-documents

grep -Fq -- "releases/ConductorOne/example/v1.2.3/example.zip" "$args_log"
grep -Fq -- "releases/ConductorOne/example/v1.2.3/example.zip.sig" "$args_log"
grep -Fq -- "releases/ConductorOne/example/v1.2.3/example.zip.cert" "$args_log"
grep -Fq -- "releases/ConductorOne/example/v1.2.3/example.zip.sbom.json" "$args_log"
grep -Fq -- "releases/ConductorOne/example/v1.2.3/example.zip.sbom.sigstore.json" "$args_log"
if grep -Fq -- "ignore.txt" "$args_log"; then
  echo "unexpected upload for ignore.txt" >&2
  exit 1
fi

echo "s3 release upload tests passed"
