#!/usr/bin/env python3
"""Trusted setup for the review summary comment, run before the model step.

Creates exactly one FRESH provisional working summary comment for this
run/attempt and binds it for the model:

- .github/pr-context.json is updated in place: summary_comment_id becomes the
  new comment's id (every other field is preserved), so the prompt and the
  publication step keep reading one consistent context.
- comment_id=<id> is appended to GITHUB_OUTPUT; the workflow binds it into
  the model step's environment (CLAUDE_COMMENT_ID), where the native
  mcp__github_comment__update_claude_comment tool inherits it. The model
  delivers the summary body through that tool as data — never through a
  shell heredoc — and the host publication step stays unchanged.

A fresh slot per run keeps this tool's target separate from previous reports
and other attempts. Existing broader GitHub API permissions are unchanged.

Safety: before any write, the fetched context is verified against trusted
reality — repository and PR number against the workflow's own environment,
the context's current_sha against BOTH the git checkout and the live PR
head. Any mismatch fails the step with zero mutations (no comment, no
context rewrite, no output).

This script is identical across repositories: it depends only on _gh (same
API everywhere) and encodes no verdict policy. The provisional marker below
is a literal interface contract with the review prompt template, not
classification logic.

Environment in: GH_TOKEN (API auth, via _gh), GITHUB_REPOSITORY, PR_NUMBER,
GITHUB_OUTPUT; REVIEW_SUMMARY_HEADING is cross-checked when set.
"""

import json
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import _gh

PR_CONTEXT_PATH = os.path.join(".github", "pr-context.json")

# Literal interface marker shared with the review template's provisional
# summaries. Defined locally (not imported from any review-state module) so
# this script stays identical across repositories.
PROVISIONAL_MARKER = "_⏳ Provisional — deeper review still in progress._"


def _fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


def _is_valid_heading(value: str) -> bool:
    """One non-empty single-line Markdown heading of the form '### <text>:'."""
    if not value or "\n" in value or "\r" in value:
        return False
    if not value.startswith("### ") or not value.endswith(":"):
        return False
    return bool(value[len("### "):-1].strip())


def _checkout_sha() -> str:
    """The checked-out PR head SHA — what the model will actually review."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        _fail("could not resolve the checked-out HEAD (git rev-parse HEAD failed)")


def _positive_comment_id(comment) -> int:
    """The new comment's id, accepted only as a positive integer from the API."""
    cid = (comment or {}).get("id")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        _fail(f"GitHub returned an invalid comment id: {cid!r}")
    return cid


def provisional_body(heading: str) -> str:
    """The fresh slot's body: heading, provisional marker, and a plain
    in-progress line. No verdict, no count row, no review-state metadata —
    those are host publication concerns, never model output."""
    return (
        f"{heading}\n\n"
        f"{PROVISIONAL_MARKER}\n\n"
        "Review in progress; this comment will be replaced with the full summary.\n"
    )


def create_provisional_slot(repo: str, pr_number: str, body: str) -> int:
    """Create once and fail closed if the response is ambiguous.

    Provisional bodies can be identical across runs, so a matching existing
    comment cannot safely identify the result of this attempt's POST.
    """
    created = _gh.rest(
        "POST",
        f"repos/{repo}/issues/{pr_number}/comments",
        data={"body": body},
        max_attempts=1,
    )
    return _positive_comment_id(created)


def main() -> int:
    repo_env = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    pr_env = (os.environ.get("PR_NUMBER") or "").strip()
    output_path = os.environ.get("GITHUB_OUTPUT") or ""
    if not repo_env or not pr_env:
        _fail("GITHUB_REPOSITORY and PR_NUMBER must be set")
    if not output_path:
        _fail("GITHUB_OUTPUT must be set")

    try:
        with open(PR_CONTEXT_PATH) as f:
            ctx = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _fail(f"could not read {PR_CONTEXT_PATH} (run fetch-pr-context.py first): {e}")

    repo = str(ctx.get("repository") or "")
    pr_number = str(ctx.get("pr_number") or "")
    head_sha = str(ctx.get("current_sha") or "")
    heading = str(ctx.get("summary_heading") or "").strip()
    if not repo or not pr_number or not head_sha:
        _fail("pr-context.json is missing repository, pr_number, or current_sha")
    if repo != repo_env:
        _fail(
            f"pr-context.json repository ({repo!r}) does not match "
            f"GITHUB_REPOSITORY ({repo_env!r})"
        )
    if pr_number != pr_env:
        _fail(
            f"pr-context.json pr_number ({pr_number!r}) does not match "
            f"PR_NUMBER ({pr_env!r})"
        )
    if not _is_valid_heading(heading):
        _fail(f"summary heading {heading!r} is not a valid single-line heading")
    env_heading = (os.environ.get("REVIEW_SUMMARY_HEADING") or "").strip()
    if env_heading and env_heading != heading:
        _fail(
            f"REVIEW_SUMMARY_HEADING ({env_heading!r}) does not match "
            f"pr-context.json summary_heading ({heading!r})"
        )

    # The fresh slot must be bound to the commit the model will review: the
    # context's recorded head must equal BOTH the checkout and the live PR
    # head, or the run's writes would target a stale commit.
    checkout = _checkout_sha()
    if checkout != head_sha:
        _fail(
            f"checkout SHA ({checkout[:12]}) does not match pr-context.json "
            f"current_sha ({head_sha[:12]})"
        )
    pr = _gh.rest("GET", f"repos/{repo}/pulls/{pr_number}")
    live_head = ((pr or {}).get("head") or {}).get("sha") or ""
    if live_head != head_sha:
        _fail(
            "PR head changed between context fetch and summary setup: "
            f"context={head_sha[:12]}, live={live_head[:12]}"
        )

    comment_id = create_provisional_slot(repo, pr_number, provisional_body(heading))

    ctx["summary_comment_id"] = comment_id
    with open(PR_CONTEXT_PATH, "w") as f:
        json.dump(ctx, f, indent=2)
    with open(output_path, "a") as f:
        f.write(f"comment_id={comment_id}\n")

    print(
        f"Fresh provisional summary slot {comment_id} created on "
        f"{repo}#{pr_number} @ {head_sha[:12]}; bound in pr-context.json "
        "and GITHUB_OUTPUT."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
