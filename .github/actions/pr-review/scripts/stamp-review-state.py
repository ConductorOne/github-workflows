#!/usr/bin/env python3
"""Stamp the sticky summary comment with a review-state marker bound to HEAD.

submit-verdict-review.py refuses to submit a formal review unless the reviewer's
sticky summary comment carries a `<!-- review-state: {"last_reviewed_sha": ...} -->`
marker matching the current HEAD, and fetch-pr-context.py only reuses prior
review state when the marker's `workflow_ref` matches this workflow. The agent
does not emit that marker reliably, so CI stamps it deterministically.

This step is a gate, not just a repair tool. It only stamps a summary that is
provably THIS run's FINAL output:

- FRESH: the comment's `updated_at` must be at/after REVIEW_RUN_STARTED_AT
  (captured before the agent step). A successful agent step is not evidence a
  summary was posted — shallow/lazy exits are the motivating failure — so a
  stale comment is never re-stamped into looking current.
- FINAL: a comment containing the provisional (in-progress) line is refused.
  Provisional output must not advance reviewed state; a run that produced only
  provisional output fails here, loudly, as incomplete.
- OWNED: a comment whose existing marker names a DIFFERENT workflow_ref is
  foreign-owned and is never appropriated.

When all gates pass, the marker is canonicalized to exactly
{last_reviewed_sha: HEAD, base_sha, workflow_ref} — a marker with the right SHA
but missing/wrong base or workflow fields is repaired, not skipped.

If the agent step had failed, the composite action stops before this step, so
a stale comment is never re-stamped to a head it wasn't reviewed against.
submit-verdict-review.py re-verifies every gate independently before
submitting; if this step is skipped, the gate still refuses.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import _gh

# Mirror submit-verdict-review.py: only github-actions-authored comments are
# trusted, and the marker format is identical so the gate reads what we write.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}
REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
PR_CONTEXT_PATH = os.path.join(".github", "pr-context.json")

# Must match the provisional line required by prompts/base-pr-review.md and the
# constant in fetch-pr-context.py / submit-verdict-review.py.
PROVISIONAL_MARKER = "_⏳ Provisional — deeper review still in progress._"


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


def run_started_at() -> datetime:
    """Return the run-start timestamp captured before the agent step."""
    raw = os.environ.get("REVIEW_RUN_STARTED_AT", "")
    if not raw:
        print("REVIEW_RUN_STARTED_AT must be set", file=sys.stderr)
        sys.exit(1)
    return _parse_ts(raw)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_fresh(comment: dict, started: datetime) -> bool:
    """Whether the comment was created/updated at or after the run started —
    i.e. it is this run's output, not a prior run's leftover."""
    raw = comment.get("updated_at") or comment.get("created_at") or ""
    if not raw:
        return False
    return _parse_ts(raw) >= started


def is_provisional(body: str) -> bool:
    """Whether a summary comment is provisional (in-progress) output."""
    return PROVISIONAL_MARKER in body


def marker_state(body: str) -> dict | None:
    """Extract the review-state marker JSON from a body, if present and valid."""
    m = REVIEW_STATE_PATTERN.search(body)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def owned_by_this_workflow(state: dict | None, workflow_ref: str) -> bool:
    """A marker that names a different workflow is foreign-owned. A missing
    marker (or missing workflow_ref) carries no ownership claim — the model
    often omits it, and repairing that is this step's purpose."""
    if not state:
        return True
    claimed = state.get("workflow_ref")
    if not claimed:
        return True
    return not workflow_ref or claimed == workflow_ref


def sha_bound_to_head(reviewed: str | None, head: str) -> bool:
    """Whether a reviewed SHA identifies the current HEAD (prefix-tolerant,
    requiring at least 7 hex chars; matches submit-verdict-review.py)."""
    if not reviewed or not head:
        return False
    reviewed = reviewed.strip().lower()
    head = head.strip().lower()
    n = min(len(reviewed), len(head))
    return n >= 7 and head[:n] == reviewed[:n]


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


def canonical_state(head: str) -> dict:
    """Build the full review-state fetch-pr-context.py can match later.

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
    return state


def marker_is_canonical(state: dict | None, canonical: dict, head: str) -> bool:
    """Whether the existing marker already equals the canonical state — every
    required field, not just the SHA."""
    if not state:
        return False
    if not sha_bound_to_head(state.get("last_reviewed_sha"), head):
        return False
    for key, value in canonical.items():
        if key == "last_reviewed_sha":
            continue
        if state.get(key) != value:
            return False
    return True


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

    started = run_started_at()
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")

    comment = latest_summary_comment(repo, pr_number, marker)
    if comment is None:
        # Nothing to stamp; submit-verdict-review.py reports the missing summary.
        print(f"No bot summary comment matching {marker!r}; nothing to stamp.")
        return

    body = comment.get("body", "")

    if not is_fresh(comment, started):
        print(
            "Refusing to stamp: the summary comment was not created or updated "
            f"during this run (updated_at={comment.get('updated_at')!r}, run "
            f"started {started.isoformat()}). A successful agent step is not "
            "evidence a final summary was posted; leaving prior state untouched.",
            file=sys.stderr,
        )
        sys.exit(1)

    if is_provisional(body):
        print(
            "Refusing to stamp: the summary comment is marked provisional "
            "(in-progress). Provisional output must not advance reviewed "
            "state; this run is incomplete and must fail.",
            file=sys.stderr,
        )
        sys.exit(1)

    existing = marker_state(body)
    if not owned_by_this_workflow(existing, workflow_ref):
        print(
            "Refusing to stamp: the summary comment's review-state marker is "
            f"owned by a different workflow ({existing.get('workflow_ref')!r} "
            f"!= {workflow_ref!r}).",
            file=sys.stderr,
        )
        sys.exit(1)

    head = current_head_sha()
    canonical = canonical_state(head)
    if marker_is_canonical(existing, canonical, head):
        print(f"Summary comment already bound to HEAD ({head[:12]}); no stamp needed.")
        return

    new_marker = f"<!-- review-state: {json.dumps(canonical)} -->"
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
