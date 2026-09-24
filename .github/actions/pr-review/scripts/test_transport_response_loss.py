#!/usr/bin/env python3
"""Transport-level response-loss control for publish-review-report.py.

Drives the REAL _gh.request retry loop (only urllib.request.urlopen is faked)
through the REAL pub.main() entry point. The fake server persists each
creation POST and then loses the response (timeout). With max_attempts=1 the
finalizer must make exactly ONE transport attempt per creation and reconcile
by identity; a mutation removing the single-attempt guard retries the POST
through the real backoff loop and creates DUPLICATE server-side objects,
which this test detects. (The _gh.rest-level seams in
test_verdict_scaffolding.py replace the retry loop wholesale, so they cannot
catch that mutation.)

Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_transport_response_loss.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

_SCRIPTS_DIR = os.path.dirname(__file__)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import _gh  # noqa: E402
import _review_state as rs  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pub = _load("publish_review_report_transport", "publish-review-report.py")

HEAD = "17bacecea830e4b52d426e1a475d1c71bdcfd8ff"
BASE = "85e78ffc65a41576d3545c81aaedae26058ae625"
WORKFLOW_REF = "ConductorOne/github-workflows/.github/workflows/pr-review.yaml@refs/heads/main"
HEADING = "### Connector PR Review:"
ENV = {
    "GITHUB_REPOSITORY": "example/repo",
    "PR_NUMBER": "42",
    "SUMMARY_MARKER": HEADING,
    "REVIEW_RUN_STARTED_AT": "2026-09-23T20:00:00Z",
    "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
    "GITHUB_RUN_ID": "87654321",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SERVER_URL": "https://github.com",
    "GH_TOKEN": "x",
}


def working_body(n):
    return (
        f"{HEADING} gate: some PR\n\n"
        f"**Blocking Issues: {n}** | **Suggestions: 0** | **Threads Resolved: 0**\n\n"
        "### Review Summary\ndid things\n"
    )


class FakeResp:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.headers = headers or {}
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


class TransportServer:
    """In-memory GitHub REST server behind fake urlopen. Creation POSTs
    persist server-side and then lose the response (timeout)."""

    def __init__(self, lose_response_for=()):
        self.comments = {}
        self.reviews = []
        self.next_id = 900
        self.attempts = []  # (method, path) — every transport attempt
        self.lose_response_for = lose_response_for

    def _timeout(self):
        raise urllib.error.URLError("timed out")

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        path = req.full_url.split("api.github.com/", 1)[1].split("?")[0]
        data = json.loads(req.data) if req.data else None
        self.attempts.append((method, path))
        if method == "GET" and path == "repos/example/repo/pulls/42":
            return FakeResp(200, {"head": {"sha": HEAD}})
        if method == "GET" and path == "repos/example/repo/issues/42/comments":
            return FakeResp(200, sorted(self.comments.values(), key=lambda c: c["id"]))
        if method == "GET" and path == "repos/example/repo/pulls/42/reviews":
            return FakeResp(200, list(self.reviews))
        if method == "POST" and path == "repos/example/repo/issues/42/comments":
            cid = self.next_id
            self.next_id += 1
            self.comments[cid] = {
                "id": cid, "user": {"login": "github-actions[bot]"},
                "body": data["body"], "updated_at": "2026-09-23T20:30:00Z",
                "html_url": f"https://github.com/example/repo/pull/42#issuecomment-{cid}",
            }
            if "comments" in self.lose_response_for:
                self._timeout()  # persisted, response lost
            return FakeResp(201, self.comments[cid])
        if method == "POST" and path == "repos/example/repo/pulls/42/reviews":
            review = {
                "id": 700 + len(self.reviews),
                "user": {"login": "github-actions[bot]"},
                "body": data["body"], "commit_id": data.get("commit_id"),
                "state": {"REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}[data["event"]],
            }
            self.reviews.append(review)
            if "reviews" in self.lose_response_for:
                self._timeout()
            return FakeResp(200, review)
        if method == "PATCH" and path.startswith("repos/example/repo/issues/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            self.comments[cid]["body"] = data["body"]
            return FakeResp(200, self.comments[cid])
        raise AssertionError(f"unexpected transport call {method} {path}")

    def post_attempts(self, substr):
        return [p for m, p in self.attempts if m == "POST" and substr in p]


class TransportResponseLossTest(unittest.TestCase):
    def _run_main(self, server):
        seed = {
            "id": 2, "user": {"login": "github-actions[bot]"},
            "body": working_body(0), "updated_at": "2026-09-23T20:30:00Z",
            "html_url": "https://github.com/example/repo/pull/42#issuecomment-2",
        }
        server.comments[2] = seed
        tmpdir = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs(".github", exist_ok=True)
            with open(".github/pr-context.json", "w") as f:
                json.dump({"current_base_sha": BASE}, f)
            with (
                mock.patch.dict(os.environ, ENV),
                mock.patch.object(_gh.urllib.request, "urlopen", server.urlopen),
                mock.patch.object(pub.subprocess, "run",
                                  lambda *a, **k: SimpleNamespace(stdout=HEAD + "\n", stderr="")),
            ):
                try:
                    pub.main()
                    return 0
                except SystemExit as e:
                    return e.code or 0
        finally:
            os.chdir(old)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ambiguous_creation_posts_reconcile_without_duplicates(self):
        server = TransportServer(lose_response_for=("comments", "reviews"))
        code = self._run_main(server)
        self.assertEqual(code, 0)
        # Exactly ONE transport attempt per creation POST: the single-attempt
        # guard means the ambiguous response loss is never blind-retried...
        self.assertEqual(len(server.post_attempts("issues/42/comments")), 1)
        self.assertEqual(len(server.post_attempts("pulls/42/reviews")), 1)
        # ...and reconciliation by identity found the landed objects: exactly
        # one report and one review exist server-side.
        reports = [c for c in server.comments.values() if c["id"] >= 900]
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(server.reviews), 1)
        state = json.loads(rs.REVIEW_STATE_PATTERN.search(reports[0]["body"]).group(1))
        self.assertEqual(state["publication"], "completed")


if __name__ == "__main__":
    unittest.main()
