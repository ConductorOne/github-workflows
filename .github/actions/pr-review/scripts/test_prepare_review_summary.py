#!/usr/bin/env python3
"""Unit tests for prepare-review-summary.py.

The module file name contains hyphens, so it is loaded by path via importlib
rather than imported normally. Tests drive the real script entrypoint against
a functioning fake GitHub boundary (a local HTTP server; _gh.API_ROOT is
pointed at it) plus a real git checkout in a temp directory, and assert only
externally observable effects: comments created (or not), pr-context.json
rewrites, and GITHUB_OUTPUT lines.

Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_prepare_review_summary.py
"""

import importlib.util
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

_SCRIPTS_DIR = os.path.dirname(__file__)
# prepare-review-summary.py imports `_gh`; make the scripts directory
# importable regardless of how the test was invoked.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import _gh

_SCRIPT = os.path.join(_SCRIPTS_DIR, "prepare-review-summary.py")
_spec = importlib.util.spec_from_file_location("prepare_review_summary", _SCRIPT)
prs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prs)

HEADING = "### Test Review:"
NOW = "2026-09-25T00:00:00Z"


def _init_git_repo() -> str:
    """A real one-commit checkout; returns its HEAD SHA."""
    subprocess.run(["git", "init", "-q"], check=True)
    with open("seed.txt", "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "add", "seed.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Minimal GitHub REST fake: pulls read, comments list/create."""

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        state = self.server.state
        path = self.path.split("?", 1)[0]
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", path):
            return self._json(
                200, {"head": {"sha": state["head"]}, "base": {"sha": "b" * 40}}
            )
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            return self._json(200, state["comments"])
        return self._json(404, {"message": "not found"})

    def do_POST(self):
        state = self.server.state
        path = self.path.split("?", 1)[0]
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["requests"].append(("POST", path, raw))
            if state["fail_before_create"]:
                return self._json(503, {"message": "service unavailable"})
            data = json.loads(raw)
            cid = state["next_id"]
            state["next_id"] += 1
            comment = {
                "id": cid,
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": data.get("body"),
                "created_at": NOW,
                "updated_at": NOW,
            }
            state["comments"].append(comment)
            if state["drop_post_response"]:
                self.close_connection = True
                return
            if state["bad_id"] is not None:
                return self._json(201, {"id": state["bad_id"]})
            return self._json(201, comment)
        return self._json(404, {"message": "not found"})


class _FakeGitHub:
    def __init__(self, head):
        self.state = {
            "head": head,
            "comments": [],
            "next_id": 1000,
            "requests": [],
            "drop_post_response": False,
            "fail_before_create": False,
            "bad_id": None,
        }

    def __enter__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._patch = mock.patch.object(
            _gh, "API_ROOT", f"http://127.0.0.1:{self.server.server_address[1]}"
        )
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self.server.shutdown()
        self.server.server_close()


class PrepareReviewSummaryTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.sha = _init_git_repo()
        self.output_path = os.path.join(self.tmp.name, "github-output.txt")
        self.ctx = {
            "repository": "octo/repo",
            "pr_number": "7",
            "pr_title": "Test PR",
            "current_sha": self.sha,
            "current_base_sha": "b" * 40,
            "workflow_ref": "octo/repo/.github/workflows/review.yml@refs/heads/main",
            "summary_heading": HEADING,
            "review_mode": "full",
            "last_reviewed_sha": "old-sha",
            # A prior run's slot id is present in the context; the script must
            # never reuse it — it always creates a fresh slot.
            "summary_comment_id": 55,
            "comments": [],
        }
        self._write_context()
        self.env = {
            "GH_TOKEN": "dummy-token",
            "GITHUB_REPOSITORY": "octo/repo",
            "PR_NUMBER": "7",
            "REVIEW_SUMMARY_HEADING": HEADING,
            "GITHUB_OUTPUT": self.output_path,
        }
        self._base_env = dict(self.env)
        self._base_ctx = dict(self.ctx)
        self.gh = _FakeGitHub(head=self.sha)
        self.gh.__enter__()
        # The prior slot id exists on the PR as a completed report.
        self.gh.state["comments"].append(
            {
                "id": 55,
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": HEADING + '\n\nold report\n<!-- review-state: {} -->\n',
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

    def tearDown(self):
        self.gh.__exit__(None, None, None)
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _write_context(self):
        os.makedirs(os.path.dirname(prs.PR_CONTEXT_PATH), exist_ok=True)
        with open(prs.PR_CONTEXT_PATH, "w") as f:
            json.dump(self.ctx, f, indent=2)

    def _context_bytes(self) -> bytes | None:
        try:
            with open(prs.PR_CONTEXT_PATH, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _reset_state(self):
        self.env = dict(self._base_env)
        self.ctx = dict(self._base_ctx)
        self._write_context()
        self.gh.state["head"] = self.sha
        self.gh.state["requests"] = []
        self.gh.state["bad_id"] = None
        if os.path.exists(self.output_path):
            os.unlink(self.output_path)

    def _update_context(self, changes):
        self.ctx.update(changes)
        self._write_context()

    def _run(self):
        with mock.patch.dict(os.environ, self.env):
            return prs.main()

    def _posts(self):
        return [r for r in self.gh.state["requests"] if r[0] == "POST"]

    def test_creates_fresh_provisional_slot_and_binds_it(self):
        self.assertEqual(self._run(), 0)

        posts = self._posts()
        self.assertEqual(len(posts), 1)
        _, path, raw = posts[0]
        self.assertEqual(path, "/repos/octo/repo/issues/7/comments")
        body = json.loads(raw)["body"]
        # Fresh provisional slot: exact heading + shared provisional marker,
        # and no verdict/count/publication metadata of any kind.
        self.assertTrue(body.startswith(HEADING + "\n"))
        self.assertIn("_⏳ Provisional — deeper review still in progress._", body)
        self.assertNotIn("review-state", body)
        self.assertNotIn("review-publication", body)
        self.assertNotIn("Blocking Issues", body)

        # The prior slot id 55 (a completed report) is never reused.
        new_id = 1000
        with open(prs.PR_CONTEXT_PATH) as f:
            ctx = json.load(f)
        expected = dict(self._base_ctx)
        expected["summary_comment_id"] = new_id
        # summary_comment_id is rebound; every other field is preserved.
        self.assertEqual(ctx, expected)
        with open(self.output_path) as f:
            self.assertIn(f"comment_id={new_id}\n", f.read())

    def test_refuses_on_mismatched_context(self):
        cases = {
            "repo_mismatch": lambda: self.env.update(
                {"GITHUB_REPOSITORY": "octo/other"}
            ),
            "pr_mismatch": lambda: self.env.update({"PR_NUMBER": "8"}),
            "checkout_mismatch": lambda: (
                self._update_context({"current_sha": "0" * 40}),
                self.gh.state.update({"head": "0" * 40}),
            ),
            "live_head_moved": lambda: self.gh.state.update({"head": "f" * 40}),
            "invalid_heading": lambda: (
                self._update_context({"summary_heading": "## not a summary heading"}),
                self.env.update({"REVIEW_SUMMARY_HEADING": "## not a summary heading"}),
            ),
            "multiline_heading": lambda: (
                self._update_context({"summary_heading": "### A:\n### B:"}),
                self.env.update({"REVIEW_SUMMARY_HEADING": "### A:\n### B:"}),
            ),
            "env_heading_mismatch": lambda: self.env.update(
                {"REVIEW_SUMMARY_HEADING": "### Other:"}
            ),
            "missing_context": lambda: os.unlink(prs.PR_CONTEXT_PATH),
            "missing_output": lambda: self.env.update({"GITHUB_OUTPUT": ""}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self._reset_state()
                mutate()
                original = self._context_bytes()
                with self.assertRaises(SystemExit) as cm:
                    self._run()
                self.assertEqual(cm.exception.code, 1)
                # Zero mutations: no comment created, context untouched, no
                # output written.
                self.assertEqual(self._posts(), [])
                self.assertEqual(self._context_bytes(), original)
                self.assertFalse(os.path.exists(self.output_path))

    def test_rejects_invalid_api_comment_id(self):
        for bad in (0, "abc"):
            with self.subTest(bad_id=bad):
                self._reset_state()
                self.gh.state["bad_id"] = bad
                original = self._context_bytes()
                with self.assertRaises(SystemExit) as cm:
                    self._run()
                self.assertEqual(cm.exception.code, 1)
                # The POST was attempted, but an unusable id binds nothing.
                self.assertEqual(len(self._posts()), 1)
                self.assertEqual(self._context_bytes(), original)
                self.assertFalse(os.path.exists(self.output_path))

    def test_ambiguous_creation_never_adopts_another_runs_slot(self):
        cases = {
            "failed_before_creation": (True, False),
            "created_but_response_lost": (False, True),
        }
        for name, (fail_before, drop_response) in cases.items():
            with self.subTest(case=name):
                self._reset_state()
                self.gh.state["fail_before_create"] = fail_before
                self.gh.state["drop_post_response"] = drop_response
                # Another run can have an identical provisional body. It is
                # not evidence that this attempt's POST succeeded.
                self.gh.state["comments"][0]["body"] = prs.provisional_body(HEADING)
                original = self._context_bytes()
                with self.assertRaises(_gh.TransientOutageError):
                    self._run()
                self.assertEqual(len(self._posts()), 1)
                self.assertEqual(self._context_bytes(), original)
                self.assertFalse(os.path.exists(self.output_path))


if __name__ == "__main__":
    unittest.main()
