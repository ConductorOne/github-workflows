#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
files=()
while IFS= read -r -d '' file; do
  files+=("$root/$file")
done < <(git -C "$root" ls-files --cached --others --exclude-standard -z -- '*.sh')
if [ "${#files[@]}" -eq 0 ]; then
  echo 'No repository Bash scripts found' >&2
  exit 1
fi
printf 'Checking %s repository Bash scripts\n' "${#files[@]}"
# Resolve runtime-relative helper imports from each source file's directory.
"${SHELLCHECK:-shellcheck}" --shell=bash --external-sources --source-path=SCRIPTDIR "${files[@]}"
