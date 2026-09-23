#!/usr/bin/env python3
"""Submit the review verdict as a formal GitHub PR review.

The review agent records its verdict in the sticky summary comment, but it no
longer submits the formal `gh pr review` itself. That used to be the last
instruction in the prompt, and it proved fragile: it depended on the agent
reliably running a trailing Bash command at the very end of its turn. Claude
Code upgrades have regressed exactly that behavior more than once — the agent
stops after posting the summary comment, so `gh pr review` never runs and the
PR shows a quiet summary with no blocking review (observed on
ConductorOne/baton-axiomatic after the Claude Code 2.1.280 upgrade: zero formal
reviews submitted across a full day of runs).

This script removes that dependency. The agent only has to write an accurate
summary comment; CI reads the verdict out of that comment and submits the
matching `gh pr review` deterministically.

Mode: baseline only. Reads "**Blocking Issues: N**" from the summary and maps
it to --request-changes (N > 0) or --comment (N == 0). This reviewer never
approves: there is deliberately no --approve path in this script.

Reads the verdict from the most recent bot-authored issue comment containing
SUMMARY_MARKER (the sticky summary the agent just posted/updated), and only
when that comment's review-state marker is bound to the current HEAD. Exits
nonzero if no verdict can be found or the review submission fails, so a broken
gate is loud rather than silently green.

Ported from ductone/github-workflows (judge/approve mode stripped).
"""

import json
import os
import re
import subprocess
import sys

import _gh

# Bot logins that post review comments via claude-code-action. Only GitHub
# itself can author comments under these logins (the "[bot]" suffix is reserved
# for apps and cannot be registered by a user), so a PR author cannot spoof a
# verdict comment directly. The hardening below defends the remaining vectors:
# a stale/foreign bot comment being read as if it were this run's verdict.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}

# The blocking-count pattern is anchored to the bold form the prompt template
# emits ("**Blocking Issues: 0**"), so free-text prose in the summary can't be
# misread as the verdict.
BLOCKING_COUNT_PATTERN = re.compile(
    r"\*\*\s*Blocking\s+Issues:\s*(\d+)", re.IGNORECASE
)
# The sticky comment embeds the SHA it reviewed. We require it to match the
# current HEAD before submitting, so a verdict from an earlier (e.g. clean)
# commit can never be replayed against the current (e.g. malicious) head, and
# a comment lacking this marker (a foreign bot comment that merely contains
# the human-readable header) is rejected.
REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)


def gh_api_paginate(endpoint: str) -> list[dict]:
    """Fetch all pages from a REST endpoint via the shared resilient helper."""
    return _gh.rest_paginate(endpoint)


def latest_summary_comment(repo: str, pr_number: str, marker: str) -> str | None:
    """Return the body of the most recent bot summary comment for this reviewer."""
    comments = gh_api_paginate(f"repos/{repo}/issues/{pr_number}/comments")
    matching = [
        c
        for c in comments
        if c.get("user", {}).get("login") in BOT_LOGINS
        and marker in c.get("body", "")
    ]
    if not matching:
        return None
    # The sticky comment is updated in place; if more than one survives, the
    # highest id is the most recently created.
    matching.sort(key=lambda c: c.get("id", 0))
    return matching[-1].get("body", "")


def current_head_sha() -> str:
    """Return the checked-out PR head SHA (what the agent actually reviewed)."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def comment_reviewed_sha(body: str) -> str | None:
    """Extract last_reviewed_sha from the comment's review-state marker."""
    m = REVIEW_STATE_PATTERN.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("last_reviewed_sha")
    except json.JSONDecodeError:
        return None


def sha_bound_to_head(reviewed: str | None, head: str) -> bool:
    """Whether the comment's reviewed SHA identifies the current HEAD.

    Prefix-tolerant so the agent may record an abbreviated SHA, but requires at
    least 7 hex chars so it can't degrade to a trivial/placeholder match — an
    empty value, a missing marker, or the literal "CURRENT_SHA" placeholder all
    fail closed.
    """
    if not reviewed or not head:
        return False
    reviewed = reviewed.strip().lower()
    head = head.strip().lower()
    n = min(len(reviewed), len(head))
    return n >= 7 and head[:n] == reviewed[:n]


def verdict_to_review(body: str) -> tuple[str, str] | None:
    """Map a summary-comment body to (gh review flag, review body).

    Baseline mode only: request changes on any blocking finding, otherwise
    leave a neutral comment. Never approves. Returns None if the blocking
    count could not be parsed.
    """
    m = BLOCKING_COUNT_PATTERN.search(body)
    if not m:
        return None
    blocking = int(m.group(1))
    if blocking > 0:
        return "--request-changes", "Blocking issues found — see review comments."
    return "--comment", "No blocking issues found."


def submit_review(repo: str, pr_number: str, flag: str, body: str) -> None:
    """Submit a formal PR review via `gh pr review`, with transient retry.

    `gh` is the right tool for review submission (handles the reviews API and
    event mapping), so it stays a subprocess; run_gh_cli adds bounded retry on
    transient-looking failures. A terminal failure exits nonzero so a broken
    gate is loud, never silently green."""
    print(f"Submitting review: gh pr review {pr_number} {flag}")
    try:
        _gh.run_gh_cli(["pr", "review", pr_number, flag, "-b", body, "-R", repo])
    except _gh.TerminalError as e:
        print(f"Failed to submit review: {e}", file=sys.stderr)
        sys.exit(1)
    print("Review submitted.")


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

    body = latest_summary_comment(repo, pr_number, marker)
    if body is None:
        print(
            f"No bot summary comment matching {marker!r} found — cannot derive a "
            f"verdict. The review agent may not have posted its summary.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Bind the verdict to the current HEAD. This refuses to act on a stale
    # comment from an earlier commit (e.g. a clean commit that was reviewed
    # before a malicious one was pushed) or a foreign bot comment that lacks
    # the review-state marker but happens to contain the human-readable header.
    head = current_head_sha()
    reviewed = comment_reviewed_sha(body)
    if not sha_bound_to_head(reviewed, head):
        print(
            "Refusing to submit a review: the summary comment's reviewed SHA "
            f"({reviewed}) does not match current HEAD ({head}). The verdict is "
            "not bound to this commit (stale comment, missing review-state "
            "marker, or the agent did not post a fresh summary this run).",
            file=sys.stderr,
        )
        sys.exit(1)

    mapping = verdict_to_review(body)
    if mapping is None:
        print(
            "Could not parse a blocking-issue count from the summary comment.",
            file=sys.stderr,
        )
        sys.exit(1)

    flag, review_body = mapping
    submit_review(repo, pr_number, flag, review_body)


if __name__ == "__main__":
    try:
        main()
    except _gh.TransientOutageError as e:
        # GitHub was down while reading the summary comment or submitting the
        # review. Fail closed: a verdict is never faked, and the check stays
        # red so the review re-runs.
        print(f"GitHub outage while submitting verdict review: {e}", file=sys.stderr)
        sys.exit(1)
