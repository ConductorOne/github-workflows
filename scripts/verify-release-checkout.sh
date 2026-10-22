#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ] || [[ ! "${EXPECTED_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || [ -z "${RELEASE_TAG:-}" ]; then
  echo '::error title=release-source::Require a checkout directory, event commit SHA, and release tag' >&2
  exit 1
fi
repo="$1"
git check-ref-format "refs/tags/$RELEASE_TAG"
head_commit="$(git -C "$repo" rev-parse --verify HEAD)"
if ! tag_commit="$(git -C "$repo" rev-parse --verify "refs/tags/$RELEASE_TAG^{commit}")"; then
  echo "::error title=release-source::Cannot peel refs/tags/$RELEASE_TAG to a commit (lightweight and annotated tags must resolve to the event SHA)" >&2
  exit 1
fi
if [ "$head_commit" != "$EXPECTED_COMMIT" ] || [ "$tag_commit" != "$EXPECTED_COMMIT" ]; then
  echo "::error title=release-source::Release source mismatch: HEAD=$head_commit, peeled refs/tags/$RELEASE_TAG=$tag_commit, event SHA=$EXPECTED_COMMIT; both lightweight and annotated tags must resolve to the event commit" >&2
  exit 1
fi
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'commit_sha=%s\n' "$EXPECTED_COMMIT" >> "$GITHUB_OUTPUT"
fi
echo "Verified event commit $EXPECTED_COMMIT and peeled tag $RELEASE_TAG"
