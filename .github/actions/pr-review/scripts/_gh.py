#!/usr/bin/env python3
"""Shared GitHub-request resilience helper for the PR-review action.

Every step in the PR-review composite action talks to GitHub. A transient
GitHub outage (5xx / 502 / 503 / 504), a secondary-rate-limit (403/429), or a
network blip should not fail a step closed with an opaque stack trace and hand
the PR author a bare red X. This module centralises:

- `request` / `rest` / `rest_paginate` / `graphql`: GitHub REST + GraphQL over
  stdlib `urllib` (no new runner deps), with exponential backoff + jitter, a
  bounded attempt count, and a total time budget.
- Retry classification: only *transient* failures are retried
  (HTTP 500/502/503/504, 429, secondary/primary rate-limit 403s, and
  connection/timeout errors). Terminal 4xx (401/404/422/permission-denied 403)
  fail fast — a real error is never retried into a false pass.
- `TransientOutageError` vs `TerminalError`: a typed distinction so callers can
  post an outage-aware, informational PR comment when a dependency is down while
  still failing hard on genuine errors.
- `report_outage`: best-effort, non-fatal outage signalling — tries to post an
  informational (never a verdict) PR comment, and always falls back to a job
  summary + `::warning`/`::error` annotation that need no REST API, so there is
  a visible signal even when GitHub itself is the outage.

Direct HTTP is used (rather than shelling out to `gh api`) specifically so the
real HTTP status code is available for retry classification — `gh` as a
subprocess only exposes an exit code + stderr text, a poor substrate for telling
a "503 transient" from a "422 terminal".

`gh` CLI is still the right tool for review submission (`gh pr review`); for that
path `run_gh_cli` adds a conservative stderr-pattern retry.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"
API_VERSION = "2022-11-28"
USER_AGENT = "conductorone-pr-review-action"

# HTTP statuses that always indicate a transient dependency problem.
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

# Defaults for the retry loop. Kept modest: the job has a 15-minute cap shared
# across many steps, so no single request should spend minutes retrying.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BUDGET_S = 45.0
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 15.0
DEFAULT_JITTER_S = 1.0


class GitHubError(Exception):
    """Base for GitHub request failures."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class TransientOutageError(GitHubError):
    """A dependency (GitHub) appears to be down: retries were exhausted on a
    transient failure class. Callers may post an informational outage comment."""


class TerminalError(GitHubError):
    """A genuine, non-retryable error (auth, not-found, unprocessable, or a
    permission-denied 403). Never retried, never converted into a pass."""


def token() -> str:
    """Resolve the GitHub token from the environment.

    The action exports it as GH_TOKEN; GITHUB_TOKEN is the platform default.
    """
    return (
        os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("github_token")
        or ""
    )


def _rate_limit_remaining(headers) -> int | None:
    raw = headers.get("x-ratelimit-remaining") if headers else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_transient_status(status: int, headers, body: str) -> bool:
    """Classify an HTTP status as transient (retry) vs terminal (fail fast).

    - 500/502/503/504 and 429: always transient.
    - 403: transient only when it is a rate-limit signal (Retry-After present,
      x-ratelimit-remaining == 0, or a secondary-rate-limit body). A plain 403
      is a permission denial — terminal.
    - Everything else (401/404/422/other 4xx): terminal.
    """
    if status in RETRYABLE_STATUS or status == 429:
        return True
    if status == 403:
        if headers and headers.get("Retry-After") is not None:
            return True
        if _rate_limit_remaining(headers) == 0:
            return True
        lowered = (body or "").lower()
        if "secondary rate limit" in lowered or "rate limit" in lowered:
            return True
        return False
    return False


def _retry_after_seconds(headers) -> float | None:
    """Honour a Retry-After / rate-limit-reset hint, if present."""
    if not headers:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass  # HTTP-date form: fall through to the reset header / backoff.
    if _rate_limit_remaining(headers) == 0:
        reset = headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                return max(0.0, float(reset) - time.time())
            except (TypeError, ValueError):
                pass
    return None


def _backoff_delay(attempt: int, base: float, cap: float, jitter: float) -> float:
    """Exponential backoff with full jitter, capped."""
    exp = min(cap, base * (2 ** (attempt - 1)))
    return exp + random.uniform(0.0, jitter)


def request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    budget_s: float = DEFAULT_BUDGET_S,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    jitter_s: float = DEFAULT_JITTER_S,
    timeout_s: float = 30.0,
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[int, dict, bytes]:
    """Perform an HTTP request with retry on transient failures.

    Returns (status, response-headers-dict, body-bytes) on success.
    Raises TransientOutageError when retries are exhausted on a transient class,
    or TerminalError immediately on a terminal status. Network errors
    (connection refused/reset, DNS, timeouts) are treated as transient.
    """
    deadline = now() + budget_s
    req_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    tok = token()
    if tok:
        req_headers["Authorization"] = f"Bearer {tok}"
    if headers:
        req_headers.update(headers)

    last_detail = ""
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - now()
        if remaining <= 0:
            break
        req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=min(timeout_s, remaining)) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - body read is best-effort
                pass
            status = e.code
            transient = _is_transient_status(status, e.headers, body)
            last_detail = f"HTTP {status} on {method} {url}: {body[:400]}"
            if not transient:
                raise TerminalError(last_detail, status=status, body=body) from e
            hint = _retry_after_seconds(e.headers)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            status = None
            last_detail = f"network error on {method} {url}: {e}"
            hint = None
        # Transient: back off and retry if attempts and budget remain.
        if attempt >= max_attempts:
            break
        remaining = deadline - now()
        if remaining <= 0:
            break
        if hint is not None:
            if hint > remaining:
                # The server requested a cooldown (Retry-After / rate-limit
                # reset) longer than the remaining budget. Shortening it would
                # violate GitHub's rate-limit contract; stop as an outage
                # instead of retrying early or overruning the budget.
                break
            delay = hint
        else:
            delay = min(
                _backoff_delay(attempt, base_delay_s, max_delay_s, jitter_s),
                max(0.0, remaining),
            )
        print(
            f"  transient GitHub failure (attempt {attempt}/{max_attempts}): "
            f"{last_detail}; retrying in {delay:.1f}s",
            file=sys.stderr,
        )
        sleep(delay)
    raise TransientOutageError(
        f"GitHub request failed after {max_attempts} attempt(s): {last_detail}"
    )


def _api_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{API_ROOT}/{path.lstrip('/')}"


def rest(method: str, path: str, *, data: dict | None = None, **kw) -> dict | list | None:
    """Call a GitHub REST endpoint and return parsed JSON (or None for 204)."""
    body = None
    headers = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    status, _, raw = request(method, _api_url(path), data=body, headers=headers, **kw)
    if status == 204 or not raw:
        return None
    return json.loads(raw)


def rest_paginate(path: str, **kw) -> list:
    """Fetch every page of a REST collection, following the Link `next` rel.

    Replaces the `gh api --paginate` + line-splitting parsing that was
    copy-pasted across scripts.
    """
    entries: list = []
    url = _api_url(path)
    # Ask for the max page size to minimise round-trips.
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}per_page=100"
    while url:
        status, headers, raw = request("GET", url, **kw)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)
        url = _next_link(headers.get("Link"))
    return entries


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        rels = [s.strip() for s in segments[1:]]
        if 'rel="next"' in rels:
            return url_part[1:-1]
    return None


def graphql(query: str, variables: dict | None = None, **kw) -> dict:
    """Execute a GraphQL query/mutation, returning the `data` object.

    HTTP-transport failures (5xx) are retried by `request`. A 200 response that
    carries a `errors` array is a query-level error: it is terminal (the query
    is wrong) unless GitHub flagged it RATE_LIMITED, which is transient.
    """
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    body = json.dumps(payload).encode("utf-8")
    _, _, raw = request(
        "POST",
        GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        **kw,
    )
    parsed = json.loads(raw)
    errors = parsed.get("errors")
    if errors:
        types = {(e.get("type") or "").upper() for e in errors}
        messages = " ".join(str(e.get("message", "")) for e in errors).lower()
        if "RATE_LIMITED" in types or "rate limit" in messages:
            raise TransientOutageError(f"GraphQL rate limited: {errors}")
        raise TerminalError(f"GraphQL errors: {errors}")
    return parsed["data"]


# --------------------------------------------------------------------------- #
# gh CLI with retry (for paths where the CLI is the right tool, e.g. review    #
# submission). Classification here is coarser — we only have stderr text — so  #
# we retry a conservative set of transient-looking patterns.                   #
# --------------------------------------------------------------------------- #

_TRANSIENT_STDERR = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "http 429",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "gateway timeout",
    "timeout",
    "timed out",
    "secondary rate limit",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "eof",
)


def _stderr_looks_transient(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return any(pat in lowered for pat in _TRANSIENT_STDERR)


def run_gh_cli(
    args: list[str],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    jitter_s: float = DEFAULT_JITTER_S,
    sleep=time.sleep,
    **run_kw,
) -> subprocess.CompletedProcess:
    """Run `gh <args>` with retry on transient-looking stderr.

    Returns the CompletedProcess on success (returncode 0). Raises
    TransientOutageError if a transient failure persists across attempts, or
    TerminalError on a non-transient non-zero exit.
    """
    run_kw.setdefault("capture_output", True)
    run_kw.setdefault("text", True)
    last: subprocess.CompletedProcess | None = None
    for attempt in range(1, max_attempts + 1):
        last = subprocess.run(["gh", *args], check=False, **run_kw)
        if last.returncode == 0:
            return last
        if not _stderr_looks_transient(last.stderr or ""):
            raise TerminalError(
                f"gh {' '.join(args[:2])} failed: {(last.stderr or '').strip()}"
            )
        if attempt >= max_attempts:
            break
        delay = _backoff_delay(attempt, base_delay_s, max_delay_s, jitter_s)
        print(
            f"  transient gh CLI failure (attempt {attempt}/{max_attempts}): "
            f"{(last.stderr or '').strip()[:300]}; retrying in {delay:.1f}s",
            file=sys.stderr,
        )
        sleep(delay)
    raise TransientOutageError(
        f"gh {' '.join(args[:2])} failed after {max_attempts} attempt(s): "
        f"{(last.stderr or '').strip() if last else 'unknown'}"
    )


# --------------------------------------------------------------------------- #
# Outage-aware, informational signalling.                                      #
# --------------------------------------------------------------------------- #

OUTAGE_COMMENT_MARKER = "<!-- pr-review-outage -->"

_SOURCE_BLURB = {
    "github": "GitHub's API returned repeated errors",
    "anthropic": "the Anthropic API is currently degraded",
}


STATUS_PAGES = {
    "github": "https://www.githubstatus.com/",
    "anthropic": "https://status.anthropic.com/",
}


def _status_link(source: str | None) -> str | None:
    """Return the provider status page URL for a classified source, if known."""
    return STATUS_PAGES.get(source or "")


# --------------------------------------------------------------------------- #
# Review-stage failure marker.                                                 #
#                                                                              #
# A review-stage step (stamp / submit-verdict) that fails cannot post the      #
# outage/incomplete notice itself without racing the always() "classify"       #
# step, which would double-post. Instead the failing script drops a small      #
# JSON marker describing WHY it failed; the single classify step reads it and  #
# posts exactly one informational notice. The marker lands under the harvested #
# claude-debug/ tree so it is also uploaded with the review-context artifact.  #
# --------------------------------------------------------------------------- #

FAILURE_MARKER_FILENAME = "review-failure.json"


def _failure_marker_path() -> str:
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    return os.path.join(workspace, ".github", "claude-debug", FAILURE_MARKER_FILENAME)


def write_failure_marker(kind: str, detail: str, *, source: str | None = None) -> None:
    """Record why a review-stage step failed, for the classify step to post.

    `kind` is a coarse class ("outage", "no-verdict", "no-summary",
    "sha-mismatch", "failure"); `source` is the provider ("github"/"anthropic")
    when `kind == "outage"`, else None. Best-effort: never raises, so it cannot
    turn a fail-closed exit into a crash that skips the exit code.
    """
    path = _failure_marker_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"kind": kind, "source": source, "detail": detail}, fh)
    except OSError as e:
        print(f"  could not write review-failure marker: {e}", file=sys.stderr)


def read_failure_marker() -> dict | None:
    """Return the review-stage failure marker written by a failing step, if any."""
    path = _failure_marker_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation. Needs no REST API — visible even when
    GitHub's API is the outage."""
    # Newlines break the annotation command; collapse them.
    flat = " ".join(message.split())
    print(f"::{level}::{flat}")


def _job_summary(markdown: str) -> None:
    """Append to the GitHub Actions job summary. File write, no REST API."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    except OSError as e:
        print(f"  could not write job summary: {e}", file=sys.stderr)


def outage_comment_body(source: str, detail: str) -> str:
    """Compose the informational PR comment. Never a verdict."""
    blurb = _SOURCE_BLURB.get(source, "a required service is currently degraded")
    status = _status_link(source)
    status_line = f"**Status page:** {status}\n\n" if status else ""
    return (
        f"{OUTAGE_COMMENT_MARKER}\n"
        f"### Automated PR review could not complete\n\n"
        f"> [!WARNING]\n"
        f"> This is an **informational notice, not a review verdict.** "
        f"It does **not** approve or request changes on your PR.\n\n"
        f"The automated PR review could not complete because {blurb}.\n\n"
        f"**Details:** {detail}\n\n"
        f"{status_line}"
        f"This reflects a dependency outage, **not** a judgement on your change. "
        f"The review will re-run automatically on the next push or workflow retry."
    )


def _post_comment_best_effort(repo: str, pr_number: str, body: str) -> bool:
    """Try to post an issue comment. Returns True on success; never raises.

    If GitHub itself is the outage this will likely fail too — that is why it is
    best-effort and always paired with the annotation/summary fallback.
    """
    try:
        rest(
            "POST",
            f"repos/{repo}/issues/{pr_number}/comments",
            data={"body": body},
            max_attempts=2,
            budget_s=15.0,
        )
        return True
    except Exception as e:  # noqa: BLE001 - best-effort; must not crash the step
        print(f"  could not post outage comment: {e}", file=sys.stderr)
        return False


def report_outage(
    source: str,
    detail: str,
    *,
    repo: str | None = None,
    pr_number: str | None = None,
    annotation_level: str = "warning",
) -> bool:
    """Signal an outage on the PR: informational, best-effort, non-fatal.

    1. Always emits a job-summary entry + a `::warning`/`::error` annotation
       (no REST API, so there is always a visible signal).
    2. Best-effort posts an informational (never a verdict) PR comment.

    Returns True if the PR comment posted, False otherwise. Never raises.
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = pr_number or os.environ.get("PR_NUMBER", "")

    blurb = _SOURCE_BLURB.get(source, "a required service is currently degraded")
    status = _status_link(source)
    status_annot = f" Status: {status}" if status else ""
    status_summary = f"Status page: {status}\n" if status else ""
    _annotate(
        annotation_level,
        f"PR review could not complete: {blurb}. {detail} "
        f"This is not a verdict; the review will re-run.{status_annot}",
    )
    _job_summary(
        f"### PR Review could not complete\n\n"
        f"The review could not complete because {blurb}. "
        f"**This is not a verdict on the change.**\n\n"
        f"Details: `{detail}`\n"
        f"{status_summary}"
    )

    if not repo or not pr_number:
        print(
            "  no repo/PR number available; skipping outage PR comment "
            "(annotation + job summary still emitted)",
            file=sys.stderr,
        )
        return False
    body = outage_comment_body(source, detail)
    return _post_comment_best_effort(repo, pr_number, body)


def report_review_incomplete(
    headline: str,
    explanation: str,
    detail: str,
    *,
    source: str | None = None,
    repo: str | None = None,
    pr_number: str | None = None,
    annotation_level: str = "warning",
) -> bool:
    """Post a general "review could not complete" informational notice.

    Used by the failure-classifier for the non-outage cases (a genuine review
    error, or a cancel/timeout) where the fixed GitHub/Anthropic outage wording
    does not fit. Same guarantees as report_outage: always annotates +
    summarises (no REST API needed), best-effort posts an informational (never a
    verdict) PR comment, never raises.

    `source` is optional and defaults to None: these callers are the cancel /
    timeout / genuine-failure classes, which are deliberately NOT provider
    outages, so no status-page link is surfaced. Pass a known provider
    ("github"/"anthropic") only when one is genuinely classified as down.
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = pr_number or os.environ.get("PR_NUMBER", "")

    status = _status_link(source)
    status_annot = f" Status: {status}" if status else ""
    status_summary = f"Status page: {status}\n" if status else ""
    status_line = f"**Status page:** {status}\n\n" if status else ""
    _annotate(
        annotation_level,
        f"{headline}: {explanation} {detail} This is not a verdict.{status_annot}",
    )
    _job_summary(
        f"### {headline}\n\n{explanation} **This is not a verdict on the change.**\n\n"
        f"Details: `{detail}`\n"
        f"{status_summary}"
    )
    if not repo or not pr_number:
        print(
            "  no repo/PR number available; skipping PR comment "
            "(annotation + job summary still emitted)",
            file=sys.stderr,
        )
        return False
    body = (
        f"{OUTAGE_COMMENT_MARKER}\n"
        f"### Automated PR review could not complete\n\n"
        f"> [!WARNING]\n"
        f"> This is an **informational notice, not a review verdict.** "
        f"It does **not** approve or request changes on your PR.\n\n"
        f"{explanation}\n\n"
        f"**Details:** {detail}\n\n"
        f"{status_line}"
        f"The review will re-run automatically on the next push or workflow retry."
    )
    return _post_comment_best_effort(repo, pr_number, body)
