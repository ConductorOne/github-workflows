#!/usr/bin/env python3
"""Shared review-comment markers, classification, and selection.

The PR-review action separates two kinds of bot summary comments:

- WORKING comments: the model's in-progress output — provisional or
  markerless. fetch-pr-context.py hands the newest working comment to the
  model as its update slot, and publish-review-report.py reads this run's
  final verdict out of the freshest working comment.
- COMPLETED reports: comments a trusted CI publication step finalized with
  CI-owned `<!-- review-state: ... -->` metadata after the required formal
  review succeeded (publication="completed"; pre-migration markers without
  the key still count). A completed report supplies review state for
  incremental mode but is NEVER a working slot — the model must not mutate a
  completed report. A report whose formal review has not succeeded carries
  publication="pending": it is preserved for same-attempt retry but supplies
  neither state nor a slot.

Both consumers must agree on what counts as foreign, malformed, superseded,
provisional, or completed, so the patterns and the classifier live here
exactly once.
"""

import json
import re
from typing import Optional

# Bot logins that post review comments via GitHub Actions. Only GitHub itself
# can author comments under these logins (the "[bot]" suffix is reserved for
# apps), so a PR author cannot spoof a summary comment directly.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}

DEFAULT_REVIEW_SUMMARY_HEADING = "### Connector PR Review:"
GENERAL_REVIEW_SUMMARY_HEADING = "### General PR Review:"
LEGACY_REVIEW_SUMMARY_HEADING = "### PR Review:"
# Headings from this workflow's own review lineage. Only these may also match
# pre-migration (legacy-heading) summaries; a caller-supplied custom heading
# selects exactly its own comments, so a one-off review run can never adopt
# or rewrite the production or legacy summary threads.
BUILT_IN_REVIEW_SUMMARY_HEADINGS = (
    DEFAULT_REVIEW_SUMMARY_HEADING,
    GENERAL_REVIEW_SUMMARY_HEADING,
)

# The sticky summary embeds CI-owned review state in an HTML comment. The
# metadata is host-written: the model never emits it.
REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
REVIEW_STATE_MARKER_PATTERN = re.compile(r"<!--\s*review-state\b")

# A comment collapsed by a successful publication starts with this marker;
# its original body is retained inside a <details> section. Superseded
# comments are archived output: never candidates for slot or state.
SUPERSEDED_PREFIX_PATTERN = re.compile(r"^\s*<!--\s*review-superseded\b")

# Line the review prompt requires on provisional (in-progress) summaries. A
# provisional comment is progress output, not a completed review: it must
# never supply review state, or a killed/lazy run would advance
# last_reviewed_sha without completing the audit behind it.
PROVISIONAL_MARKER = "_⏳ Provisional — deeper review still in progress._"


def is_valid_summary_heading(value: str) -> bool:
    """Whether a summary heading is one non-empty single-line Markdown heading
    of the form '### <text>:'. Newlines are rejected so a crafted heading can
    never smuggle extra lines wherever it is written, and empty/whitespace
    heading text is rejected so the heading always identifies a real summary.
    """
    if not value or "\n" in value or "\r" in value:
        return False
    if not value.startswith("### ") or not value.endswith(":"):
        return False
    # Substring-based legacy consumers must not mistake a custom summary for
    # their own. Exact built-in headings remain valid for existing callers.
    if value != LEGACY_REVIEW_SUMMARY_HEADING and LEGACY_REVIEW_SUMMARY_HEADING in value:
        return False
    for reserved in BUILT_IN_REVIEW_SUMMARY_HEADINGS:
        if value != reserved and reserved in value:
            return False
    return bool(value[len("### "):-1].strip())


def matching_heading(body: str, summary_heading: str) -> Optional[str]:
    """The summary heading this comment body starts with, or None.

    Pre-migration legacy headings count only when the selected heading is one
    of this workflow's built-in production headings. Matching is anchored to
    the start of the body: a comment that merely QUOTES the heading (e.g. a
    Claude tracking comment echoing the summary) is not a summary comment.
    """
    body = body.lstrip()
    headings = (summary_heading,)
    if summary_heading in BUILT_IN_REVIEW_SUMMARY_HEADINGS:
        headings += (LEGACY_REVIEW_SUMMARY_HEADING,)
    for heading in headings:
        if body.startswith(heading):
            return heading
    return None


def is_provisional(body: str) -> bool:
    """Whether a summary comment is provisional (in-progress) output."""
    return PROVISIONAL_MARKER in body


def is_superseded(body: str) -> bool:
    """Whether a comment was collapsed by a successful publication."""
    return bool(SUPERSEDED_PREFIX_PATTERN.search(body))


def marker_state(body: str) -> Optional[dict]:
    """Extract the review-state marker JSON from a body, if present and valid."""
    m = REVIEW_STATE_PATTERN.search(body)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def classify_summary_comment(body: str, workflow_ref: str) -> str:
    """Classify a bot summary comment body for slot/state selection.

    - "superseded": collapsed by a successful publication — archived output,
      never a candidate for slot or state.
    - "foreign": carries an explicit marker owned by another workflow, or a
      marker that fails to parse (unparseable, not a JSON object, or
      unterminated) — fail closed: neither slot nor state.
    - "pending": a published report whose required formal review has not
      succeeded yet (publication is present but not "completed"). A completed
      report is the report PLUS its formal result, so a pending report
      supplies neither review state nor a working slot.
    - "completed": owned, non-provisional review-state marker whose
      publication is "completed" or absent (pre-migration markers still
      represent completed reports). Supplies review state; NEVER a working
      slot.
    - "working": markerless, or provisional with an owned marker — eligible
      working slot; carries no completed state.
    """
    if is_superseded(body):
        return "superseded"
    if not REVIEW_STATE_PATTERN.search(body):
        if REVIEW_STATE_MARKER_PATTERN.search(body):
            # An invalid or incomplete marker is not a markerless summary.
            return "foreign"
        # Markerless summary: reusable as the working slot under the
        # heading/bot trust fallback, but it carries no review state.
        return "working"
    state = marker_state(body)
    if state is None:
        # Malformed marker (unparseable or not a JSON object): fail closed.
        return "foreign"
    if workflow_ref and state.get("workflow_ref") != workflow_ref:
        # Explicit foreign workflow marker: never adopt its summary thread or
        # its state, even under a legacy heading.
        return "foreign"
    if is_provisional(body):
        # Provisional owned marker: a valid working slot, but completed state
        # must come from an older finished review.
        return "working"
    publication = state.get("publication")
    if publication is not None and publication != "completed":
        # Published but not completed (e.g. "pending": the formal review has
        # not succeeded). Never state, never a working slot.
        return "pending"
    return "completed"


def select_working_slot_and_state(
    review_comments: list[dict], workflow_ref: str
) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Choose the working comment to update and the completed state supplier.

    `review_comments` must be newest-last and already filtered to bot-authored
    comments matching the selected heading. Returns (working, state_comment,
    state), independently selected:

    - working is the newest WORKING comment (provisional or markerless) — an
      earlier in-progress run's unfinished output, which a retried run
      updates instead of posting a duplicate. Completed reports are never
      returned here: the model must not mutate them.
    - state_comment/state come from the newest COMPLETED report (owned,
      non-provisional marker). A provisional marker never supplies state.

    Either side may be None. When no completed state exists the caller falls
    back to full review mode but still updates the same working comment.
    """
    working = None
    state_comment = None
    state = None
    for c in reversed(review_comments):
        cls = classify_summary_comment(c["body"], workflow_ref)
        if cls == "working" and working is None:
            working = c
        elif cls == "completed" and state_comment is None:
            state_comment = c
            state = marker_state(c["body"])
        if working is not None and state_comment is not None:
            break
    return working, state_comment, state
