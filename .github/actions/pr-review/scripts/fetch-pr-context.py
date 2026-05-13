#!/usr/bin/env python3
"""Fetch PR comments and extract review state for the review prompt.

Fetches all issue comments via gh api, then extracts:
- last_reviewed_sha: the SHA from the <!-- review-state: ... --> marker
- review_mode: "incremental" when a GitHub API compare diff is available, otherwise "full"
- All comments (for dedup of existing findings)

Writes structured JSON to .github/pr-context.json.
"""

import json
import os
import re
import subprocess
import sys
from typing import Optional

REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)

# Bot logins that post review comments via GitHub Actions.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}


def is_bot_review_comment(comment: dict) -> bool:
    """Check if a comment is a bot-posted review summary."""
    return (
        comment["user"] in BOT_LOGINS
        and comment["body"].lstrip().startswith("### Connector PR Review:")
    )


def gh_api_paginate(endpoint: str) -> list[dict]:
    """Fetch all pages from a gh api endpoint."""
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate"],
        capture_output=True,
        text=True,
        check=True,
    )
    # --paginate concatenates JSON arrays; each page is a JSON array
    # Parse by finding all top-level arrays
    entries = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)
        except json.JSONDecodeError:
            pass
    # If the whole output is a single JSON array, handle that too
    if not entries:
        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
    return entries


def fetch_compare_diff(head_repo: str, base_sha: str, head_sha: str) -> Optional[str]:
    """Fetch a compare diff from the PR head repo without checking out PR code."""
    endpoint = f"repos/{head_repo}/compare/{base_sha}...{head_sha}"
    try:
        metadata = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            check=True,
        )
        compare = json.loads(metadata.stdout)
        status = compare.get("status", "")
        if status != "ahead":
            print(
                f"Compare status is {status!r}, using full review mode",
                file=sys.stderr,
            )
            return None
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.diff", endpoint],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(
            f"Could not fetch incremental diff from {head_repo}: {e.stderr}",
            file=sys.stderr,
        )
        return None
    if not result.stdout.strip():
        return None
    return result.stdout


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    if not repo or not pr_number:
        print("GITHUB_REPOSITORY and PR_NUMBER must be set", file=sys.stderr)
        sys.exit(1)

    endpoint = f"repos/{repo}/issues/{pr_number}/comments"
    print(f"Fetching comments from {endpoint}...")
    raw_comments = gh_api_paginate(endpoint)
    print(f"Found {len(raw_comments)} comments")

    # Extract comment summaries
    comments = []
    for c in raw_comments:
        comments.append({
            "id": c["id"],
            "user": c.get("user", {}).get("login", "unknown"),
            "body": c.get("body", ""),
        })

    # Only bot-authored review comments are authoritative state. User-authored
    # markers are untrusted PR content and must not influence review mode.
    review_comments = [c for c in comments if is_bot_review_comment(c)]

    # Extract last_reviewed_sha from the newest valid bot review state.
    last_reviewed_sha = None
    last_review_base_sha = None
    summary_comment_id = review_comments[-1]["id"] if review_comments else None
    for c in reversed(review_comments):
        match = REVIEW_STATE_PATTERN.search(c["body"])
        if match:
            try:
                state = json.loads(match.group(1))
                if workflow_ref and state.get("workflow_ref") != workflow_ref:
                    continue
                last_reviewed_sha = state.get("last_reviewed_sha")
                last_review_base_sha = state.get("base_sha")
                if last_reviewed_sha:
                    break
            except json.JSONDecodeError:
                pass

    pr_endpoint = f"repos/{repo}/pulls/{pr_number}"
    pr_result = subprocess.run(
        ["gh", "api", pr_endpoint],
        capture_output=True,
        text=True,
        check=True,
    )
    pr = json.loads(pr_result.stdout)
    current_sha = pr["head"]["sha"]
    current_base_sha = pr["base"]["sha"]
    head_repo = pr["head"]["repo"]["full_name"]
    print(f"Current PR head: {current_sha[:12]}")
    print(f"Current PR base: {current_base_sha[:12]}")

    # This action intentionally does not check out PR head code under
    # pull_request_target. Use GitHub-provided diffs instead of relying on
    # local git history from untrusted code.
    review_mode = "full"
    incremental_diff_path = None
    if not last_reviewed_sha:
        print("No previous review state found, using full review mode")
    elif last_review_base_sha != current_base_sha:
        print("PR base changed since last review, using full review mode")
        last_reviewed_sha = None
    else:
        incremental_diff = fetch_compare_diff(head_repo, last_reviewed_sha, current_sha)
        if incremental_diff:
            incremental_diff_path = os.path.join(".github", "incremental.diff")
            os.makedirs(os.path.dirname(incremental_diff_path), exist_ok=True)
            with open(incremental_diff_path, "w") as f:
                f.write(incremental_diff)
            review_mode = "incremental"
            print(f"Incremental diff written to {incremental_diff_path}")
        else:
            print("No incremental diff available, using full review mode")
            last_reviewed_sha = None

    # Collect existing findings from bot review comments to help with dedup.
    # Human comments remain available as context, but they are not authoritative
    # review state and cannot suppress findings by mimicking the summary format.
    existing_findings = []
    for c in review_comments:
        body = c["body"]
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("- ") and "`" in line:
                existing_findings.append(line)

    context = {
        "repository": repo,
        "pr_number": pr_number,
        "current_sha": current_sha,
        "current_base_sha": current_base_sha,
        "workflow_ref": workflow_ref,
        "review_mode": review_mode,
        "last_reviewed_sha": last_reviewed_sha,
        "last_review_base_sha": last_review_base_sha,
        "summary_comment_id": summary_comment_id,
        "incremental_diff_path": incremental_diff_path,
        "existing_findings": existing_findings,
        "comments": comments,
    }

    output_path = os.path.join(".github", "pr-context.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(context, f, indent=2)

    print(f"Context written to {output_path}")


if __name__ == "__main__":
    main()
