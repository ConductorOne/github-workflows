#!/usr/bin/env python3
"""Unit tests for the CI verdict scaffolding: submit-verdict-review.py,
stamp-review-state.py, and the prior-findings additions to
resolve-outdated-threads.py.

The module file names contain hyphens, so they are loaded by path via
importlib rather than imported normally. Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_verdict_scaffolding.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

_SCRIPTS_DIR = os.path.dirname(__file__)
# The scripts `import _gh`; make the scripts directory importable.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _load(name: str, filename: str):
    path = os.path.join(_SCRIPTS_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = _load("submit_verdict_review", "submit-verdict-review.py")
stamp = _load("stamp_review_state", "stamp-review-state.py")
rot = _load("resolve_outdated_threads", "resolve-outdated-threads.py")


def _thread(
    body: str,
    *,
    author: str = "github-actions[bot]",
    resolved: bool = False,
    outdated: bool = False,
    path: str = "pkg/foo.go",
    line: int | None = 42,
) -> dict:
    return {
        "id": "PRRT_x",
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "comments": {
            "totalCount": 1,
            "nodes": [{"body": body, "author": {"login": author}}],
        },
    }


class VerdictToReviewTest(unittest.TestCase):
    def test_blocking_findings_request_changes(self):
        body = "### Connector PR Review: t\n\n**Blocking Issues: 2** | **Suggestions: 1**\n"
        self.assertEqual(
            sv.verdict_to_review(body),
            ("--request-changes", "Blocking issues found — see review comments."),
        )

    def test_zero_blocking_leaves_neutral_comment(self):
        body = "**Blocking Issues: 0** | **Suggestions: 3** | **Threads Resolved: 0**"
        self.assertEqual(
            sv.verdict_to_review(body),
            ("--comment", "No blocking issues found."),
        )

    def test_unparseable_body_returns_none(self):
        self.assertIsNone(sv.verdict_to_review("no counts here"))

    def test_never_approves(self):
        # Every parseable outcome must be request-changes or comment; the
        # reviewer has no approve path by design.
        for n in ("0", "1", "17"):
            flag, _ = sv.verdict_to_review(f"**Blocking Issues: {n}**")
            self.assertIn(flag, ("--request-changes", "--comment"))


class ShaBindingTest(unittest.TestCase):
    HEAD = "17bacecea830e4b52d426e1a475d1c71bdcfd8ff"

    def test_full_sha_matches(self):
        self.assertTrue(sv.sha_bound_to_head(self.HEAD, self.HEAD))

    def test_prefix_matches(self):
        self.assertTrue(sv.sha_bound_to_head("17bacec", self.HEAD))

    def test_other_sha_rejected(self):
        self.assertFalse(sv.sha_bound_to_head("85e78ffc65a4", self.HEAD))

    def test_placeholder_and_empty_rejected(self):
        self.assertFalse(sv.sha_bound_to_head("CURRENT_SHA", self.HEAD))
        self.assertFalse(sv.sha_bound_to_head("", self.HEAD))
        self.assertFalse(sv.sha_bound_to_head(None, self.HEAD))

    def test_short_prefix_rejected(self):
        self.assertFalse(sv.sha_bound_to_head("17ba", self.HEAD))


class StampMarkerTest(unittest.TestCase):
    def test_marker_includes_base_and_workflow_ref(self):
        with mock.patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW_REF": "ConductorOne/github-workflows/.github/workflows/pr-review.yaml@refs/heads/main"},
        ), mock.patch.object(stamp, "current_base_sha", return_value="85e78ffc65a4"):
            marker = stamp.build_marker("17bacece")
        state = json.loads(stamp.REVIEW_STATE_PATTERN.search(marker).group(1))
        self.assertEqual(state["last_reviewed_sha"], "17bacece")
        self.assertEqual(state["base_sha"], "85e78ffc65a4")
        self.assertEqual(
            state["workflow_ref"],
            "ConductorOne/github-workflows/.github/workflows/pr-review.yaml@refs/heads/main",
        )

    def test_marker_omits_missing_optional_fields(self):
        with mock.patch.dict(os.environ, {"GITHUB_WORKFLOW_REF": ""}), mock.patch.object(
            stamp, "current_base_sha", return_value=None
        ):
            marker = stamp.build_marker("17bacece")
        state = json.loads(stamp.REVIEW_STATE_PATTERN.search(marker).group(1))
        self.assertNotIn("base_sha", state)
        self.assertNotIn("workflow_ref", state)

    def test_already_bound_prefix_tolerant(self):
        self.assertTrue(stamp.already_bound("17bacec", "17bacecea830"))
        self.assertFalse(stamp.already_bound("85e78ff", "17bacecea830"))


class PriorFindingsTest(unittest.TestCase):
    def test_collects_bot_findings_only(self):
        threads = [
            _thread("🟠 Bug: nil deref in parse"),
            _thread("🟡 Suggestion: rename this", path="pkg/bar.go"),
            _thread("looks like a finding but is human", author="octocat"),
            _thread("a bot comment without the finding prefix"),
        ]
        findings = rot.collect_prior_findings(threads)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["severity"], "suggestion")  # pkg/bar.go sorts first
        self.assertEqual(findings[1]["severity"], "bug")

    def test_resolved_threads_included_and_sorted_last(self):
        threads = [
            _thread("🟠 Bug: resolved one", resolved=True),
            _thread("🟠 Bug: open one", path="pkg/zzz.go"),
        ]
        findings = rot.collect_prior_findings(threads)
        self.assertEqual(len(findings), 2)
        self.assertFalse(findings[0]["thread_resolved"])
        self.assertTrue(findings[1]["thread_resolved"])

    def test_outdated_state_preserved(self):
        findings = rot.collect_prior_findings([_thread("🟠 Bug: x", outdated=True)])
        self.assertTrue(findings[0]["thread_outdated"])

    def test_severity_mapping(self):
        self.assertEqual(rot.severity_of("🔴 Security: s"), "security")
        self.assertEqual(rot.severity_of("🟠 Bug: b"), "bug")
        self.assertEqual(rot.severity_of("🟡 Suggestion: s"), "suggestion")
        self.assertEqual(rot.severity_of("other"), "unknown")


class ResolveThreadTest(unittest.TestCase):
    def _error(self, stderr: str) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(1, ["gh"], stderr=stderr)

    def test_permission_denial_flagged(self):
        with mock.patch.object(
            rot, "gh_graphql", side_effect=self._error("gh: Resource not accessible by integration")
        ):
            ok, blocked = rot.resolve_thread("PRRT_x")
        self.assertFalse(ok)
        self.assertTrue(blocked)

    def test_other_failure_not_flagged(self):
        with mock.patch.object(
            rot, "gh_graphql", side_effect=self._error("HTTP 502: bad gateway")
        ):
            ok, blocked = rot.resolve_thread("PRRT_x")
        self.assertFalse(ok)
        self.assertFalse(blocked)

    def test_success(self):
        with mock.patch.object(rot, "gh_graphql", return_value={}):
            ok, blocked = rot.resolve_thread("PRRT_x")
        self.assertTrue(ok)
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
