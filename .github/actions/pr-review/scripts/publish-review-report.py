#!/usr/bin/env python3
"""Publish the completed review report and submit the commit-bound verdict.

The review agent records its verdict in a WORKING summary comment (provisional
first, final last) and never writes review-state metadata. After a successful
agent run, this trusted CI step finalizes the run:

1. Selects this run's FRESH, FINAL working output — a bot summary comment
   matching the summary heading that was created/updated at or after
   REVIEW_RUN_STARTED_AT, is not provisional, carries no foreign or
   malformed marker, and was not already consumed by a published report
   (republishing consumed output would fabricate a verdict without fresh
   model work). Completed reports and superseded comments are never working
   output; a successful agent step is not evidence a summary was posted, so
   stale-only output fails here. Before any NEW publication, an owned
   completed report whose attempt started LATER than this one (comparing
   actual attempt start times from the persisted started_at, never run-ID
   order) makes this attempt obsolete: it fails closed. Legacy reports
   without started_at never obsolete an attempt; an unparseable started_at
   is left untouched.
2. Validates the baseline canonical fields: exactly one canonical count row
   (`**Blocking Issues: N** | **Suggestions: M** | **Threads Resolved: R**`)
   in its prescribed top-level position (CommonMark fence rules), and that
   the live PR head still equals the checked-out HEAD the agent reviewed.
3. POSTs a NEW report comment: the working body plus a visible
   reviewed-commit link and CI-owned review-state metadata
   {last_reviewed_sha, base_sha, workflow_ref, run_id, run_attempt,
   summary_marker, verdict_mode, publication: "pending"}. Editing the
   working comment is NOT publication — the report is a separate comment, so
   the previous completed report survives every failure before this point.
4. Submits the formal PR review with an explicit commit_id, linking directly
   to the new report. Baseline mode only: N > 0 -> REQUEST_CHANGES,
   N == 0 -> COMMENT. Never approves. The live head is re-checked immediately
   before the vote. An existing review is recognized as this run's formal
   result ONLY by exact binding — host identity marker + report_comment_id +
   GitHub's commit_id + the expected submitted state; an identity-matching
   but inconsistently bound review fails closed, and a completed report
   whose review was later deleted or dismissed is never recreated.
5. Transitions the report to publication="completed" — a completed report is
   the report PLUS its required formal result, so a report whose review
   never landed stays pending: preserved (and reconciled by identity) for a
   same-attempt retry, but never the next run's completed-state baseline and
   never a model-mutable working slot. The transition retains the report's
   original snapshot metadata and flips only the publication status.
6. ONLY after the report is completed and the formal review exists,
   supersedes the exact previous owned reports OLDER than the new one
   (including any pending leftover from an interrupted publication — a
   replay never retires newer output) and the exact consumed working
   comment, recovered by the identity (id + timestamp) the report persisted
   at publication — never a later run's reused slot. Each body is retained
   inside a collapsed <details> section linking to the new report. Human
   comments and inline threads are never touched. Cleanup failure warns but
   does not fail the run and never destroys a useful report; re-running is
   safe (already-superseded comments are skipped).

Idempotency: the report's metadata identifies its publication (workflow +
summary marker + verdict mode + run + attempt + head). A repeated
finalization of the SAME run/attempt reuses the published report and does not
create another formal review — the review carries a host-owned
<!-- review-publication: ... --> identity marker linking its report so an
existing review is recognized. Creation POSTs are non-idempotent, so they run
with max_attempts=1: after an ambiguous timeout/5xx the script reconciles by
identity (re-listing and matching) before concluding anything, and fails
closed — preserving all previous output — when no result materialized. An
intentional new run/attempt gets a new report.

Any gate failure exits nonzero — a broken review is a loud red check, never
silent green.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import _gh
import _review_state

VERDICT_MODE = "baseline"
PR_CONTEXT_PATH = os.path.join(".github", "pr-context.json")

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

# Host-owned identity marker embedded in the formal review body, linking the
# review to its report so a repeated finalization recognizes it.
VERDICT_MARKER_PATTERN = re.compile(
    r"<!--\s*review-publication:\s*(\{.*?\})\s*-->", re.DOTALL
)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def run_started_raw() -> str:
    """The raw REVIEW_RUN_STARTED_AT value captured before the agent step —
    persisted verbatim in the report marker as the attempt's chronology key."""
    raw = os.environ.get("REVIEW_RUN_STARTED_AT", "")
    if not raw:
        print("REVIEW_RUN_STARTED_AT must be set", file=sys.stderr)
        sys.exit(1)
    return raw


def run_started_at() -> datetime:
    """Return the run-start timestamp captured before the agent step."""
    return _parse_ts(run_started_raw())


def is_fresh(comment: dict, started: datetime) -> bool:
    """Whether the comment was created/updated at or after the run started —
    i.e. it is this run's output, not a prior run's leftover."""
    raw = comment.get("updated_at") or comment.get("created_at") or ""
    if not raw:
        return False
    return _parse_ts(raw) >= started


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


def live_head_sha(repo: str, pr_number: str) -> str:
    """Re-fetch the PR's current head from the API immediately before acting."""
    pr = _gh.rest("GET", f"repos/{repo}/pulls/{pr_number}")
    return pr["head"]["sha"]


def sha_bound_to_head(reviewed: str | None, head: str) -> bool:
    """Whether a reviewed SHA identifies the current HEAD.

    Prefix-tolerant so a marker may record an abbreviated SHA, but requires at
    least 7 hex chars so it can't degrade to a trivial/placeholder match — an
    empty value, a missing marker, or the literal "CURRENT_SHA" placeholder
    all fail closed.
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
    """Map a summary body to (review event, lead sentence).

    Baseline mode only: request changes on any blocking finding, otherwise
    leave a neutral comment. Never approves. Returns None if the blocking
    count could not be parsed unambiguously from the summary's metadata row.
    """
    blocking = parse_blocking_count(body, heading)
    if blocking is None:
        return None
    if blocking > 0:
        return "REQUEST_CHANGES", "Blocking issues found"
    return "COMMENT", "No blocking issues found"


def publication_identity() -> dict:
    """The host-owned identity of this finalization: workflow + summary
    marker + verdict mode + run + attempt. GITHUB_RUN_ID is per-run, not
    per-job, so the summary marker is part of the identity — two jobs in one
    run reviewing under different markers must not dedupe into each other."""
    return {
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "summary_marker": os.environ.get("SUMMARY_MARKER", ""),
        "verdict_mode": VERDICT_MODE,
    }


def report_state(
    identity: dict,
    head: str,
    base: str | None,
    publication: str = "completed",
    started_at: str | None = None,
) -> dict:
    """CI-owned review-state metadata for a newly published report.

    Keeps the pre-migration keys (last_reviewed_sha/base_sha/workflow_ref) so
    existing state selection reads it unchanged, plus the host-owned
    publication identity. These are host values, never model authority.

    A report is created with publication="pending" and transitioned to
    "completed" by the host ONLY after the required formal review exists: a
    completed report is the report PLUS its formal result, so a report whose
    review never landed must never become the next run's state baseline.

    started_at is the host-captured attempt start (REVIEW_RUN_STARTED_AT) —
    a NON-identity chronology key: publication ordering compares actual
    attempt times, never run-ID order (run creation time ≠ attempt execution
    time across reruns).
    """
    state = {"last_reviewed_sha": head}
    if base:
        state["base_sha"] = base
    for key in ("workflow_ref", "run_id", "run_attempt", "summary_marker", "verdict_mode"):
        if identity.get(key):
            state[key] = identity[key]
    state["publication"] = publication
    if started_at:
        state["started_at"] = started_at
    return state


def summary_comments(comments: list[dict], marker: str) -> list[dict]:
    """Bot-authored summary comments for this reviewer (heading prefix match,
    legacy fallback for built-in headings), newest id last. Superseded
    comments no longer start with the heading, so they drop out here."""
    matching = [
        c
        for c in comments
        if (c.get("user") or {}).get("login") in _review_state.BOT_LOGINS
        and _review_state.matching_heading(c.get("body", ""), marker) is not None
    ]
    matching.sort(key=lambda c: c.get("id", 0))
    return matching


def later_completed_attempt(
    comments: list[dict], started: datetime, workflow_ref: str
) -> dict | None:
    """An owned COMPLETED report whose attempt started LATER than this one —
    evidence this attempt's publication would be stale (a concurrent or
    intentionally-rerun later attempt already finished). Chronology compares
    actual attempt start times, never run-ID order (run creation time ≠
    attempt execution time across reruns).

    Legacy compatibility: a report without started_at never makes an attempt
    obsolete, and a present-but-unparseable started_at is left untouched
    (not treated as evidence either way).
    """
    for c in comments:
        if (
            _review_state.classify_summary_comment(c.get("body", ""), workflow_ref)
            != "completed"
        ):
            continue
        state = _review_state.marker_state(c.get("body", "")) or {}
        raw = state.get("started_at")
        if not raw:
            continue
        try:
            completed_started = _parse_ts(raw)
        except (ValueError, TypeError):
            continue
        if completed_started > started:
            return c
    return None


def consumed_working_ids(comments: list[dict], workflow_ref: str) -> dict:
    """Working comment id -> consumption timestamp recorded by owned reports.

    A working comment named in a report's persisted working_comment_id was
    already turned into a publication. When that report's cleanup collapse
    failed, the comment is still visible — and it stays reusable as the
    model's update slot, so it is only "consumed" for publication while it
    is UNCHANGED (its current timestamp still equals the recorded one).
    Republishing the unchanged comment would fabricate a verdict without
    fresh model work; a comment updated since consumption IS fresh work.
    """
    consumed = {}
    for c in comments:
        if _review_state.classify_summary_comment(
            c.get("body", ""), workflow_ref
        ) in ("completed", "pending"):
            state = _review_state.marker_state(c.get("body", "")) or {}
            working_id = state.get("working_comment_id")
            if working_id:
                consumed[working_id] = state.get("working_comment_updated_at")
    return consumed


def select_working_output(
    comments: list[dict],
    started: datetime,
    workflow_ref: str,
    consumed_ids=frozenset(),
) -> tuple[dict | None, str | None]:
    """Pick this run's final working output from the candidates, newest first.

    Returns (comment, rejection_reason). A rejection_reason is set when
    working candidates exist but none qualifies — the run produced output
    that cannot be treated as final, which must fail loudly. Completed
    reports are skipped: they are publication output, never working input.
    Comments recorded as consumed by a published report (consumed_ids maps
    id -> consumption timestamp) are skipped ONLY while unchanged: a comment
    updated since its recorded consumption carries fresh model work and is
    eligible again.
    """
    saw_stale = False
    saw_consumed = False
    for comment in reversed(comments):
        body = comment.get("body", "")
        if _review_state.classify_summary_comment(body, workflow_ref) != "working":
            continue
        if comment.get("id") in consumed_ids:
            recorded_ts = consumed_ids[comment["id"]]
            current_ts = comment.get("updated_at") or comment.get("created_at")
            if not recorded_ts or current_ts == recorded_ts:
                # Unchanged since a published report consumed it (or the
                # report predates timestamp recording — fail closed).
                saw_consumed = True
                continue
            # Updated after consumption: fresh model work — eligible below.
        if not is_fresh(comment, started):
            saw_stale = True
            continue
        if _review_state.is_provisional(body):
            return None, (
                "the newest working summary from this run is marked provisional "
                "(in-progress); the run is incomplete and no report may be "
                "published"
            )
        return comment, None
    if saw_consumed:
        return None, (
            "the only working summary comments here were already consumed by "
            "a published report; republishing them would fabricate a verdict "
            "without fresh model work"
        )
    if saw_stale:
        return None, (
            "no working summary comment was created or updated during this "
            "run; a successful agent step is not evidence a final summary "
            "was posted"
        )
    return None, None


def find_published_report(
    comments: list[dict], identity: dict, head: str
) -> dict | None:
    """The already-published report for THIS run/attempt, if finalization is
    being replayed — whether still pending (the formal review never landed)
    or completed. Pre-migration completed markers carry no run identity and
    can never match; a report for a different head is a different
    publication."""
    for c in reversed(comments):
        body = c.get("body", "")
        cls = _review_state.classify_summary_comment(body, identity["workflow_ref"])
        if cls not in ("completed", "pending"):
            continue
        state = _review_state.marker_state(body) or {}
        if state.get("publication") not in ("pending", "completed"):
            continue
        if any(state.get(k) != v for k, v in identity.items() if v):
            continue
        if not sha_bound_to_head(state.get("last_reviewed_sha"), head):
            continue
        return c
    return None


def is_pending(report: dict) -> bool:
    """Whether a published report is still awaiting its formal result."""
    state = _review_state.marker_state(report.get("body", "")) or {}
    return state.get("publication") == "pending"


def previous_reports(
    comments: list[dict], new_report: dict, workflow_ref: str
) -> list[dict]:
    """The exact previous owned reports the new report replaces: every owned
    completed report OLDER than the new one, plus any pending leftover from
    an interrupted publication. A replayed finalization must never retire
    NEWER output — a report another run published after this one stays.
    Newest first."""
    new_id = new_report.get("id", 0)
    previous = []
    for c in reversed(comments):
        if c.get("id", 0) >= new_id:
            continue
        if _review_state.classify_summary_comment(
            c.get("body", ""), workflow_ref
        ) in ("completed", "pending"):
            previous.append(c)
    return previous


def recover_consumed_working(comments: list[dict], report: dict) -> dict | None:
    """The exact working comment this report consumed, recovered by the
    identity the report persisted at publication — never by reselecting
    whatever fresh working output happens to exist now.

    Returns None when the report predates persisted working identity, when
    the comment is gone or already superseded, or when its timestamp no
    longer matches: a changed timestamp means a later run reused the slot,
    and collapsing it would destroy that run's output."""
    state = _review_state.marker_state(report.get("body", "")) or {}
    working_id = state.get("working_comment_id")
    if not working_id:
        return None
    expected_ts = state.get("working_comment_updated_at")
    for c in comments:
        if c.get("id") != working_id:
            continue
        current_ts = c.get("updated_at") or c.get("created_at")
        if expected_ts and current_ts != expected_ts:
            return None
        if _review_state.is_superseded(c.get("body", "")):
            return None
        return c
    return None


def compose_report_body(
    working: dict, identity: dict, head: str, base: str | None, started_raw: str
) -> str:
    """The completed report: the run's working output plus a visible
    reviewed-commit link and the CI-owned review-state marker. The marker is
    written as publication="pending": the host transitions it to "completed"
    only after the required formal review exists. It also persists the exact
    consumed working comment's identity (id + timestamp) so a replayed
    finalization collapses exactly that comment — and can never collapse a
    later run's reused slot — and the attempt's started_at so publication
    chronology compares actual attempt times."""
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    commit_url = f"{server_url}/{repo}/commit/{head}"
    state = report_state(identity, head, base, publication="pending", started_at=started_raw)
    state["working_comment_id"] = working.get("id")
    consumed_ts = working.get("updated_at") or working.get("created_at")
    if consumed_ts:
        state["working_comment_updated_at"] = consumed_ts
    stripped = working.get("body", "").rstrip()
    return (
        f"{stripped}\n\n"
        f"---\n"
        f"Reviewed commit: [`{head[:12]}`]({commit_url})\n"
        f"<!-- review-state: {json.dumps(state)} -->\n"
    )


def create_report(
    repo: str, pr_number: str, body: str, identity: dict, head: str
) -> dict:
    """POST the new report comment.

    Creation is non-idempotent, so it runs with max_attempts=1. After an
    ambiguous transient failure (timeout/5xx), reconcile by publication
    identity before concluding anything: the POST may have landed
    server-side. Never blind-retry a creation POST; fail closed when no
    report materialized — all previous output is preserved and a later
    finalization retries safely.
    """
    try:
        return _gh.rest(
            "POST",
            f"repos/{repo}/issues/{pr_number}/comments",
            data={"body": body},
            max_attempts=1,
        )
    except _gh.TransientOutageError:
        print(
            "::warning::Report creation returned an ambiguous transient "
            "error; reconciling by publication identity before concluding "
            "failure.",
            file=sys.stderr,
        )
        comments = summary_comments(
            _gh.rest_paginate(f"repos/{repo}/issues/{pr_number}/comments"),
            identity["summary_marker"],
        )
        found = find_published_report(comments, identity, head)
        if found is not None:
            print(
                f"Report {found['id']} was created despite the ambiguous "
                "error; reusing it."
            )
            return found
        raise


# GitHub's submitted review state for each baseline event. The formal result
# is recognized by EXACT binding — host identity marker + report link +
# reviewed commit + submitted state — never by run identity alone.
REVIEW_STATE_FOR_EVENT = {"REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


def find_verdict_review(
    reviews: list[dict], identity: dict, report: dict, head: str, event: str
) -> tuple[dict | None, dict | None]:
    """Locate THIS run/attempt's formal review for THIS report.

    Returns (matched, conflict). `matched` is the review whose host-owned
    identity marker matches the run identity AND which is bound exactly: the
    marker's report_comment_id is this report, GitHub's commit_id is the
    reviewed head, and the submitted state is the expected one for the
    verdict. `conflict` is an identity-matching review that fails that exact
    binding (wrong report, wrong commit, or an unexpected state such as
    DISMISSED) — an inconsistent existing result, which must fail closed
    rather than be treated as success or be blindly duplicated.
    """
    expected_state = REVIEW_STATE_FOR_EVENT.get(event)
    conflict = None
    for r in reviews:
        if (r.get("user") or {}).get("login") not in _review_state.BOT_LOGINS:
            continue
        m = VERDICT_MARKER_PATTERN.search(r.get("body") or "")
        if not m:
            continue
        try:
            marker = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(marker, dict):
            continue
        if not all(marker.get(k) == v for k, v in identity.items() if v):
            continue
        # Same run/attempt identity: bind it to the exact report, commit,
        # and submitted state before accepting it as the formal result.
        if (
            marker.get("report_comment_id") != report.get("id")
            or r.get("commit_id") != head
            or (expected_state is not None and r.get("state") != expected_state)
        ):
            conflict = r
            continue
        return r, None
    return None, conflict


def submit_verdict(
    repo: str,
    pr_number: str,
    head: str,
    event: str,
    lead: str,
    report: dict,
    identity: dict,
) -> None:
    """Submit the formal PR review via the REST API, bound to the reviewed
    commit and linking directly to the new report.

    `gh pr review` cannot carry a commit argument, so submission goes through
    POST /pulls/{n}/reviews with an explicit commit_id. Same creation
    discipline as the report: max_attempts=1, reconcile by identity after an
    ambiguous failure, fail closed otherwise.
    """
    report_url = report.get("html_url") or ""
    marker = {
        "run_id": identity["run_id"],
        "run_attempt": identity["run_attempt"],
        "workflow_ref": identity["workflow_ref"],
        "summary_marker": identity["summary_marker"],
        "verdict_mode": identity["verdict_mode"],
        "report_comment_id": report.get("id"),
    }
    link = f" — see the [full review report]({report_url})" if report_url else "."
    body = f"{lead}{link}\n\n<!-- review-publication: {json.dumps(marker)} -->"
    print(
        f"Submitting review: POST pulls/{pr_number}/reviews event={event} "
        f"commit={head[:12]}"
    )
    try:
        _gh.rest(
            "POST",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            data={"commit_id": head, "event": event, "body": body},
            max_attempts=1,
        )
    except _gh.TransientOutageError:
        print(
            "::warning::Verdict submission returned an ambiguous transient "
            "error; reconciling by publication identity before concluding "
            "failure.",
            file=sys.stderr,
        )
        reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{pr_number}/reviews")
        matched, conflict = find_verdict_review(reviews, identity, report, head, event)
        if matched is not None:
            print("Verdict review was created despite the ambiguous error; reusing it.")
            return
        if conflict is not None:
            raise _gh.TerminalError(
                "an existing review matches this run/attempt but is bound "
                "inconsistently (different report, commit, or state: review "
                f"id {conflict.get('id')}); refusing to submit another"
            )
        raise
    print("Review submitted.")


def complete_report(
    repo: str, report: dict, identity: dict, head: str
) -> None:
    """Host transition pending -> completed, applied ONLY after the required
    formal review exists. A completed report is the report PLUS its formal
    result; until this transition lands, the report supplies neither review
    state nor a working slot. Idempotent per attempt: a same-attempt retry
    reconciles the report and review by identity and re-runs this PATCH.

    The recovered report's own snapshot metadata (reviewed SHA, base, run
    identity, consumed working identity) is retained verbatim — only the
    publication status flips. Rebuilding state from the current workspace
    could mark the report completed against a base it never reviewed.
    """
    body = report.get("body", "")
    state = _review_state.marker_state(body)
    if state is None:
        print(
            f"Refusing to complete report {report.get('id')}: its review-state "
            "marker no longer parses.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state.get("publication") != "pending":
        print(
            f"Refusing to complete report {report.get('id')}: its publication "
            f"is {state.get('publication')!r}, not 'pending'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if any(state.get(k) != v for k, v in identity.items() if v):
        print(
            f"Refusing to complete report {report.get('id')}: its marker "
            "identity disagrees with this run.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not sha_bound_to_head(state.get("last_reviewed_sha"), head):
        print(
            f"Refusing to complete report {report.get('id')}: its reviewed "
            f"SHA ({state.get('last_reviewed_sha')}) does not match the "
            f"checked-out HEAD ({head}).",
            file=sys.stderr,
        )
        sys.exit(1)
    completed = dict(state)
    completed["publication"] = "completed"
    new_marker = f"<!-- review-state: {json.dumps(completed)} -->"
    # Callable replacement: the JSON (which may contain \uXXXX escapes for
    # non-ASCII summary markers) must be inserted literally, not parsed as a
    # regex replacement template.
    new_body, count = _review_state.REVIEW_STATE_PATTERN.subn(
        lambda _m: new_marker, body, count=1
    )
    if count != 1:
        print(
            f"Refusing to complete report {report.get('id')}: its review-state "
            "marker no longer appears exactly once.",
            file=sys.stderr,
        )
        sys.exit(1)
    _gh.rest(
        "PATCH",
        f"repos/{repo}/issues/comments/{report['id']}",
        data={"body": new_body},
    )
    report["body"] = new_body
    print(f"Report {report['id']} marked completed (formal review published).")


def supersede_comment(
    repo: str, comment: dict, report: dict, head: str, identity: dict
) -> bool:
    """Collapse one consumed comment, retaining its body and linking the new
    report. Returns False when the comment was already superseded (retry-safe
    skip). Never touches human comments or inline threads — callers pass only
    the exact previous owned report and the consumed working comment."""
    body = comment.get("body", "")
    if _review_state.is_superseded(body):
        return False
    report_url = report.get("html_url") or ""
    meta = {
        "report_comment_id": report.get("id"),
        "run_id": identity["run_id"],
        "run_attempt": identity["run_attempt"],
    }
    if report_url:
        link = f'<a href="{report_url}">current review report</a>'
    else:
        link = "current review report"
    new_body = (
        f"<!-- review-superseded: {json.dumps(meta)} -->\n"
        f"<details>\n"
        f"<summary>Superseded — see the {link} for commit "
        f"<code>{head[:12]}</code></summary>\n\n"
        f"{body}\n\n"
        f"</details>\n"
    )
    _gh.rest(
        "PATCH",
        f"repos/{repo}/issues/comments/{comment['id']}",
        data={"body": new_body},
    )
    return True


def _run() -> None:
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
    identity = publication_identity()
    head = current_head_sha()
    base = current_base_sha()

    comments = summary_comments(
        _gh.rest_paginate(f"repos/{repo}/issues/{pr_number}/comments"), marker
    )

    # Idempotent replay: a repeated finalization of the SAME run/attempt
    # reuses its published report instead of creating another.
    report = find_published_report(comments, identity, head)
    if report is not None:
        print(
            f"Finalization replay: reusing published report {report['id']} "
            f"for run {identity['run_id']} attempt {identity['run_attempt']}."
        )
        mapping = verdict_to_review(report.get("body", ""), marker)
        if mapping is None:
            print(
                "Refusing to finalize: the published report's canonical "
                "count row no longer parses unambiguously.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Obsolete-attempt guard, BEFORE any new publication: a concurrent or
        # intentionally-rerun later attempt already completed. Chronology is
        # actual attempt start time, never run-ID order. Existing-report
        # replay (above) stays idempotent and is unaffected.
        obsolete = later_completed_attempt(comments, started, identity["workflow_ref"])
        if obsolete is not None:
            obsolete_started = (_review_state.marker_state(obsolete.get("body", "")) or {}).get(
                "started_at"
            )
            print(
                "Refusing to publish: a later attempt already completed "
                f"report {obsolete['id']} (started {obsolete_started}, after "
                f"this attempt's {run_started_raw()}). This attempt's "
                "publication would be stale.",
                file=sys.stderr,
            )
            sys.exit(1)

        working, rejection = select_working_output(
            comments,
            started,
            identity["workflow_ref"],
            consumed_working_ids(comments, identity["workflow_ref"]),
        )
        if working is None:
            if rejection is None:
                rejection = (
                    f"no bot summary comment matching {marker!r} found — the "
                    "review agent may not have posted its summary"
                )
            print(f"Refusing to publish a review report: {rejection}.", file=sys.stderr)
            sys.exit(1)

        body = working.get("body", "")
        mapping = verdict_to_review(body, marker)
        if mapping is None:
            print(
                "Could not parse an unambiguous blocking-issue count from the "
                "working summary (need exactly one canonical count row — "
                "'**Blocking Issues: N** | **Suggestions: M** | **Threads Resolved: R**' — "
                "as the first non-empty line after the summary heading; fenced "
                "code blocks are ignored).",
                file=sys.stderr,
            )
            sys.exit(1)

        # Bind publication to the LIVE PR head: a push during the run stops
        # it. The prompt's head guard covers the agent's own posts; this
        # covers the CI publication the agent no longer performs.
        live = live_head_sha(repo, pr_number)
        if live != head:
            print(
                "Refusing to publish: the PR head changed during the run "
                f"(reviewed {head}, live {live}). The verdict belongs to a "
                "commit that is no longer current.",
                file=sys.stderr,
            )
            sys.exit(1)

        report = create_report(
            repo, pr_number,
            compose_report_body(working, identity, head, base, run_started_raw()),
            identity, head,
        )
        print(f"Published review report {report['id']} -> {head[:12]} (pending completion)")

    event, lead = mapping

    reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{pr_number}/reviews")
    matched_review, conflict_review = find_verdict_review(
        reviews, identity, report, head, event
    )
    if conflict_review is not None:
        print(
            "Refusing to finalize: an existing review matches this "
            f"run/attempt but is bound inconsistently (different report, "
            f"commit, or state: review id {conflict_review.get('id')}). "
            "Failing closed rather than duplicating or adopting it.",
            file=sys.stderr,
        )
        sys.exit(1)
    if matched_review is not None:
        print(
            "Formal verdict review already submitted for this run/attempt; "
            "not creating another."
        )
    elif not is_pending(report):
        # The report is completed, so its formal review succeeded at
        # publication time. A missing review now means it was deleted or
        # dismissed afterwards — never recreate a historical verdict.
        print(
            f"Refusing to finalize: report {report['id']} is completed but "
            "its formal review no longer exists (deleted or dismissed after "
            "publication). Not recreating a historical verdict.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        # Recheck the live head immediately before the formal vote.
        live = live_head_sha(repo, pr_number)
        if live != head:
            print(
                "Refusing to submit the verdict: the PR head changed during "
                f"the run (reviewed {head}, live {live}). The verdict belongs "
                "to a commit that is no longer current.",
                file=sys.stderr,
            )
            sys.exit(1)
        submit_verdict(repo, pr_number, head, event, lead, report, identity)

    # Host transition: the report becomes completed state ONLY after the
    # required formal review exists. A report whose review never landed stays
    # pending — preserved for same-attempt retry, but never the next run's
    # state baseline and never a model-mutable slot.
    if is_pending(report):
        complete_report(repo, report, identity, head)

    # Cleanup ONLY after the report is completed and the formal review
    # exists: collapse the exact previous owned reports OLDER than the new
    # report (including any pending leftover from an interrupted publication)
    # and the exact consumed working comment, recovered by the identity the
    # report persisted — never a newer report, never a later run's reused
    # slot. Failure here warns but never destroys the published output.
    targets = previous_reports(comments, report, identity["workflow_ref"])
    consumed = recover_consumed_working(comments, report)
    if consumed is not None:
        targets.append(consumed)
    for target in targets:
        try:
            if supersede_comment(repo, target, report, head, identity):
                print(f"Superseded comment {target['id']} -> report {report['id']}")
        except _gh.GitHubError as e:
            print(
                f"::warning::Could not supersede comment {target['id']}: {e}. "
                "The published report and verdict are unaffected; a later "
                "finalization retries cleanup.",
                file=sys.stderr,
            )


def main() -> None:
    try:
        _run()
    except _gh.TransientOutageError as e:
        # GitHub was down while reading state or publishing. Fail closed: a
        # verdict is never faked, previous output is preserved, and the check
        # stays red so the review re-runs.
        print(f"GitHub outage while publishing review report: {e}", file=sys.stderr)
        sys.exit(1)
    except _gh.TerminalError as e:
        print(f"Failed to publish review report: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
