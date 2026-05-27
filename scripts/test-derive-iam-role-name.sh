#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="${script_dir}/derive-iam-role-name.sh"

assert_output() {
  local want="$1"
  shift
  local got
  got="$(bash "$script" "$@")"
  if [ "$got" != "$want" ]; then
    echo "got:  $got" >&2
    echo "want: $want" >&2
    exit 1
  fi
}

assert_output \
  "gha_artifacts_role_name=GHA-Artifacts-ConductorOne-baton-axiomatic-jira" \
  --prefix GHA-Artifacts- \
  --suffix ConductorOne-baton-axiomatic-jira \
  --output-name gha_artifacts_role_name

assert_output \
  "gha_artifacts_role_name=GHA-Artifacts-ConductorOne-baton-axiomatic-github-enter-f6552060" \
  --prefix GHA-Artifacts- \
  --suffix ConductorOne-baton-axiomatic-github-enterprise-cloud \
  --output-name gha_artifacts_role_name

assert_output \
  "gha_artifacts_role_name=GHA-Artifacts-ConductorOne-baton-axiomatic-github-enter-571914a9" \
  --prefix GHA-Artifacts- \
  --suffix ConductorOne-baton-axiomatic-github-enterprise-cloud-extra-long-name \
  --output-name gha_artifacts_role_name

assert_output \
  "ecr_push_role_name=GitHubActionsECRPushRole-baton-axiomatic-github-enterprise-cloud" \
  --prefix GitHubActionsECRPushRole- \
  --suffix baton-axiomatic-github-enterprise-cloud \
  --output-name ecr_push_role_name

assert_output \
  "ecr_push_role_name=GitHubActionsECRPushRole-baton-axiomatic-github-enterpr-302f51c5" \
  --prefix GitHubActionsECRPushRole- \
  --suffix baton-axiomatic-github-enterprise-cloud-extra \
  --output-name ecr_push_role_name
