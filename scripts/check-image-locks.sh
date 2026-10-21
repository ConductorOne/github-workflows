#!/usr/bin/env bash
set -euo pipefail

# Include dotfiles/templates and newly added files, but not ignored build output.
cd "$(git rev-parse --show-toplevel)"

digest_pattern='@sha256:[a-f0-9]{64}$'

git ls-files --cached --others --exclude-standard -z | (
failed=0
while IFS= read -r -d '' file; do
  # These contain the check's patterns and deliberately invalid test fixtures.
  case "$file" in
    scripts/check-image-locks.sh|scripts/test-check-image-locks.py) continue ;;
  esac
  [[ -f "$file" ]] || continue

  from_pattern='FROM'
  case "$file" in
    *[Dd]ockerfile*) from_pattern='[Ff][Rr][Oo][Mm]' ;;
  esac
  status=0
  matches=$(grep -nIE "^[[:space:]]*$from_pattern[[:space:]]|image=|^[[:space:]]*image:" "$file") || status=$?
  if (( status > 1 )); then
    exit "$status"
  fi
  [[ -n "$matches" ]] || continue

  while IFS= read -r match; do
    line_number=${match%%:*}
    line=${match#*:}
    references=()
    if [[ "$line" =~ ^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]] ]]; then
      read -r -a fields <<< "$line"
      index=1
      # Docker permits --platform before the base image.
      if [[ "${fields[1]:-}" == --platform=* ]]; then
        index=2
      fi
      references+=("${fields[$index]:-}")
    fi
    # Check each helper separately, even when a line contains several images.
    # Also cover setup-qemu-action's YAML `image:` input (not just image=).
    while IFS= read -r reference; do
      references+=("$reference")
    done < <(printf '%s\n' "$line" | grep -oE "image=[^[:space:],]*|^[[:space:]]*image:[[:space:]]*[^[:space:]]*" | sed -E 's/^[[:space:]]*image[=:][[:space:]]*//' || true)

    for reference in "${references[@]}"; do
      reference=${reference//\"/}
      reference=${reference//\'/}
      reference=${reference%\\}
      if [[ ! "$reference" =~ $digest_pattern ]]; then
        printf '%s:%s: image must be pinned with @sha256:<64 hex characters>: %s\n' "$file" "$line_number" "$reference" >&2
        # Do not stop at the first diagnostic, including across files.
        failed=1
      fi
    done
  done <<< "$matches"
done
exit "$failed"
)
