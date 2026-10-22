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
# Shared with publish-review-report.py and fetch-pr-context.py so every side
# of the contract classifies comments identically. Thread classification is one
# of those sides: an unlisted bot login would read as a human reply here and
# suppress re-raising a finding, so a private copy drifting is fail-open.
from _review_state import BOT_LOGINS  # noqa: E402

LIST_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          isOutdated
          resolvedBy { login __typename }
          path
          line
          comments(first: 20) {
            totalCount
            nodes {
              body
              author { login __typename }
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


def resolve_thread(thread_id: str) -> tuple[bool, bool]:
    """Resolve a single review thread.

    Returns (resolved, permission_blocked). permission_blocked is True when the
    token is denied the resolveReviewThread mutation ("Resource not accessible
    by integration") — in that case every remaining thread would fail the same
    way, so the caller stops attempting.
    """
    try:
        gh_graphql(RESOLVE_THREAD_MUTATION, threadId=thread_id)
        return True, False
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        print(f"  Failed to resolve {thread_id}: {detail}", file=sys.stderr)
        return False, "Resource not accessible by integration" in detail


def severity_of(body: str) -> str:
    """Map a bot finding's emoji prefix to a severity label."""
    for prefix in REVIEW_PREFIXES:
        if body.startswith(prefix):
            if prefix.startswith("🔴"):
                return "security"
            if prefix.startswith("🟠"):
                return "bug"
            return "suggestion"
    return "unknown"


def _is_person(actor: Optional[dict]) -> bool:
    """Whether a GraphQL Actor is a human, by node type rather than by login.

    Every GitHub App is a `Bot`, so this excludes the other apps that write on
    these PRs as well as this reviewer. Matching on the login instead would
    need a list of every app anyone ever installs, and would miss them by
    default — the direction that silently retires a finding.
    """
    return bool(actor) and actor.get("__typename") == "User"


def collect_prior_findings(threads: list[dict]) -> list[dict]:
    """Build the prior-findings list the review prompt audits against.

    Every bot-authored review thread becomes one entry, whether resolved or
    not: thread state is not evidence of code state (a resolved thread does
    not mean the issue was fixed; an open one does not mean it is still
    present), so the reviewer re-verifies each entry against the current code.
    Unresolved entries sort first, then by path.

    `human_engaged` records whether a person has dealt with the thread at all:
    wrote in it, or resolved it. It is not evidence about the code either —
    it says the thread is not just this bot talking to itself. Step 3 of the
    review prompt branches on it to decide whether to open a NEW inline thread
    for a still-present finding, never to skip verifying one.

    It is the axis rather than `thread_resolved`, which does not separate the
    populations: should_resolve above resolves this reviewer's own outdated,
    reply-free threads on every run, so most resolved threads are the action's
    doing. `resolvedBy` is what tells those apart from a person clicking
    Resolve, which people do far more often than they reply.

    "A person" is GraphQL's `__typename == "User"`, not "a login outside
    BOT_LOGINS". Plenty of other apps write on these PRs — Copilot,
    CodeRabbit, linear-code, dependabot — and counting any of them as
    engagement would retire the finding from inline reporting for good. The
    login is no help: GraphQL reports the GitHub Actions bot as
    `github-actions`, with no `[bot]` suffix, which is why BOT_LOGINS carries
    both spellings. BOT_LOGINS stays narrow on purpose — _review_state uses it
    as a trust allowlist for summary authorship, which is a different question
    from "is this commenter a human".
    """
    findings = []
    for thread in threads:
        comments = thread["comments"]["nodes"]
        if not comments:
            continue
        first = comments[0]
        if (first.get("author") or {}).get("login", "") not in BOT_LOGINS:
            continue
        body = first.get("body", "")
        if not any(body.startswith(prefix) for prefix in REVIEW_PREFIXES):
            continue
        findings.append({
            "path": thread["path"],
            "line": thread.get("line"),
            "severity": severity_of(body),
            "excerpt": body.splitlines()[0][:200],
            "thread_resolved": thread["isResolved"],
            "thread_outdated": thread["isOutdated"],
            # A truncated thread counts as engaged-with. The query takes the
            # first 20 comments, so on a longer thread a human reply can sit
            # past the cut and read as "nobody ever answered".
            #
            # Note which way this errs, because it is not the usual one: the
            # audit suppresses a new inline thread when this is true, so True
            # buys silence, and for a reviewer silence is the unsafe
            # direction. It is chosen anyway because a thread past twenty
            # comments has demonstrably been engaged with, and re-raising into
            # a conversation that long is the worse failure. should_resolve
            # above reads the same truncation as a reason not to auto-resolve:
            # opposite literal value, same instinct — do not act on what was
            # not read.
            "human_engaged": (
                thread["comments"]["totalCount"] > len(comments)
                or any(_is_person(c.get("author")) for c in comments[1:])
                # Resolved by a person rather than by should_resolve, whose
                # resolutions come back as a Bot and so keep the re-anchor path.
                or _is_person(thread.get("resolvedBy"))
            ),
        })
    findings.sort(key=lambda f: (f["thread_resolved"], f["path"] or "", f["line"] or 0))
    return findings


def write_prior_findings(findings: list[dict]) -> None:
    output_path = os.path.join(".github", "prior-findings.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"prior_findings": findings}, f, indent=2)
    print(f"Prior findings written to {output_path} ({len(findings)} entries)")


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
    resolution_blocked = False
    for thread in to_resolve:
        comments = thread["comments"]["nodes"]
        body_preview = comments[0]["body"][:80] if comments else ""
        print(f"  Resolving: {thread['path']}:{thread.get('line', '?')} — {body_preview}...")
        ok, permission_blocked = resolve_thread(thread["id"])
        if ok:
            resolved.append({
                "path": thread["path"],
                "line": thread.get("line"),
                "body_preview": body_preview,
            })
        elif permission_blocked:
            # The Actions token is denied the resolveReviewThread mutation in
            # this context (observed across repos and orgs); every remaining
            # thread would fail identically, so stop here and let the review
            # rely on prior-findings.json instead of thread resolution.
            resolution_blocked = True
            print(
                "::warning::resolveReviewThread is denied for this token "
                "(Resource not accessible by integration); skipping the "
                f"remaining {len(to_resolve) - len(resolved) - 1} thread(s). "
                "Prior findings are still passed to the review via "
                ".github/prior-findings.json.",
                file=sys.stderr,
            )
            break

    summary = {
        "total_threads": len(threads),
        "outdated_bot_threads": len(to_resolve),
        "resolved_count": len(resolved),
        "resolved": resolved,
        "resolution_blocked": resolution_blocked,
    }

    write_summary(summary)
    # DELIBERATELY the pre-mutation snapshot. `resolve_thread` changes GitHub,
    # not these dicts, so a thread resolved seconds ago is still reported
    # unresolved and outdated — which is the one path that lets the review
    # re-anchor that finding at its new location before the resolution takes
    # effect for good. "Reporting the state we just set" looks like an obvious
    # cleanup and would silently turn every outdated finding into a summary
    # line forever. test_prior_findings_report_pre_resolution_state pins it.
    write_prior_findings(collect_prior_findings(threads))

    print(f"\nDone: resolved {len(resolved)}/{len(to_resolve)} threads")


if __name__ == "__main__":
    main()
