#!/bin/bash

# Sync with deliberately invalid credentials and assert the connector exits
# with a gRPC status code: Unauthenticated (16) or PermissionDenied (7).
#
# Connectors must call exit.LogExit (baton-sdk v0.25.0+) to surface those
# codes. The older main.go pattern always os.Exit(1); treat that as "not
# migrated yet" and skip rather than fail.

set -euo pipefail

GRPC_UNAUTHENTICATED=16
GRPC_PERMISSION_DENIED=7
DEFAULT_INVALID_CREDENTIAL="invalid"

if [ -z "${BATON_CONNECTOR:-}" ]; then
  echo "BATON_CONNECTOR not set"
  exit 1
fi

# Returns 0 if NAME looks like a connector credential env var.
# Only BATON_* names are considered here; required-secrets and
# bad-credentials can name additional vars.
is_credential_var() {
  local name="$1"
  case "$name" in
    BATON_CONNECTOR | BATON_ENTITLEMENT | BATON_PRINCIPAL | BATON_PRINCIPAL_TYPE | BATON)
      return 1
      ;;
  esac
  case "$name" in
    BATON_*) ;;
    *) return 1 ;;
  esac
  # Connection/config, not a secret.
  case "$name" in
    *_URL | *_ENDPOINT | *_PATH | *_HOST | *_ADDRESS | *_TIMEOUT | *_LEVEL | *_FILE | *_DIR)
      return 1
      ;;
  esac
  # Identifiers that often sit next to secrets.
  case "$name" in
    *_KEY_ID | *_CLIENT_ID | *_TENANT_ID | *_ORGANIZATION_ID | *_ORG_ID)
      return 1
      ;;
  esac
  case "$name" in
    *TOKEN* | *PASSWORD* | *SECRET* | *KEY* | *CREDENTIAL* | *PAT | *PASSWD* | *BEARER*)
      return 0
      ;;
  esac
  return 1
}

# Collect unique env var names to overwrite, preserving insertion order.
names=()
seen=" "

add_name() {
  local name="$1"
  case "$seen" in
    *" $name "*) return 0 ;;
  esac
  seen+="$name "
  names+=("$name")
}

while IFS= read -r name; do
  [ -z "$name" ] && continue
  if is_credential_var "$name"; then
    add_name "$name"
  fi
done < <(compgen -e | LC_ALL=C sort)

if [ -n "${REQUIRED_SECRETS:-}" ]; then
  for name in ${REQUIRED_SECRETS//,/ }; do
    [ -z "$name" ] && continue
    add_name "$name"
  done
fi

if [ ${#names[@]} -eq 0 ] && [ -z "${BAD_CREDENTIALS:-}" ]; then
  echo "::notice title=auth-error test skipped::no credential env vars found to invalidate; skipping unauthenticated sync test"
  exit 0
fi

if [ ${#names[@]} -gt 0 ]; then
  for name in "${names[@]}"; do
    export "${name}=${DEFAULT_INVALID_CREDENTIAL}"
  done
fi

# Overlay caller-supplied dummy credentials. Used when a value must parse
# as JWT, PEM, JSON, etc. before the connector will attempt authentication.
if [ -n "${BAD_CREDENTIALS:-}" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    # trim leading whitespace
    line="${line#"${line%%[![:space:]]*}"}"
    [ -z "$line" ] && continue
    [ "${line:0:1}" = "#" ] && continue
    case "$line" in
      *=*) ;;
      *)
        echo "bad-credentials line is not NAME=value: $line"
        exit 1
        ;;
    esac
    name="${line%%=*}"
    value="${line#*=}"
    if [ -z "$name" ]; then
      echo "bad-credentials line is missing NAME: $line"
      exit 1
    fi
    export "${name}=${value}"
    add_name "$name"
  done <<< "$BAD_CREDENTIALS"
fi

if [ ${#names[@]} -eq 0 ]; then
  echo "::notice title=auth-error test skipped::no credential env vars found to invalidate; skipping unauthenticated sync test"
  exit 0
fi

echo "Syncing with invalid credentials in: ${names[*]}"

set +e
"$BATON_CONNECTOR"
exit_code=$?
set -e

case "$exit_code" in
  "$GRPC_UNAUTHENTICATED" | "$GRPC_PERMISSION_DENIED")
    echo "Connector exited $exit_code as expected for invalid credentials."
    exit 0
    ;;
  1)
    echo "::notice title=auth-error test skipped::connector exited 1; unauthenticated sync test requires exit.LogExit (baton-sdk v0.25.0+). Skipping until the connector is updated."
    exit 0
    ;;
  0)
    echo "Connector succeeded (exit 0) with invalid credentials; expected Unauthenticated (16) or PermissionDenied (7)."
    exit 1
    ;;
  *)
    echo "Connector exited $exit_code with invalid credentials; expected Unauthenticated (16) or PermissionDenied (7)."
    echo "If dummy credentials failed config validation, set the bad-credentials input to well-formed invalid values."
    exit 1
    ;;
esac
