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
from dataclasses import dataclass
from typing import Optional

REVIEW_STATE_PATTERN = re.compile(
    r"<!--\s*review-state:\s*(\{.*?\})\s*-->", re.DOTALL
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_DIFF_BYTES = 5 * 1024 * 1024

# Bot logins that post review comments via GitHub Actions.
BOT_LOGINS = {"github-actions[bot]", "github-actions"}
DEFAULT_REVIEW_SUMMARY_HEADING = "### Connector PR Review:"
LEGACY_REVIEW_SUMMARY_HEADING = "### PR Review:"


@dataclass
class CompareResult:
    status: str
    diff: Optional[str]
    reason: Optional[str] = None


def mode_reason(code: str, message: str, **details: str) -> dict:
    return {
        "code": code,
        "message": message,
        "details": {k: v for k, v in details.items() if v is not None},
    }


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


def comment_sort_key(comment: dict) -> tuple[str, str]:
    """Sort comments by explicit timestamps, falling back to ID order."""
    return (
        comment.get("updated_at") or comment.get("created_at") or "",
        str(comment.get("id", "")),
    )


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA_PATTERN.match(value) is not None


def parse_review_state(body: str) -> Optional[dict]:
    match = REVIEW_STATE_PATTERN.search(body)
    if not match:
        return None

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def valid_owned_state_fields(state: dict, workflow_ref: str) -> bool:
    if workflow_ref and state.get("workflow_ref") != workflow_ref:
        return False
    if not valid_sha(state.get("last_reviewed_sha")):
        return False
    if not valid_sha(state.get("base_sha")):
        return False
    return True


def parse_owned_review_state(body: str, workflow_ref: str) -> Optional[dict]:
    state = parse_review_state(body)
    if state is None or not valid_owned_state_fields(state, workflow_ref):
        return None
    run_id = state.get("run_id")
    if run_id is None or not str(run_id).isdigit():
        return None
    return state


def parse_legacy_owned_review_state(body: str, workflow_ref: str) -> Optional[dict]:
    state = parse_review_state(body)
    if state is None or not valid_owned_state_fields(state, workflow_ref):
        return None
    if state.get("run_id") is not None:
        return None
    return state


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


def fetch_compare_diff(compare_repo: str, base_sha: str, head_sha: str) -> CompareResult:
    """Fetch a compare diff without checking out PR code."""
    endpoint = f"repos/{compare_repo}/compare/{base_sha}...{head_sha}"
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
                f"Compare {base_sha[:12]}...{head_sha[:12]} status is {status!r}",
                file=sys.stderr,
            )
            return CompareResult(status=status, diff=None, reason="compare_not_ahead")
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.diff", endpoint],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(
            f"Could not fetch compare diff from {compare_repo}: {e.stderr}",
            file=sys.stderr,
        )
        return CompareResult(status="unavailable", diff=None, reason="compare_unavailable")
    if not result.stdout.strip():
        return CompareResult(status="ahead", diff=None, reason="empty_diff")
    if len(result.stdout.encode("utf-8")) > MAX_DIFF_BYTES:
        return CompareResult(status="ahead", diff=None, reason="diff_too_large")
    return CompareResult(status="ahead", diff=result.stdout)


def normalize_diff(diff: str) -> list[str]:
    keep_index_lines = has_binary_diff(diff)
    normalized = []
    for line in diff.splitlines():
        if line.startswith("index ") and not keep_index_lines:
            continue
        if line.startswith("@@ "):
            normalized.append(re.sub(r"@@ -[0-9,]+ \+[0-9,]+ @@", "@@ @@", line))
            continue
        normalized.append(line)
    return normalized


def has_binary_diff(diff: str) -> bool:
    return any(
        line.startswith("Binary files ")
        or line == "GIT binary patch"
        for line in diff.splitlines()
    )


def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def build_interdiff(previous_diff: str, current_diff: str) -> Optional[str]:
    import difflib

    previous = normalize_diff(previous_diff)
    current = normalize_diff(current_diff)
    if previous == current:
        return None
    return "\n".join(
        difflib.unified_diff(
            previous,
            current,
            fromfile="previous-effective-pr.diff",
            tofile="current-effective-pr.diff",
            lineterm="",
        )
    ) + "\n"


def prepare_effective_interdiff(
    base_repo: str,
    head_repo: str,
    last_review_base_sha: str,
    last_reviewed_sha: str,
    current_base_sha: str,
    current_sha: str,
) -> dict:
    result = {
        "review_mode": "full",
        "review_mode_reason": None,
        "interdiff_path": None,
        "previous_effective_diff_path": None,
        "current_effective_diff_path": None,
        "clear_last_reviewed_sha": False,
    }
    if head_repo != base_repo:
        result["review_mode_reason"] = mode_reason(
            "interdiff_head_repo_differs",
            "Effective interdiff is only enabled for same-repository PRs.",
            head_repo=head_repo,
            base_repo=base_repo,
        )
        result["clear_last_reviewed_sha"] = True
        return result

    previous_effective = fetch_compare_diff(
        base_repo,
        last_review_base_sha,
        last_reviewed_sha,
    )
    current_effective = fetch_compare_diff(
        base_repo,
        current_base_sha,
        current_sha,
    )
    if not previous_effective.diff or not current_effective.diff:
        result["review_mode_reason"] = mode_reason(
            "interdiff_unavailable",
            "Could not fetch both effective PR diffs for interdiff mode.",
            previous_status=previous_effective.status,
            previous_reason=previous_effective.reason,
            current_status=current_effective.status,
            current_reason=current_effective.reason,
        )
        result["clear_last_reviewed_sha"] = True
        return result

    previous_effective_diff_path = os.path.join(
        ".github",
        "previous-effective-pr.diff",
    )
    current_effective_diff_path = os.path.join(
        ".github",
        "current-effective-pr.diff",
    )
    write_file(previous_effective_diff_path, previous_effective.diff)
    write_file(current_effective_diff_path, current_effective.diff)
    result["previous_effective_diff_path"] = previous_effective_diff_path
    result["current_effective_diff_path"] = current_effective_diff_path

    interdiff = build_interdiff(
        previous_effective.diff,
        current_effective.diff,
    )
    if interdiff is None:
        result["review_mode"] = "unchanged"
        result["review_mode_reason"] = mode_reason(
            "interdiff_unchanged",
            "The effective PR diff is unchanged since the last review.",
        )
        return result

    if len(interdiff.encode("utf-8")) > MAX_DIFF_BYTES:
        result["review_mode_reason"] = mode_reason(
            "interdiff_too_large",
            "Effective interdiff exceeded the size limit.",
        )
        result["clear_last_reviewed_sha"] = True
        return result

    interdiff_path = os.path.join(".github", "interdiff.diff")
    write_file(interdiff_path, interdiff)
    result["review_mode"] = "interdiff"
    result["review_mode_reason"] = mode_reason(
        "effective_pr_interdiff",
        "Reviewing the difference between effective PR diffs.",
    )
    result["interdiff_path"] = interdiff_path
    return result


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
    raw_comments = gh_api_paginate(endpoint)
    print(f"Found {len(raw_comments)} comments")

    # Extract comment summaries
    comments = []
    for c in raw_comments:
        comments.append({
            "id": c["id"],
            "user": c.get("user", {}).get("login", "unknown"),
            "body": c.get("body", ""),
            "created_at": c.get("created_at", ""),
            "updated_at": c.get("updated_at", ""),
        })

    # Only bot-authored review comments are authoritative state. User-authored
    # markers are untrusted PR content and must not influence review mode.
    review_comments = sorted(
        [c for c in comments if is_bot_review_comment(c, summary_heading)],
        key=comment_sort_key,
    )

    # Extract state from the newest bot review comment owned by this workflow.
    last_reviewed_sha = None
    last_review_base_sha = None
    last_review_run_id = None
    previous_summary_comment_id = None
    legacy_summary_comment_id = None
    legacy_state_ignored = False
    for c in reversed(review_comments):
        state = parse_owned_review_state(c["body"], workflow_ref)
        if state is None:
            if parse_legacy_owned_review_state(c["body"], workflow_ref) is not None:
                legacy_state_ignored = True
            if legacy_summary_comment_id is None:
                legacy_summary_comment_id = c["id"]
            continue

        previous_summary_comment_id = c["id"]
        last_reviewed_sha = state.get("last_reviewed_sha")
        last_review_base_sha = state.get("base_sha")
        last_review_run_id = state.get("run_id")
        break

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
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    base_repo = (pr["base"].get("repo") or {}).get("full_name") or repo
    print(f"Current PR head: {current_sha[:12]}")
    print(f"Current PR base: {current_base_sha[:12]}")

    # This action intentionally does not check out PR head code under
    # pull_request_target. Use GitHub-provided diffs instead of relying on
    # local git history from untrusted code.
    review_mode = "full"
    review_mode_reason = mode_reason(
        "no_previous_state",
        "No previous valid review state found.",
    )
    incremental_diff_path = None
    interdiff_path = None
    previous_effective_diff_path = None
    current_effective_diff_path = None
    if not last_reviewed_sha:
        if legacy_state_ignored:
            print("Only legacy review state found, using full review mode")
            review_mode_reason = mode_reason(
                "legacy_state_requires_refresh",
                "Previous review state predates deterministic run markers; running one full review.",
            )
        else:
            print("No previous review state found, using full review mode")
    elif not head_repo:
        print("PR head repository is unavailable, using full review mode")
        review_mode_reason = mode_reason(
            "head_repo_unavailable",
            "PR head repository is unavailable.",
        )
        last_reviewed_sha = None
    elif last_review_base_sha == current_base_sha:
        direct = fetch_compare_diff(head_repo, last_reviewed_sha, current_sha)
        if direct.diff:
            incremental_diff_path = os.path.join(".github", "incremental.diff")
            write_file(incremental_diff_path, direct.diff)
            review_mode = "incremental"
            review_mode_reason = mode_reason(
                "direct_compare_ahead",
                "Current head is ahead of the last reviewed head on the same base.",
            )
            print(f"Incremental diff written to {incremental_diff_path}")
        else:
            print("No direct incremental diff available, trying effective PR interdiff")
            effective = prepare_effective_interdiff(
                base_repo,
                head_repo,
                last_review_base_sha,
                last_reviewed_sha,
                current_base_sha,
                current_sha,
            )
            review_mode = effective["review_mode"]
            review_mode_reason = effective["review_mode_reason"] or mode_reason(
                direct.reason or "direct_compare_unavailable",
                "Direct compare did not produce a usable incremental diff.",
                compare_status=direct.status,
            )
            interdiff_path = effective["interdiff_path"]
            previous_effective_diff_path = effective["previous_effective_diff_path"]
            current_effective_diff_path = effective["current_effective_diff_path"]
            if effective["clear_last_reviewed_sha"]:
                last_reviewed_sha = None
    else:
        print("PR base changed since last review, trying effective PR interdiff")
        effective = prepare_effective_interdiff(
            base_repo,
            head_repo,
            last_review_base_sha,
            last_reviewed_sha,
            current_base_sha,
            current_sha,
        )
        review_mode = effective["review_mode"]
        review_mode_reason = effective["review_mode_reason"] or review_mode_reason
        interdiff_path = effective["interdiff_path"]
        previous_effective_diff_path = effective["previous_effective_diff_path"]
        current_effective_diff_path = effective["current_effective_diff_path"]
        if effective["clear_last_reviewed_sha"]:
            last_reviewed_sha = None
        if review_mode == "unchanged":
            print("Effective PR diff is unchanged since last review")
        elif interdiff_path:
            print(f"Interdiff written to {interdiff_path}")

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
        "run_id": run_id,
        "summary_heading": summary_heading,
        "review_mode": review_mode,
        "review_mode_reason": review_mode_reason,
        "last_reviewed_sha": last_reviewed_sha,
        "last_review_base_sha": last_review_base_sha,
        "last_review_run_id": last_review_run_id,
        "previous_summary_comment_id": previous_summary_comment_id,
        "legacy_summary_comment_id": legacy_summary_comment_id,
        "legacy_state_ignored": legacy_state_ignored,
        "incremental_diff_path": incremental_diff_path,
        "interdiff_path": interdiff_path,
        "previous_effective_diff_path": previous_effective_diff_path,
        "current_effective_diff_path": current_effective_diff_path,
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
