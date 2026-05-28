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
import time
from typing import Optional

REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
HTTP_STATUS_PATTERN = re.compile(r"HTTP\s+(\d{3})")

# Bot logins that post review comments via GitHub Actions.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}
DEFAULT_REVIEW_SUMMARY_HEADING = "### Connector PR Review:"
LEGACY_REVIEW_SUMMARY_HEADING = "### PR Review:"
DEFAULT_API_ATTEMPTS = 3


def review_comment_heading(comment: dict, summary_heading: str) -> Optional[str]:
    body = comment["body"].lstrip()
    for heading in (summary_heading, LEGACY_REVIEW_SUMMARY_HEADING):
        if body.startswith(heading):
            return heading
    return None


def is_bot_review_comment(comment: dict, summary_heading: str) -> bool:
    """Check if a comment is a bot-posted review summary."""
    return (
        comment["user"] in BOT_LOGINS
        and review_comment_heading(comment, summary_heading) is not None
    )


def is_legacy_review_comment(comment: dict, summary_heading: str) -> bool:
    """Check if a comment is a bot-posted pre-migration review summary."""
    return review_comment_heading(comment, summary_heading) == LEGACY_REVIEW_SUMMARY_HEADING


def command_error_summary(error: subprocess.CalledProcessError) -> str:
    detail = (error.stderr or error.stdout or "").strip()
    if not detail:
        detail = f"exit status {error.returncode}"
    return detail.splitlines()[-1]


def error_http_status(error: subprocess.CalledProcessError) -> Optional[int]:
    match = HTTP_STATUS_PATTERN.search(error.stderr or error.stdout or "")
    if not match:
        return None
    return int(match.group(1))


def retry_limit_for_error(
    error: subprocess.CalledProcessError,
    attempts: int,
) -> int:
    status = error_http_status(error)
    if status == 404:
        return min(attempts, 2)
    if status in (408, 429) or (status is not None and status >= 500):
        return attempts
    if status is None:
        return attempts
    return 1


def gh_api(args: list[str], *, attempts: int = DEFAULT_API_ATTEMPTS) -> subprocess.CompletedProcess:
    """Run gh api with a small retry window for transient GitHub API failures."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                ["gh", "api", *args],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            last_error = e
            retry_limit = retry_limit_for_error(e, attempts)
            e.retry_limit = retry_limit
            if attempt >= retry_limit:
                break
            print(
                "::warning::GitHub API request failed; "
                f"retrying ({attempt}/{retry_limit}): gh api {' '.join(args)}: "
                f"{command_error_summary(e)}",
                file=sys.stderr,
            )
            time.sleep(attempt)
    raise last_error


def gh_api_paginate(endpoint: str) -> list[dict]:
    """Fetch all pages from a gh api endpoint."""
    result = gh_api(
        [endpoint, "--paginate"],
    )
    return parse_paginated_json(result.stdout)


def parse_paginated_json(output: str) -> list[dict]:
    """Parse gh api --paginate output."""
    # --paginate concatenates JSON arrays; each page is a JSON array
    # Parse by finding all top-level arrays
    entries = []
    for line in output.strip().splitlines():
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
            entries = json.loads(output)
        except json.JSONDecodeError:
            pass
    return entries


def fetch_compare_diff(head_repo: str, base_sha: str, head_sha: str) -> Optional[str]:
    """Fetch a compare diff from the PR head repo without checking out PR code."""
    endpoint = f"repos/{head_repo}/compare/{base_sha}...{head_sha}"
    try:
        metadata = gh_api([endpoint])
        compare = json.loads(metadata.stdout)
        status = compare.get("status", "")
        if status != "ahead":
            print(
                f"Compare status is {status!r}, using full review mode",
                file=sys.stderr,
            )
            return None
        result = gh_api(["-H", "Accept: application/vnd.github.diff", endpoint])
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
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    review_run_url = f"{server_url}/{repo}/actions/runs/{run_id}" if repo and run_id else None
    summary_heading = os.environ.get(
        "REVIEW_SUMMARY_HEADING",
        DEFAULT_REVIEW_SUMMARY_HEADING,
    ).strip()
    if not repo or not pr_number:
        print("GITHUB_REPOSITORY and PR_NUMBER must be set", file=sys.stderr)
        sys.exit(1)
    if not summary_heading.startswith("### ") or not summary_heading.endswith(":"):
        print("REVIEW_SUMMARY_HEADING must look like a markdown heading", file=sys.stderr)
        sys.exit(1)

    endpoint = f"repos/{repo}/issues/{pr_number}/comments"
    print(f"Fetching comments from {endpoint}...")
    try:
        raw_comments = gh_api_paginate(endpoint)
    except subprocess.CalledProcessError as e:
        retry_limit = getattr(e, "retry_limit", DEFAULT_API_ATTEMPTS)
        print(
            "::error::Could not fetch prior PR comments after "
            f"{retry_limit} attempt(s). Endpoint: {endpoint}. "
            f"Repository: {repo}. PR: {pr_number}. Last error: "
            f"{command_error_summary(e)}",
            file=sys.stderr,
        )
        raise
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
    review_comments = [c for c in comments if is_bot_review_comment(c, summary_heading)]

    # Extract state from the newest bot review comment owned by this workflow.
    # If only legacy markerless comments exist, reuse the newest one so the first
    # marker-writing run does not create a duplicate summary.
    last_reviewed_sha = None
    last_review_base_sha = None
    summary_comment_id = None
    legacy_summary_comment_id = None
    for c in reversed(review_comments):
        match = REVIEW_STATE_PATTERN.search(c["body"])
        if not match:
            if legacy_summary_comment_id is None:
                legacy_summary_comment_id = c["id"]
            continue

        try:
            state = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        if workflow_ref and state.get("workflow_ref") != workflow_ref:
            if is_legacy_review_comment(c, summary_heading) and legacy_summary_comment_id is None:
                legacy_summary_comment_id = c["id"]
            continue

        summary_comment_id = c["id"]
        last_reviewed_sha = state.get("last_reviewed_sha")
        last_review_base_sha = state.get("base_sha")
        break

    if summary_comment_id is None:
        summary_comment_id = legacy_summary_comment_id

    pr_endpoint = f"repos/{repo}/pulls/{pr_number}"
    pr_result = gh_api([pr_endpoint])
    pr = json.loads(pr_result.stdout)
    current_sha = pr["head"]["sha"]
    current_base_sha = pr["base"]["sha"]
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
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
    elif not head_repo:
        print("PR head repository is unavailable, using full review mode")
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
        "review_run_url": review_run_url,
        "summary_heading": summary_heading,
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
