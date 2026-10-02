#!/usr/bin/env bash
# publish-homebrew-formula.sh - Writes a Homebrew formula to a tap repository
# through the GitHub Contents API, retrying when a concurrent release moved
# the tap's branch.
#
# Every release writes its own file in the tap, so two releases never
# conflict on content; they race on the branch head, and GitHub answers the
# loser with 409 "is at X but expected Y". GoReleaser treats that as final,
# so the formula is rendered with skip_upload and written here instead: read
# the file's current blob SHA, write, and on a 409 read and write again. A
# formula already at the wanted content is left alone, so a re-run writes
# nothing.
#
# Environment:
#   GITHUB_TOKEN           token with contents write on the tap
#   HOMEBREW_TAP_REPO      owner/name of the tap repository
#   FORMULA_FILE           the rendered formula on disk
#   FORMULA_PATH           its path inside the tap, e.g. Formula/baton-foo.rb
#   COMMIT_MESSAGE         commit message for the write
#   GITHUB_API_URL         API base (default https://api.github.com)
#   HOMEBREW_PUSH_ATTEMPTS attempts before giving up (default 5)
#   HOMEBREW_PUSH_DELAY    seconds before the second attempt, doubling after
#                          that (default 2)

set -euo pipefail

: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${HOMEBREW_TAP_REPO:?HOMEBREW_TAP_REPO is required (owner/name)}"
: "${FORMULA_FILE:?FORMULA_FILE is required}"
: "${FORMULA_PATH:?FORMULA_PATH is required}"
: "${COMMIT_MESSAGE:?COMMIT_MESSAGE is required}"

api_url="${GITHUB_API_URL:-https://api.github.com}"
attempts="${HOMEBREW_PUSH_ATTEMPTS:-5}"
delay="${HOMEBREW_PUSH_DELAY:-2}"
if [[ ! "$attempts" =~ ^[1-9][0-9]*$ || ! "$delay" =~ ^[0-9]+$ ]]; then
  echo "homebrew: HOMEBREW_PUSH_ATTEMPTS must be a positive integer and HOMEBREW_PUSH_DELAY a non-negative integer, got: ${attempts} / ${delay}" >&2
  exit 2
fi
delay=$((10#$delay))

if [[ ! -f "$FORMULA_FILE" ]]; then
  echo "homebrew: formula not found at $FORMULA_FILE" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

endpoint="${api_url}/repos/${HOMEBREW_TAP_REPO}/contents/${FORMULA_PATH}"
want_sha="$(git hash-object "$FORMULA_FILE")"
content="$(base64 < "$FORMULA_FILE" | tr -d '\n')"

# request METHOD [DATA_FILE]: prints the HTTP status, body in $tmp_dir/body.
# A transport failure prints 000.
request() {
  local method="$1" data="${2:-}" status
  local args=(
    --silent --show-error --output "$tmp_dir/body" --write-out '%{http_code}'
    --request "$method"
    --header "Authorization: Bearer ${GITHUB_TOKEN}"
    --header "Accept: application/vnd.github+json"
    --header "X-GitHub-Api-Version: 2022-11-28"
  )
  if [[ -n "$data" ]]; then
    args+=(--header "Content-Type: application/json" --data-binary "@${data}")
  fi
  status="$(curl "${args[@]}" "$endpoint")" || status=000
  printf '%s\n' "$status"
}

# retriable STATUS: the branch moved (409), rate limiting, server errors and
# transport failures are worth another attempt; anything else is final.
retriable() {
  case "$1" in
    000|409|429|5??) return 0 ;;
    *) return 1 ;;
  esac
}

# try_once: 0 written or already current, 1 retriable failure, 2 final failure.
try_once() {
  local status current_sha=""
  status="$(request GET)"
  case "$status" in
    200)
      current_sha="$(jq -r '.sha // empty' "$tmp_dir/body")"
      if [[ "$current_sha" == "$want_sha" ]]; then
        echo "homebrew: ${FORMULA_PATH} in ${HOMEBREW_TAP_REPO} already has this content; nothing to write" >&2
        return 0
      fi
      ;;
    404) ;;
    *)
      echo "homebrew: reading ${FORMULA_PATH} failed with HTTP ${status}" >&2
      cat "$tmp_dir/body" >&2 || true
      retriable "$status" && return 1
      return 2
      ;;
  esac

  # Committer matches what GoReleaser wrote for years, so the tap's history
  # reads the same before and after this script took over the write.
  jq -n --arg message "$COMMIT_MESSAGE" --arg content "$content" --arg sha "$current_sha" '
    {message: $message, content: $content, committer: {name: "goreleaserbot", email: "bot@goreleaser.com"}}
    + (if $sha != "" then {sha: $sha} else {} end)' > "$tmp_dir/put.json"
  status="$(request PUT "$tmp_dir/put.json")"
  case "$status" in
    200|201)
      echo "homebrew: wrote ${FORMULA_PATH} to ${HOMEBREW_TAP_REPO}: $(jq -r '.commit.html_url // "commit URL not returned"' "$tmp_dir/body")" >&2
      return 0
      ;;
    *)
      echo "homebrew: writing ${FORMULA_PATH} failed with HTTP ${status}" >&2
      cat "$tmp_dir/body" >&2 || true
      retriable "$status" && return 1
      return 2
      ;;
  esac
}

attempt=1
while :; do
  outcome=0
  try_once || outcome=$?
  case "$outcome" in
    0)
      if [[ $attempt -gt 1 ]]; then
        echo "homebrew: succeeded on attempt $attempt of $attempts" >&2
      fi
      exit 0
      ;;
    2)
      exit 1
      ;;
  esac
  if [[ $attempt -ge $attempts ]]; then
    echo "homebrew: giving up after $attempts attempts" >&2
    exit 1
  fi
  echo "homebrew: attempt $attempt of $attempts failed; retrying in ${delay}s" >&2
  sleep "$delay"
  delay=$((delay * 2))
  attempt=$((attempt + 1))
done
