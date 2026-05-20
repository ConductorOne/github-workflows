#!/usr/bin/env python3
"""Post and finalize the PR review summary after the model runs.

The model returns the human-readable summary as structured output. This script
owns posting the summary, writing the hidden review-state marker, and stale
summary cleanup so PR comments cannot poison the next run's state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

REVIEW_STATE_PATTERN = re.compile(
    r"\n?<!--\s*review-state:\s*(\{.*?\})\s*-->\s*",
    re.DOTALL,
)
BOT_LOGINS = {"github-actions[bot]", "github-actions"}
DEFAULT_REVIEW_SUMMARY_HEADING = "### Connector PR Review:"
LEGACY_REVIEW_SUMMARY_HEADING = "### PR Review:"


def gh_json(*args: str) -> dict | list[dict]:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout or "{}")


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def gh_api_paginate(endpoint: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate"],
        capture_output=True,
        text=True,
        check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            entries.extend(parsed)
        else:
            entries.append(parsed)
    if not entries:
        try:
            parsed = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    return entries


def summary_heading(context: dict) -> str:
    return context.get("summary_heading") or DEFAULT_REVIEW_SUMMARY_HEADING


def review_comment_heading(comment: dict, context: dict) -> Optional[str]:
    body = (comment.get("body") or "").lstrip()
    for heading in (summary_heading(context), LEGACY_REVIEW_SUMMARY_HEADING):
        if body.startswith(heading):
            return heading
    return None


def is_bot_review_comment(comment: dict, context: dict) -> bool:
    user = (comment.get("user") or {}).get("login", "unknown")
    return user in BOT_LOGINS and review_comment_heading(comment, context) is not None


def parse_time(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def comment_time(comment: dict) -> datetime:
    return max(
        parse_time(comment.get("updated_at", "")),
        parse_time(comment.get("created_at", "")),
    )


def comment_id(comment: dict) -> int:
    try:
        return int(comment.get("id", 0))
    except (TypeError, ValueError):
        return 0


def comment_order_key(comment: dict) -> tuple[datetime, int]:
    return (comment_time(comment), comment_id(comment))


def parse_review_state(body: str) -> Optional[dict]:
    match = REVIEW_STATE_PATTERN.search(body or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def expected_state(context: dict) -> dict:
    return {
        "last_reviewed_sha": context["current_sha"],
        "base_sha": context["current_base_sha"],
        "workflow_ref": context["workflow_ref"],
        "run_id": context["run_id"],
    }


def marker_for(context: dict) -> str:
    state = expected_state(context)
    return f"<!-- review-state: {json.dumps(state, sort_keys=True)} -->"


def has_expected_state(comment: dict, context: dict) -> bool:
    return parse_review_state(comment.get("body", "")) == expected_state(context)


def strip_review_state(body: str) -> str:
    return REVIEW_STATE_PATTERN.sub("\n", body or "").rstrip()


def current_run_candidate(comment: dict, context: dict) -> bool:
    if not is_bot_review_comment(comment, context):
        return False
    body = comment.get("body") or ""
    if has_expected_state(comment, context):
        return True
    review_run_url = context.get("review_run_url")
    return bool(review_run_url and review_run_url in body)


def summary_body_from_structured_output(raw_output: str, context: dict) -> str:
    if not raw_output:
        raise ValueError("CLAUDE_STRUCTURED_OUTPUT is empty")
    try:
        output = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ValueError("CLAUDE_STRUCTURED_OUTPUT is not valid JSON") from exc
    summary_body = output.get("summary_body")
    if not isinstance(summary_body, str) or not summary_body.strip():
        raise ValueError("summary_body is required in Claude structured output")
    summary_body = strip_review_state(summary_body)
    if not summary_body.lstrip().startswith(summary_heading(context)):
        raise ValueError("summary_body must start with the configured review heading")
    return summary_body


def ensure_review_run_link(body: str, context: dict) -> str:
    review_run_url = context.get("review_run_url")
    if not review_run_url or review_run_url in body:
        return body

    lines = body.splitlines()
    insert_at = min(len(lines), 1)
    for index, line in enumerate(lines):
        if line.startswith("_Review mode:"):
            insert_at = index + 1
            break
    lines.insert(insert_at, f"[View review run]({review_run_url})")
    return "\n".join(lines)


def post_comment(repository: str, pr_number: int, body: str) -> dict:
    return gh_json(
        f"repos/{repository}/issues/{pr_number}/comments",
        "-X",
        "POST",
        "-f",
        f"body={body}",
    )


def delete_comment(repository: str, comment_id: int) -> None:
    result = gh(
        f"repos/{repository}/issues/comments/{comment_id}",
        "-X",
        "DELETE",
        check=False,
    )
    if result.returncode != 0:
        print(f"Failed to delete stale summary {comment_id}: {result.stderr}", file=sys.stderr)


def owned_stale_summary(comment: dict, context: dict) -> bool:
    if not is_bot_review_comment(comment, context):
        return False
    state = parse_review_state(comment.get("body", ""))
    if state is None:
        return False
    return state.get("workflow_ref") == context.get("workflow_ref")


def main() -> None:
    context_path = os.path.join(".github", "pr-context.json")
    with open(context_path, encoding="utf-8") as f:
        context = json.load(f)

    repository = context["repository"]
    pr_number = context["pr_number"]
    run_id = context.get("run_id")
    if not run_id:
        print("run_id is required to finalize review summary", file=sys.stderr)
        sys.exit(1)

    pr = gh_json(f"repos/{repository}/pulls/{pr_number}")
    if pr["head"]["sha"] != context["current_sha"]:
        print("PR head changed after review; skipping summary finalization and cleanup")
        return

    try:
        summary_body = summary_body_from_structured_output(
            os.environ.get("CLAUDE_STRUCTURED_OUTPUT", ""),
            context,
        )
    except ValueError as exc:
        print(f"Could not read Claude review summary: {exc}", file=sys.stderr)
        sys.exit(1)

    summary_body = ensure_review_run_link(summary_body, context)
    finalized_body = f"{summary_body}\n\n{marker_for(context)}\n"
    current_summary = post_comment(repository, pr_number, finalized_body)
    current_id = current_summary["id"]

    comments = gh_api_paginate(f"repos/{repository}/issues/{pr_number}/comments")
    finalized = next((c for c in comments if c["id"] == current_id), None)
    if not finalized or not has_expected_state(finalized, context):
        print("Current summary does not contain the expected state marker", file=sys.stderr)
        sys.exit(1)

    finalized_key = comment_order_key(finalized)
    newer_summaries = [
        c for c in comments
        if c["id"] != current_id
        and owned_stale_summary(c, context)
        and comment_order_key(c) > finalized_key
    ]
    if newer_summaries:
        print("A newer review summary exists; skipping stale summary cleanup")
        return

    for comment in comments:
        if comment["id"] == current_id:
            continue
        if current_run_candidate(comment, context):
            delete_comment(repository, comment["id"])
            continue
        if owned_stale_summary(comment, context):
            delete_comment(repository, comment["id"])

    print(f"Finalized review summary {current_id}")


if __name__ == "__main__":
    main()
