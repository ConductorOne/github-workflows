#!/usr/bin/env python3
"""Fetch PR comments and extract review state for the review prompt.

Fetches all issue comments via gh api, then extracts:
- last_reviewed_sha: the SHA from the <!-- review-state: ... --> marker
- review_mode: "incremental" when a GitHub API compare diff is available, otherwise "full"
- Trusted owner/member/collaborator comments (for human review context)

Writes structured JSON to .github/pr-context.json.
"""

import json
import os
import re
import shlex
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
TRUSTED_COMMENT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
DEFAULT_REVIEW_SUMMARY_HEADING = "### Connector PR Review:"
LEGACY_REVIEW_SUMMARY_HEADING = "### PR Review:"
DEFAULT_API_ATTEMPTS = 3

# Incremental-diff hardening. GitHub compare diffs on large vendor-refresh PRs
# can inline non-UTF-8 bytes (git misclassifies a NUL-free encrypted vendored
# file as text) and can be pathologically large (100s of MB), so the raw diff is
# read as bytes, stripped of vendored/generated noise, capped, and decoded
# losslessly before it is handed to the reviewer.
DIFF_MAX_BYTES = 20 * 1024 * 1024
EXCLUDE_PREFIXES = ("vendor/",)
EXCLUDE_SUFFIXES = (
    ".pb.go",
    "_gen.go",
    "package-lock.json",
    "yarn.lock",
)
DIFF_GIT_SPLIT_PATTERN = re.compile(rb"(?m)^(?=diff --git )")
DIFF_GIT_PATH_PATTERN = re.compile(r"^diff --git (a/.*) b/(.*)$")
DIFF_DROPPED_PATH_LIMIT = 200


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


def gh_api_bytes(args: list[str], *, attempts: int = DEFAULT_API_ATTEMPTS) -> bytes:
    """Run gh api like gh_api but return raw stdout bytes without UTF-8 decoding.

    The compare diff for a large vendor-refresh PR can contain a non-UTF-8 byte,
    so a strict text decode of the whole response (what text=True does) raises
    UnicodeDecodeError and kills the review job. Reading raw bytes here lets the
    caller decode leniently. The retry policy mirrors gh_api exactly; stderr and
    stdout are decoded leniently only to build human-readable error messages.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                ["gh", "api", *args],
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError as e:
            if isinstance(e.stderr, bytes):
                e.stderr = e.stderr.decode("utf-8", "backslashreplace")
            if isinstance(e.stdout, bytes):
                e.stdout = e.stdout.decode("utf-8", "backslashreplace")
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


def _split_git_header_paths(header: bytes) -> list[str]:
    text = header.decode("utf-8", "backslashreplace")
    match = DIFF_GIT_PATH_PATTERN.match(text)
    if match:
        return [match.group(1), f"b/{match.group(2)}"]
    try:
        parts = shlex.split(text)
    except ValueError:
        return []
    if len(parts) < 4 or parts[:2] != ["diff", "--git"]:
        return []
    return parts[2:4]


def _strip_diff_prefix(path: str) -> str:
    if path == "/dev/null":
        return ""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _decode_diff_path(raw: bytes) -> str:
    return _strip_diff_prefix(raw.decode("utf-8", "backslashreplace"))


def _diff_paths(header: bytes) -> list[str]:
    """Extract old and new paths from a 'diff --git ...' header."""
    return [
        path
        for path in (_strip_diff_prefix(path) for path in _split_git_header_paths(header))
        if path
    ]


def _diff_path(header: bytes) -> str:
    """Extract the a-side file path from a 'diff --git ...' header."""
    paths = _diff_paths(header)
    if not paths:
        return ""
    return paths[0]


def _section_paths(section: bytes) -> list[str]:
    paths = []
    for index, line in enumerate(section.splitlines()):
        path = ""
        if index == 0:
            paths.extend(_diff_paths(line))
            continue
        for prefix in (b"--- ", b"+++ "):
            if line.startswith(prefix):
                path = _decode_diff_path(line[len(prefix) :])
                break
        for prefix in (b"rename from ", b"rename to ", b"copy from ", b"copy to "):
            if line.startswith(prefix):
                path = _decode_diff_path(line[len(prefix) :])
                break
        if path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _is_excluded(path: str) -> bool:
    """Report whether a diff path is vendored or a known generated/lockfile."""
    if not path:
        return False
    if any(path.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
        return True
    return any(path.endswith(suffix) for suffix in EXCLUDE_SUFFIXES)


def filter_and_decode_diff(raw: bytes) -> tuple[str, dict]:
    """Strip vendored/generated noise from a raw diff, cap it, and decode it.

    Splits ``raw`` into per-file ``diff --git`` sections, drops vendored and
    generated/lockfile sections, retains sections up to ``DIFF_MAX_BYTES``, and
    decodes the kept bytes with errors="backslashreplace" so a stray non-UTF-8
    byte is preserved losslessly instead of raising. Returns the decoded text and
    metadata: ``dropped_sections`` (count), ``dropped_paths`` (bounded list),
    ``truncated`` (bool), ``kept_bytes``.
    """
    sections = DIFF_GIT_SPLIT_PATTERN.split(raw)
    kept_chunks: list[bytes] = []
    dropped_sections = 0
    dropped_paths: list[str] = []
    dropped_paths_omitted = 0
    kept_bytes = 0
    truncated = False
    for section in sections:
        if not section:
            continue
        if not section.startswith(b"diff --git "):
            # Leading preamble before the first file header (rare); keep verbatim.
            if section.strip():
                kept_chunks.append(section)
                kept_bytes += len(section)
            continue
        paths = _section_paths(section)
        if paths and all(_is_excluded(path) for path in paths):
            dropped_sections += 1
            for path in paths:
                if len(dropped_paths) < DIFF_DROPPED_PATH_LIMIT:
                    dropped_paths.append(path)
                else:
                    dropped_paths_omitted += 1
            continue
        if kept_bytes + len(section) > DIFF_MAX_BYTES:
            truncated = True
            break
        kept_chunks.append(section)
        kept_bytes += len(section)

    text = b"".join(kept_chunks).decode("utf-8", "backslashreplace")
    if truncated:
        text += f"\n[diff truncated to {DIFF_MAX_BYTES} bytes for review context]\n"
    return text, {
        "dropped_sections": dropped_sections,
        "dropped_paths": dropped_paths,
        "dropped_paths_omitted": dropped_paths_omitted,
        "truncated": truncated,
        "kept_bytes": kept_bytes,
        "partial": bool(dropped_sections or truncated),
    }


def empty_incremental_diff_metadata() -> dict:
    return {
        "dropped_sections": 0,
        "dropped_paths": [],
        "dropped_paths_omitted": 0,
        "truncated": False,
        "kept_bytes": 0,
        "partial": False,
    }


def incremental_diff_notice(meta: dict) -> str:
    lines = [
        "[incremental diff partial coverage]",
        (
            f"Dropped {meta['dropped_sections']} vendored/generated/lockfile "
            f"section(s); kept {meta['kept_bytes']} bytes."
        ),
    ]
    if meta["dropped_paths"]:
        lines.append("Dropped paths:")
        lines.extend(f"- {path}" for path in meta["dropped_paths"])
    if meta["dropped_paths_omitted"]:
        lines.append(f"- ... {meta['dropped_paths_omitted']} more path(s)")
    if meta["truncated"]:
        lines.append(f"Diff truncated to {DIFF_MAX_BYTES} bytes.")
    lines.append("Scan the full PR diff before issuing a no-blocking-issues verdict.")
    return "\n".join(lines) + "\n\n"


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


def fetch_compare_diff(head_repo: str, base_sha: str, head_sha: str) -> tuple[Optional[str], dict]:
    """Fetch a compare diff from the PR head repo for incremental review."""
    endpoint = f"repos/{head_repo}/compare/{base_sha}...{head_sha}"
    meta = empty_incremental_diff_metadata()
    try:
        metadata = gh_api([endpoint])
        compare = json.loads(metadata.stdout)
        status = compare.get("status", "")
        if status != "ahead":
            print(
                f"Compare status is {status!r}, using full review mode",
                file=sys.stderr,
            )
            return None, meta
        raw = gh_api_bytes(["-H", "Accept: application/vnd.github.diff", endpoint])
    except subprocess.CalledProcessError as e:
        print(
            f"Could not fetch incremental diff from {head_repo}: {e.stderr}",
            file=sys.stderr,
        )
        return None, meta
    if not raw.strip():
        return None, meta
    diff_text, meta = filter_and_decode_diff(raw)
    if meta["truncated"] and meta["kept_bytes"] == 0:
        print(
            "Incremental diff truncated before retaining reviewable content, "
            "using full review mode",
            file=sys.stderr,
        )
        return None, meta
    if not diff_text.strip():
        return None, meta
    if meta["partial"]:
        diff_text = incremental_diff_notice(meta) + diff_text
    print(
        f"Incremental diff: dropped {meta['dropped_sections']} vendored/generated "
        f"section(s), kept {meta['kept_bytes']} bytes"
        f"{', truncated' if meta['truncated'] else ''}",
        file=sys.stderr,
    )
    return diff_text, meta


def current_checkout_sha() -> Optional[str]:
    """Return the current git checkout SHA, if the workspace is a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return result.stdout.strip()


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    expected_head_sha = os.environ.get("PR_HEAD_SHA", "").strip()
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

    # Keep bot review comments for authoritative state, but only expose trusted
    # owner/member/collaborator human comments to the review prompt. Public repo
    # comments from contributors or random users are untrusted prompt input.
    state_comments = []
    trusted_context_comments = []
    for c in raw_comments:
        author_association = c.get("author_association", "NONE")
        user = c.get("user") or {}
        comment = {
            "id": c["id"],
            "user": user.get("login", "unknown"),
            "user_type": user.get("type", "unknown"),
            "author_association": author_association,
            "body": c.get("body", ""),
        }
        state_comments.append(comment)
        if user.get("type") == "User" and author_association in TRUSTED_COMMENT_ASSOCIATIONS:
            trusted_context_comments.append(comment)

    ignored_count = len(state_comments) - len(trusted_context_comments)
    print(f"Trusted review-context comments: {len(trusted_context_comments)}")
    print(f"Ignored untrusted or bot comments for prompt context: {ignored_count}")

    # Only bot-authored review comments are authoritative state. User-authored
    # markers are untrusted PR content and must not influence review mode.
    review_comments = [c for c in state_comments if is_bot_review_comment(c, summary_heading)]

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
    live_head_sha = pr["head"]["sha"]
    if expected_head_sha and live_head_sha != expected_head_sha:
        print(
            f"PR head changed before review started: event={expected_head_sha}, live={live_head_sha}",
            file=sys.stderr,
        )
        sys.exit(1)

    checkout_sha = current_checkout_sha()
    if expected_head_sha and checkout_sha != expected_head_sha:
        print(
            f"Checkout SHA does not match event PR head: checkout={checkout_sha}, event={expected_head_sha}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not expected_head_sha and checkout_sha and checkout_sha != live_head_sha:
        print(
            f"Checkout SHA does not match live PR head: checkout={checkout_sha}, live={live_head_sha}",
            file=sys.stderr,
        )
        sys.exit(1)

    current_sha = expected_head_sha or live_head_sha
    current_base_sha = pr["base"]["sha"]
    current_base_ref = pr["base"].get("ref")
    base_default_branch = (pr["base"].get("repo") or {}).get("default_branch")
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    print(f"Current PR head: {current_sha[:12]}")
    print(f"Current PR base: {current_base_sha[:12]}")
    if current_base_ref:
        print(f"Current PR base ref: {current_base_ref}")

    # Review runs only for same-repo PRs with PR head checked out. GitHub
    # compare diffs are used only to select incremental/full review mode and to
    # provide a compact incremental artifact.
    review_mode = "full"
    incremental_diff_path = None
    incremental_diff_metadata = empty_incremental_diff_metadata()
    if not last_reviewed_sha:
        print("No previous review state found, using full review mode")
    elif last_review_base_sha != current_base_sha:
        print("PR base changed since last review, using full review mode")
        last_reviewed_sha = None
    elif not head_repo:
        print("PR head repository is unavailable, using full review mode")
        last_reviewed_sha = None
    else:
        incremental_diff, incremental_diff_metadata = fetch_compare_diff(
            head_repo,
            last_reviewed_sha,
            current_sha,
        )
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
    # Trusted human comments remain available as context, but they are not
    # authoritative review state and cannot suppress findings by mimicking the
    # summary format.
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
        "current_base_ref": current_base_ref,
        "base_default_branch": base_default_branch,
        "workflow_ref": workflow_ref,
        "review_run_url": review_run_url,
        "summary_heading": summary_heading,
        "review_mode": review_mode,
        "last_reviewed_sha": last_reviewed_sha,
        "last_review_base_sha": last_review_base_sha,
        "summary_comment_id": summary_comment_id,
        "incremental_diff_path": incremental_diff_path,
        "incremental_diff_metadata": incremental_diff_metadata,
        "existing_findings": existing_findings,
        "comments": trusted_context_comments,
    }

    output_path = os.path.join(".github", "pr-context.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(context, f, indent=2)

    print(f"Context written to {output_path}")


if __name__ == "__main__":
    main()
