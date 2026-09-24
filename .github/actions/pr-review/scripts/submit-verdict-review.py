#!/usr/bin/env python3
"""Submit the review verdict as a formal GitHub PR review.

The review agent records its verdict in the sticky summary comment, but it no
longer submits the formal review itself — that trailing model step regressed
repeatedly (the agent stops after the summary and no review is ever posted).
CI reads the verdict out of the summary and submits it deterministically.

This script is a gate. It submits ONLY from a summary that is provably this
run's final output:

- FRESH: the comment's `updated_at` must be at/after REVIEW_RUN_STARTED_AT —
  a successful agent step is not evidence a summary was posted.
- FINAL: a comment containing the provisional (in-progress) line is refused;
  a run that produced only provisional output fails here as incomplete.
- OWNED: the review-state marker's workflow_ref must match this workflow.
- BOUND: the marker's last_reviewed_sha must match the local checkout HEAD,
  AND the live PR head (re-fetched immediately before submitting) must still
  equal that SHA — a push during the run stops publication.
- UNAMBIGUOUS: the verdict comes from exactly one canonical count row
  (`**Blocking Issues: N** | **Suggestions: M** | **Threads Resolved: R**`)
  in its prescribed top-level position — the first non-empty line after the
  summary heading — where "top-level" is determined with CommonMark fence
  rules (backtick or tilde fences; a closer needs the same character and at
  least the opening length with only whitespace after). PR titles, quoted
  findings, fenced example/source text, malformed values, out-of-position
  rows, or multiple candidate rows are all rejected.

Mode: baseline only. N > 0 -> REQUEST_CHANGES, N == 0 -> COMMENT. This
reviewer never approves: there is deliberately no APPROVE path. The review is
submitted via the REST API with an explicit `commit_id` (the reviewed SHA),
so the verdict is bound to the commit it reviewed. Any gate failure exits
nonzero — a broken review is a loud red check, never silent green.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import _gh

# Bot logins that post review comments via claude-code-action. Only GitHub
# itself can author comments under these logins (the "[bot]" suffix is reserved
# for apps and cannot be registered by a user), so a PR author cannot spoof a
# verdict comment directly. The gates below defend the remaining vectors: a
# stale/foreign/provisional comment being read as this run's final verdict.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}

# The canonical verdict row from the summary template, on its own line, with
# all three counts and closing bold markers. Anchoring to the full row means a
# PR title (which precedes the row in the template), quoted findings, or code
# blocks cannot supply the verdict, and malformed values ("0-2") do not parse.
COUNT_ROW_PATTERN = re.compile(
    r"^\*\*Blocking Issues: (\d+)\*\* \| "
    r"\*\*Suggestions: \d+\*\* \| "
    r"\*\*Threads Resolved: \d+\*\*\s*$",
    re.MULTILINE,
)
# The sticky comment embeds the SHA it reviewed; it must match the current
# HEAD, so a verdict from an earlier commit can never be replayed against the
# current one, and a comment lacking this marker is rejected.
REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
# Must match the provisional line required by prompts/base-pr-review.md.
PROVISIONAL_MARKER = "_⏳ Provisional — deeper review still in progress._"


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def run_started_at() -> datetime:
    raw = os.environ.get("REVIEW_RUN_STARTED_AT", "")
    if not raw:
        print("REVIEW_RUN_STARTED_AT must be set", file=sys.stderr)
        sys.exit(1)
    return _parse_ts(raw)


def is_fresh(comment: dict, started: datetime) -> bool:
    raw = comment.get("updated_at") or comment.get("created_at") or ""
    if not raw:
        return False
    return _parse_ts(raw) >= started


def is_provisional(body: str) -> bool:
    return PROVISIONAL_MARKER in body


def marker_state(body: str) -> dict | None:
    m = REVIEW_STATE_PATTERN.search(body)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def summary_candidates(repo: str, pr_number: str, marker: str) -> list[dict]:
    """All bot-authored summary comments for this reviewer, newest id last."""
    comments = _gh.rest_paginate(f"repos/{repo}/issues/{pr_number}/comments")
    matching = [
        c
        for c in comments
        if c.get("user", {}).get("login") in BOT_LOGINS
        and marker in c.get("body", "")
    ]
    matching.sort(key=lambda c: c.get("id", 0))
    return matching


def current_head_sha() -> str:
    """Return the checked-out PR head SHA (what the agent actually reviewed)."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def live_head_sha(repo: str, pr_number: str) -> str:
    """Re-fetch the PR's current head from the API immediately before submit."""
    pr = _gh.rest("GET", f"repos/{repo}/pulls/{pr_number}")
    return pr["head"]["sha"]


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


# A fence opener: up to 3 leading spaces, then 3+ backticks or tildes, then an
# optional info string (CommonMark 0.31.2, fenced code blocks).
_FENCE_OPEN_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A fence closer: up to 3 LITERAL leading spaces (a leading tab is 4 columns —
# content, not a closer), then a delimiter run, then only spaces/tabs. The
# delimiter character and minimum length are checked against the opener.
_FENCE_CLOSE_PATTERN = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")


def _top_level_lines(body: str) -> list[str]:
    """Return the body's lines that are NOT inside a fenced code block.

    Fence handling follows CommonMark: openers and closers use backticks or
    tildes; a closer must use the SAME character, be AT LEAST the opening
    length, and have only whitespace after it (a line like "```example" is an
    opener, never a closer; a shorter run inside a longer fence is content).
    A backtick fence's info string may not contain a backtick. Fenced content
    is untrusted example/source text — the summary template itself ends with
    a fenced "Prompt for AI agents" block — and must never supply the verdict
    or the owning heading.
    """
    lines = []
    fence_char = None
    fence_len = 0
    for line in body.splitlines():
        if fence_char is None:
            m = _FENCE_OPEN_PATTERN.match(line)
            if m:
                fence, info = m.group(1), m.group(2)
                if fence[0] == "`" and "`" in info:
                    # Not a valid backtick-fence opener; ordinary text.
                    lines.append(line)
                    continue
                fence_char, fence_len = fence[0], len(fence)
                continue
            lines.append(line)
            continue
        # Inside a fence: only a valid closer ends it. The closer grammar is
        # anchored: 0-3 literal leading spaces (a leading tab is 4 columns,
        # i.e. content), the matching delimiter repeated at least the opening
        # length, and only spaces/tabs afterward.
        closer = _FENCE_CLOSE_PATTERN.match(line)
        if closer:
            delimiter = closer.group(1)
            if delimiter[0] == fence_char and len(delimiter) >= fence_len:
                fence_char = None
                fence_len = 0
        # Fence openers/closers and fenced content are never top-level lines.
    return lines


def parse_blocking_count(body: str, heading: str) -> int | None:
    """Extract the blocking-issue count from the summary's metadata row.

    The verdict is accepted ONLY from exactly one canonical count row sitting
    in its prescribed top-level position: the first non-empty line after the
    summary heading, where both the heading and the row are top-level lines
    (never inside a fenced code block, per CommonMark fence rules). Returns
    None — reject — when the row is absent, malformed, out of position, or
    when more than one canonical row remains at top level (ambiguous).
    """
    lines = _top_level_lines(body)
    rows = [line for line in lines if COUNT_ROW_PATTERN.match(line)]
    if len(rows) != 1:
        return None
    for i, line in enumerate(lines):
        if line.startswith(heading):
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                if COUNT_ROW_PATTERN.match(nxt):
                    return int(COUNT_ROW_PATTERN.match(nxt).group(1))
                return None
            return None
    return None


def verdict_to_review(body: str, heading: str) -> tuple[str, str] | None:
    """Map a summary-comment body to (review event, review body).

    Baseline mode only: request changes on any blocking finding, otherwise
    leave a neutral comment. Never approves. Returns None if the blocking
    count could not be parsed unambiguously from the summary's metadata row.
    """
    blocking = parse_blocking_count(body, heading)
    if blocking is None:
        return None
    if blocking > 0:
        return "REQUEST_CHANGES", "Blocking issues found — see review comments."
    return "COMMENT", "No blocking issues found."


def select_final_summary(
    candidates: list[dict], started: datetime
) -> tuple[dict | None, str | None]:
    """Pick this run's final summary from the candidates, newest first.

    Returns (comment, rejection_reason). A rejection_reason is set when
    candidates exist but none qualifies — the run produced output that cannot
    be treated as a final verdict, which must fail loudly.
    """
    saw_stale = False
    for comment in reversed(candidates):
        body = comment.get("body", "")
        if not is_fresh(comment, started):
            saw_stale = True
            continue
        if is_provisional(body):
            return None, (
                "the newest summary from this run is marked provisional "
                "(in-progress); the run is incomplete and no verdict may be "
                "submitted"
            )
        return comment, None
    if saw_stale:
        return None, (
            "no summary comment was created or updated during this run; a "
            "successful agent step is not evidence a final summary was posted"
        )
    return None, None


def submit_review(repo: str, pr_number: str, commit: str, event: str, body: str) -> None:
    """Submit a formal PR review via the REST API, bound to the reviewed commit.

    `gh pr review` cannot carry a commit argument, so submission goes through
    POST /pulls/{n}/reviews with an explicit commit_id — the verdict is bound
    to the SHA that was actually reviewed."""
    print(f"Submitting review: POST pulls/{pr_number}/reviews event={event} commit={commit[:12]}")
    try:
        _gh.rest(
            "POST",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            data={"commit_id": commit, "event": event, "body": body},
        )
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

    started = run_started_at()
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")

    candidates = summary_candidates(repo, pr_number, marker)
    if not candidates:
        print(
            f"No bot summary comment matching {marker!r} found — cannot derive a "
            f"verdict. The review agent may not have posted its summary.",
            file=sys.stderr,
        )
        sys.exit(1)

    comment, rejection = select_final_summary(candidates, started)
    if comment is None:
        print(f"Refusing to submit a review: {rejection}.", file=sys.stderr)
        sys.exit(1)

    body = comment.get("body", "")

    # Ownership: the verdict must belong to this workflow, not a foreign one
    # whose heading happens to match.
    state = marker_state(body)
    claimed_ref = (state or {}).get("workflow_ref")
    if workflow_ref and claimed_ref and claimed_ref != workflow_ref:
        print(
            "Refusing to submit a review: the summary's review-state marker is "
            f"owned by a different workflow ({claimed_ref!r} != {workflow_ref!r}).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Bind the verdict to the reviewed commit. This refuses to act on a stale
    # comment from an earlier commit or a comment lacking the marker.
    head = current_head_sha()
    reviewed = (state or {}).get("last_reviewed_sha")
    if not sha_bound_to_head(reviewed, head):
        print(
            "Refusing to submit a review: the summary comment's reviewed SHA "
            f"({reviewed}) does not match current HEAD ({head}). The verdict is "
            "not bound to this commit (stale comment, missing review-state "
            "marker, or the agent did not post a fresh summary this run).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Bind to the LIVE PR head: a push during the run stops publication. The
    # prompt's head guard covers the agent's own posts; this covers the CI
    # submission the agent no longer performs.
    live = live_head_sha(repo, pr_number)
    if live != head:
        print(
            "Refusing to submit a review: the PR head changed during the run "
            f"(reviewed {head}, live {live}). The verdict belongs to a commit "
            "that is no longer current.",
            file=sys.stderr,
        )
        sys.exit(1)

    mapping = verdict_to_review(body, marker)
    if mapping is None:
        print(
            "Could not parse an unambiguous blocking-issue count from the "
            "summary comment (need exactly one canonical count row — "
            "'**Blocking Issues: N** | **Suggestions: M** | **Threads Resolved: R**' — "
            "as the first non-empty line after the summary heading; fenced "
            "code blocks are ignored).",
            file=sys.stderr,
        )
        sys.exit(1)

    event, review_body = mapping
    submit_review(repo, pr_number, head, event, review_body)


if __name__ == "__main__":
    try:
        main()
    except _gh.TransientOutageError as e:
        # GitHub was down while reading the summary comment or submitting the
        # review. Fail closed: a verdict is never faked, and the check stays
        # red so the review re-runs.
        print(f"GitHub outage while submitting verdict review: {e}", file=sys.stderr)
        sys.exit(1)
