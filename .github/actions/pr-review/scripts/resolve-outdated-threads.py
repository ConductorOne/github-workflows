#!/usr/bin/env python3
"""Resolve outdated bot review threads on a GitHub PR.

Fetches all review threads via GraphQL, resolves any that are:
- not already resolved
- marked outdated by GitHub (code has changed since the comment)
- authored by our review bot (prefix: Security/Bug/Suggestion emoji)

Writes a JSON summary to .github/resolved-threads.json for the review prompt.
"""

import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

REVIEW_PREFIXES = ("🔴 Security:", "🟠 Bug:", "🟡 Suggestion:")
DEFAULT_API_ATTEMPTS = 3
HTTP_STATUS_PATTERN = re.compile(r"HTTP\s+(\d{3})")
BOT_LOGINS = {"github-actions[bot]", "github-actions"}

LIST_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 20) {
            totalCount
            nodes {
              body
              author { login }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { isResolved }
  }
}
"""


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


def retry_limit_for_error(error: subprocess.CalledProcessError) -> int:
    status = error_http_status(error)
    if status == 404:
        return min(DEFAULT_API_ATTEMPTS, 2)
    if status in (408, 429) or (status is not None and status >= 500):
        return DEFAULT_API_ATTEMPTS
    if status is None:
        return DEFAULT_API_ATTEMPTS
    return 1


def gh_graphql(query: str, **variables: str) -> dict:
    """Call gh api graphql and return parsed JSON."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        cmd.extend([flag, f"{key}={value}"])
    last_error = None
    for attempt in range(1, DEFAULT_API_ATTEMPTS + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            last_error = e
            retry_limit = retry_limit_for_error(e)
            e.retry_limit = retry_limit
            if attempt >= retry_limit:
                break
            print(
                "::warning::GitHub GraphQL request failed; "
                f"retrying ({attempt}/{retry_limit}): "
                f"{command_error_summary(e)}",
                file=sys.stderr,
            )
            time.sleep(attempt)
    raise last_error


def get_all_threads(owner: str, repo: str, number: int) -> list[dict]:
    """Fetch all review threads, handling pagination."""
    threads = []
    cursor = None
    while True:
        variables = {"owner": owner, "repo": repo, "number": number}
        if cursor:
            variables["after"] = cursor
        data = gh_graphql(LIST_THREADS_QUERY, **variables)
        pr = data["data"]["repository"]["pullRequest"]
        page = pr["reviewThreads"]
        threads.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return threads


def should_resolve(thread: dict) -> bool:
    """Check if a thread is an outdated bot comment that should be resolved."""
    if thread["isResolved"]:
        return False
    if not thread["isOutdated"]:
        return False
    comments = thread["comments"]["nodes"]
    if not comments:
        return False
    if thread["comments"]["totalCount"] != len(comments):
        return False
    if any((c.get("author") or {}).get("login", "") not in BOT_LOGINS for c in comments):
        return False
    body = comments[0].get("body", "")
    return any(body.startswith(prefix) for prefix in REVIEW_PREFIXES)


def resolve_thread(thread_id: str) -> bool:
    """Resolve a single review thread. Returns True on success."""
    try:
        gh_graphql(RESOLVE_THREAD_MUTATION, threadId=thread_id)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Failed to resolve {thread_id}: {e.stderr}", file=sys.stderr)
        return False


def write_summary(summary: dict) -> None:
    output_path = os.path.join(".github", "resolved-threads.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary written to {output_path}")


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    if not repo or not pr_number:
        print("GITHUB_REPOSITORY and PR_NUMBER must be set", file=sys.stderr)
        sys.exit(1)

    owner, repo_name = repo.split("/", 1)
    number = int(pr_number)

    print(f"Fetching review threads for {owner}/{repo_name}#{number}...")
    try:
        threads = get_all_threads(owner, repo_name, number)
    except subprocess.CalledProcessError as e:
        retry_limit = getattr(e, "retry_limit", DEFAULT_API_ATTEMPTS)
        print(
            "::error::Could not fetch review threads after "
            f"{retry_limit} attempt(s). Repository: {repo}. "
            f"PR: {pr_number}. Last error: {command_error_summary(e)}",
            file=sys.stderr,
        )
        raise
    print(f"Found {len(threads)} total review threads")

    to_resolve = [t for t in threads if should_resolve(t)]
    print(f"  {len(to_resolve)} are outdated bot comments to resolve")

    resolved = []
    for thread in to_resolve:
        comments = thread["comments"]["nodes"]
        body_preview = comments[0]["body"][:80] if comments else ""
        print(f"  Resolving: {thread['path']}:{thread.get('line', '?')} — {body_preview}...")
        if resolve_thread(thread["id"]):
            resolved.append({
                "path": thread["path"],
                "line": thread.get("line"),
                "body_preview": body_preview,
            })

    summary = {
        "total_threads": len(threads),
        "outdated_bot_threads": len(to_resolve),
        "resolved_count": len(resolved),
        "resolved": resolved,
    }

    write_summary(summary)

    print(f"\nDone: resolved {len(resolved)}/{len(to_resolve)} threads")


if __name__ == "__main__":
    main()
