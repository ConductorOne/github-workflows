#!/usr/bin/env python3
"""Create or update the workflow-owned review summary after a stale-head check."""

import json
import os
import re
import subprocess
import sys

REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)


def load_context() -> dict:
    with open(os.path.join(".github", "pr-context.json")) as f:
        return json.load(f)


def verify_head(context: dict) -> None:
    endpoint = f"repos/{context['repository']}/pulls/{context['pr_number']}"
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        check=True,
    )
    pr = json.loads(result.stdout)
    actual_sha = pr["head"]["sha"]
    expected_sha = context["current_sha"]
    if actual_sha != expected_sha:
        print(
            f"PR head changed from {expected_sha} to {actual_sha}; not posting",
            file=sys.stderr,
        )
        sys.exit(3)


def verify_summary(context: dict, body: str) -> None:
    heading = context["summary_heading"]
    if not body.lstrip().startswith(heading):
        print(f"summary must start with {heading!r}", file=sys.stderr)
        sys.exit(2)

    match = REVIEW_STATE_PATTERN.search(body)
    if not match:
        print("summary is missing review-state marker", file=sys.stderr)
        sys.exit(2)

    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        print("review-state marker is not valid JSON", file=sys.stderr)
        sys.exit(2)
    expected = {
        "last_reviewed_sha": context["current_sha"],
        "base_sha": context["current_base_sha"],
        "workflow_ref": context["workflow_ref"],
    }
    if state != expected:
        print("review-state marker does not match current context", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    body = sys.stdin.read()
    if not body.strip():
        print("summary body is required on stdin", file=sys.stderr)
        sys.exit(2)

    context = load_context()
    verify_head(context)
    verify_summary(context, body)

    comment_id = context.get("summary_comment_id")
    if comment_id:
        cmd = [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{context['repository']}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        ]
    else:
        cmd = [
            "gh",
            "api",
            f"repos/{context['repository']}/issues/{context['pr_number']}/comments",
            "-f",
            f"body={body}",
        ]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
