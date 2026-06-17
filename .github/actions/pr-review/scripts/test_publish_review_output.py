#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import unittest
from unittest import mock


_SCRIPT = os.path.join(os.path.dirname(__file__), "publish-review-output.py")
_spec = importlib.util.spec_from_file_location("publish_review_output", _SCRIPT)
pro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pro)


class PublishReviewOutputTest(unittest.TestCase):
    def test_rejects_unsafe_paths(self):
        with self.assertRaises(ValueError):
            pro.validated_output(
                {
                    "review_summary": "Reviewed the full diff.",
                    "security_issues": [
                        {
                            "path": "../secret",
                            "line": 1,
                            "confidence": "high",
                            "summary": "Bad path.",
                            "details": "Bad path.",
                        }
                    ],
                    "correctness_issues": [],
                    "suggestions": [],
                }
            )

    def test_renders_summary_with_review_state(self):
        context = {
            "repository": "ConductorOne/example",
            "pr_number": 42,
            "pr_title": "Example change",
            "current_sha": "head-sha",
            "current_base_sha": "base-sha",
            "workflow_ref": "ConductorOne/github-workflows/.github/workflows/pr-review.yaml@main",
            "summary_heading": "### Connector PR Review:",
            "review_mode": "full",
        }
        output = {
            "review_summary": "Reviewed the full diff for security and correctness.",
            "security_issues": [],
            "correctness_issues": [],
            "suggestions": [],
        }
        with mock.patch.object(pro, "criteria_status", return_value="loaded `ci-review.md`"):
            with mock.patch.object(pro, "resolved_count", return_value=2):
                body = pro.render_summary(context, output)

        self.assertIn("### Connector PR Review: Example change", body)
        self.assertIn("**Criteria:** Criteria status: loaded `ci-review.md`.", body)
        self.assertIn('"last_reviewed_sha": "head-sha"', body)
        self.assertIn("**Threads Resolved: 2**", body)

    def test_post_verdict_never_approves(self):
        context = {
            "repository": "ConductorOne/example",
            "pr_number": 42,
        }
        with mock.patch.object(pro, "gh") as gh:
            pro.post_verdict(context, has_blockers=False)

        command = gh.call_args.args[0]
        self.assertIn("--comment", command)
        self.assertNotIn("--approve", command)

    def test_verify_head_stops_before_posting_when_stale(self):
        context = {
            "repository": "ConductorOne/example",
            "pr_number": 42,
            "current_sha": "old-sha",
        }
        result = subprocess.CompletedProcess(
            ["gh"],
            0,
            stdout=json.dumps({"head": {"sha": "new-sha"}}),
            stderr="",
        )
        with mock.patch.object(pro, "gh", return_value=result):
            with self.assertRaises(SystemExit) as exit_info:
                pro.verify_head(context)

        self.assertEqual(exit_info.exception.code, 3)

    def test_post_inline_comment_uses_fixed_pull_comment_endpoint(self):
        context = {
            "repository": "ConductorOne/example",
            "pr_number": 42,
            "current_sha": "head-sha",
        }
        entry = {
            "path": "internal/foo.go",
            "line": 7,
            "confidence": "high",
            "summary": "Summary.",
            "details": "Details.",
        }
        with mock.patch.object(pro, "gh") as gh:
            pro.post_inline_comment(context, entry, "🟠 Bug:")

        command = gh.call_args.args[0]
        self.assertEqual(
            command[:2],
            ["api", "repos/ConductorOne/example/pulls/42/comments"],
        )
        self.assertIn("-f", command)
        self.assertIn("commit_id=head-sha", command)
        self.assertIn("path=internal/foo.go", command)
        self.assertIn("side=RIGHT", command)

    def test_load_review_output_requires_json_object(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_OUTPUT": json.dumps([])}):
            with self.assertRaises(SystemExit) as exit_info:
                pro.load_review_output()

        self.assertEqual(exit_info.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
