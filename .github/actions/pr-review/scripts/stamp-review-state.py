#!/usr/bin/env python3
"""Stamp the sticky summary comment with a review-state marker bound to HEAD.

submit-verdict-review.py refuses to submit a formal review unless the reviewer's
sticky summary comment carries a `<!-- review-state: {"last_reviewed_sha": ...} -->`
marker matching the current HEAD, and fetch-pr-context.py only reuses prior
review state when the marker's `workflow_ref` matches this workflow. That marker
was meant to be emitted by the review agent from its prompt template, but the
agent does not produce it reliably (the sticky-comment path can drop the
trailing HTML comment), so state detection fails closed — every run falls back
to full review mode and no verdict can be submitted.

This step removes that dependency on the model. It runs in the same job,
immediately after a *successful* agent review of the checked-out PR head, so
`git rev-parse HEAD` is exactly the SHA the agent just reviewed; we write that
SHA into the sticky comment's marker, along with the base SHA from
`.github/pr-context.json` and this workflow's ref (both required by
fetch-pr-context.py's state matching). The HEAD binding is preserved — it is
just sourced deterministically from CI instead of an unreliable model output.
If the agent step had failed, the composite action would have stopped before
this step, so a stale comment is never re-stamped to a head it wasn't reviewed
against.

submit-verdict-review.py stays an independent verifier: if this step is skipped,
or no matching comment exists, the gate still refuses. No-ops when there is no
matching summary comment or when the marker already identifies HEAD.

Ported from ductone/github-workflows; adapted to stamp the full
{last_reviewed_sha, base_sha, workflow_ref} marker this repo's state tracking
requires.
"""

import json
import os
import re
import subprocess
import sys

import _gh

# Mirror submit-verdict-review.py: only github-actions-authored comments are
# trusted, and the marker format is identical so the gate reads what we write.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}
REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
PR_CONTEXT_PATH = os.path.join(".github", "pr-context.json")


def gh_api_paginate(endpoint: str) -> list[dict]:
    """Fetch all pages from a REST endpoint via the shared resilient helper."""
    return _gh.rest_paginate(endpoint)


def current_head_sha() -> str:
    """Return the checked-out PR head SHA — what the agent actually reviewed."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def current_base_sha() -> str | None:
    """Return the PR base SHA recorded by fetch-pr-context.py, if available."""
    try:
        with open(PR_CONTEXT_PATH) as f:
            base = json.load(f).get("current_base_sha")
    except (OSError, json.JSONDecodeError):
        return None
    return base or None


def latest_summary_comment(repo: str, pr_number: str, marker: str) -> dict | None:
    """Return the most recent bot summary comment for this reviewer (full object)."""
    comments = gh_api_paginate(f"repos/{repo}/issues/{pr_number}/comments")
    matching = [
        c
        for c in comments
        if c.get("user", {}).get("login") in BOT_LOGINS
        and marker in c.get("body", "")
    ]
    if not matching:
        return None
    matching.sort(key=lambda c: c.get("id", 0))
    return matching[-1]


def reviewed_sha(body: str) -> str | None:
    """Extract last_reviewed_sha from the body's review-state marker, if any."""
    m = REVIEW_STATE_PATTERN.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("last_reviewed_sha")
    except json.JSONDecodeError:
        return None


def already_bound(reviewed: str | None, head: str) -> bool:
    """Whether the existing marker already identifies HEAD (prefix-tolerant,
    matching submit-verdict-review.py's sha_bound_to_head)."""
    if not reviewed or not head:
        return False
    reviewed = reviewed.strip().lower()
    head = head.strip().lower()
    n = min(len(reviewed), len(head))
    return n >= 7 and head[:n] == reviewed[:n]


def build_marker(head: str) -> str:
    """Build the full review-state marker fetch-pr-context.py can match later.

    workflow_ref must round-trip through fetch-pr-context.py's state matching
    (it rejects state whose workflow_ref differs from GITHUB_WORKFLOW_REF), so
    it is stamped from the environment. base_sha is taken from pr-context.json
    so the next run's incremental diff compares against the right base.
    """
    state: dict[str, str] = {"last_reviewed_sha": head}
    base = current_base_sha()
    if base:
        state["base_sha"] = base
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    if workflow_ref:
        state["workflow_ref"] = workflow_ref
    return f"<!-- review-state: {json.dumps(state)} -->"


def main() -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    marker = os.environ.get("SUMMARY_MARKER", "")
    if not repo or not pr_number or not marker:
        print(
            "GITHUB_REPOSITORY, PR_NUMBER, and SUMMARY_MARKER must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    comment = latest_summary_comment(repo, pr_number, marker)
    if comment is None:
        # Nothing to stamp; submit-verdict-review.py reports the missing summary.
        print(f"No bot summary comment matching {marker!r}; nothing to stamp.")
        return

    head = current_head_sha()
    body = comment.get("body", "")
    if already_bound(reviewed_sha(body), head):
        print(f"Summary comment already bound to HEAD ({head[:12]}); no stamp needed.")
        return

    new_marker = build_marker(head)
    stripped = REVIEW_STATE_PATTERN.sub("", body).rstrip()
    new_body = f"{stripped}\n\n{new_marker}\n"

    try:
        _gh.rest(
            "PATCH",
            f"repos/{repo}/issues/comments/{comment['id']}",
            data={"body": new_body},
        )
    except _gh.TerminalError as e:
        print(f"Failed to stamp review-state marker: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Stamped review-state marker on comment {comment['id']} -> {head[:12]}")


if __name__ == "__main__":
    try:
        main()
    except _gh.TransientOutageError as e:
        print(f"GitHub outage while stamping review-state: {e}", file=sys.stderr)
        sys.exit(1)
