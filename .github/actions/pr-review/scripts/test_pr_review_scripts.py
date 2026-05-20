#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


SCRIPT_DIR = pathlib.Path(__file__).parent


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fetch_context = load_script("fetch_pr_context", "fetch-pr-context.py")
finalize_summary = load_script("finalize_review_summary", "finalize-review-summary.py")


class FetchPrContextTests(unittest.TestCase):
    def test_review_comment_heading_uses_configured_heading(self):
        comment = {
            "user": "github-actions[bot]",
            "body": "### General PR Review: example\n\nBody",
        }

        self.assertEqual(
            fetch_context.review_comment_heading(comment, "### General PR Review:"),
            "### General PR Review:",
        )
        self.assertTrue(
            fetch_context.is_bot_review_comment(comment, "### General PR Review:")
        )

    def test_parse_owned_review_state_requires_workflow_ref_and_shas(self):
        body = (
            "### Connector PR Review: example\n"
            "<!-- review-state: {"
            '"last_reviewed_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"workflow_ref":"owner/repo/.github/workflows/review.yaml@refs/heads/main",'
            '"run_id":"123"'
            "} -->"
        )

        state = fetch_context.parse_owned_review_state(
            body,
            "owner/repo/.github/workflows/review.yaml@refs/heads/main",
        )

        self.assertEqual(
            state["last_reviewed_sha"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(state["run_id"], "123")

    def test_parse_owned_review_state_rejects_wrong_workflow_ref(self):
        body = (
            "<!-- review-state: {"
            '"last_reviewed_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"workflow_ref":"other"'
            "} -->"
        )

        state = fetch_context.parse_owned_review_state(body, "expected")

        self.assertIsNone(state)

    def test_parse_owned_review_state_rejects_bad_sha(self):
        body = (
            "<!-- review-state: {"
            '"last_reviewed_sha":"not-a-sha",'
            '"base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"workflow_ref":"expected"'
            "} -->"
        )

        state = fetch_context.parse_owned_review_state(body, "expected")

        self.assertIsNone(state)

    def test_parse_owned_review_state_requires_run_id(self):
        body = (
            "<!-- review-state: {"
            '"last_reviewed_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"workflow_ref":"expected"'
            "} -->"
        )

        state = fetch_context.parse_owned_review_state(body, "expected")
        legacy_state = fetch_context.parse_legacy_owned_review_state(body, "expected")

        self.assertIsNone(state)
        self.assertEqual(
            legacy_state["last_reviewed_sha"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_build_interdiff_normalizes_index_and_hunk_line_numbers(self):
        previous = "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "index 1111111..2222222 100644",
                "@@ -1,3 +1,3 @@",
                " unchanged",
                "-old",
                "+new",
            ]
        )
        current = "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "index 3333333..4444444 100644",
                "@@ -8,3 +8,3 @@",
                " unchanged",
                "-old",
                "+new",
            ]
        )

        interdiff = fetch_context.build_interdiff(previous, current)

        self.assertIsNone(interdiff)

    def test_build_interdiff_reports_real_changes(self):
        previous = "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
            ]
        )
        current = "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+newer",
            ]
        )

        interdiff = fetch_context.build_interdiff(previous, current)

        self.assertIsNotNone(interdiff)
        self.assertIn("+newer", interdiff)

    def test_build_interdiff_preserves_binary_index_changes(self):
        previous = "\n".join(
            [
                "diff --git a/image.png b/image.png",
                "index 1111111..2222222 100644",
                "Binary files a/image.png and b/image.png differ",
            ]
        )
        current = "\n".join(
            [
                "diff --git a/image.png b/image.png",
                "index 1111111..3333333 100644",
                "Binary files a/image.png and b/image.png differ",
            ]
        )

        interdiff = fetch_context.build_interdiff(previous, current)

        self.assertIsNotNone(interdiff)
        self.assertIn("3333333", interdiff)

    def test_prepare_effective_interdiff_rejects_cross_repo(self):
        result = fetch_context.prepare_effective_interdiff(
            "org/base",
            "fork/head",
            "b" * 40,
            "a" * 40,
            "d" * 40,
            "c" * 40,
        )

        self.assertEqual(result["review_mode"], "full")
        self.assertEqual(
            result["review_mode_reason"]["code"],
            "interdiff_head_repo_differs",
        )
        self.assertTrue(result["clear_last_reviewed_sha"])


class FinalizeReviewSummaryTests(unittest.TestCase):
    def test_marker_for_uses_expected_state(self):
        context = {
            "current_sha": "a" * 40,
            "current_base_sha": "b" * 40,
            "workflow_ref": "workflow",
            "run_id": "123",
        }

        marker = finalize_summary.marker_for(context)

        self.assertIn('"last_reviewed_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', marker)
        self.assertIn('"base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"', marker)
        self.assertIn('"workflow_ref": "workflow"', marker)
        self.assertIn('"run_id": "123"', marker)

    def test_strip_review_state_removes_existing_marker(self):
        body = (
            "### Connector PR Review: example\n\n"
            "Body\n\n"
            "<!-- review-state: {\"last_reviewed_sha\":\"bad\"} -->\n"
        )

        stripped = finalize_summary.strip_review_state(body)

        self.assertNotIn("review-state", stripped)
        self.assertIn("Body", stripped)

    def test_summary_body_from_structured_output_uses_configured_heading(self):
        context = {"summary_heading": "### General PR Review:"}
        output = json.dumps({
            "summary_body": (
                "### General PR Review: example\n\n"
                "Body\n\n"
                "<!-- review-state: {\"last_reviewed_sha\":\"bad\"} -->\n"
            )
        })

        body = finalize_summary.summary_body_from_structured_output(output, context)

        self.assertTrue(body.startswith("### General PR Review:"))
        self.assertNotIn("review-state", body)

    def test_summary_body_from_structured_output_rejects_wrong_heading(self):
        context = {"summary_heading": "### General PR Review:"}
        output = json.dumps({
            "summary_body": "### Connector PR Review: example\n\nBody",
        })

        with self.assertRaises(ValueError):
            finalize_summary.summary_body_from_structured_output(output, context)

    def test_ensure_review_run_link_inserts_missing_link(self):
        context = {"review_run_url": "https://github.com/org/repo/actions/runs/123"}
        body = "\n".join([
            "### Connector PR Review: example",
            "",
            "**Blocking Issues: 0** | **Suggestions: 0** | **Threads Resolved: 0**",
            "_Review mode: full_",
            "",
            "### Review Summary",
            "No issues found.",
        ])

        linked = finalize_summary.ensure_review_run_link(body, context)

        self.assertIn("[View review run](https://github.com/org/repo/actions/runs/123)", linked)

    def test_current_run_candidate_matches_review_run_url(self):
        context = {
            "review_run_url": "https://github.com/org/repo/actions/runs/123",
            "current_sha": "a" * 40,
            "current_base_sha": "b" * 40,
            "workflow_ref": "workflow",
            "run_id": "123",
            "summary_heading": "### Connector PR Review:",
        }
        comment = {
            "id": 1,
            "user": {"login": "github-actions[bot]"},
            "body": (
                "### Connector PR Review: example\n"
                "[View review run](https://github.com/org/repo/actions/runs/123)\n"
            ),
        }

        self.assertTrue(finalize_summary.current_run_candidate(comment, context))

    def test_current_run_candidate_rejects_human_comment(self):
        context = {
            "review_run_url": "https://github.com/org/repo/actions/runs/123",
            "current_sha": "a" * 40,
            "current_base_sha": "b" * 40,
            "workflow_ref": "workflow",
            "run_id": "123",
            "summary_heading": "### Connector PR Review:",
        }
        comment = {
            "id": 1,
            "user": {"login": "octocat"},
            "body": (
                "### Connector PR Review: example\n"
                "[View review run](https://github.com/org/repo/actions/runs/123)\n"
            ),
        }

        self.assertFalse(finalize_summary.current_run_candidate(comment, context))

    def test_comment_order_key_uses_id_tie_breaker(self):
        older = {
            "id": 1,
            "created_at": "2026-05-20T10:00:00Z",
            "updated_at": "2026-05-20T10:00:00Z",
        }
        newer = {
            "id": 2,
            "created_at": "2026-05-20T10:00:00Z",
            "updated_at": "2026-05-20T10:00:00Z",
        }

        self.assertGreater(
            finalize_summary.comment_order_key(newer),
            finalize_summary.comment_order_key(older),
        )

    def test_owned_stale_summary_requires_matching_workflow_ref(self):
        context = {
            "workflow_ref": "workflow",
            "summary_heading": "### Connector PR Review:",
        }
        comment = {
            "id": 1,
            "user": {"login": "github-actions[bot]"},
            "body": (
                "### Connector PR Review: example\n"
                "<!-- review-state: {"
                '"last_reviewed_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                '"base_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
                '"workflow_ref":"other",'
                '"run_id":"123"'
                "} -->"
            ),
        }

        self.assertFalse(finalize_summary.owned_stale_summary(comment, context))

    def test_owned_stale_summary_preserves_markerless_legacy_comment(self):
        context = {
            "workflow_ref": "workflow",
            "summary_heading": "### Connector PR Review:",
        }
        comment = {
            "id": 1,
            "user": {"login": "github-actions[bot]"},
            "body": "### PR Review: example\n\nOld markerless summary",
        }

        self.assertFalse(finalize_summary.owned_stale_summary(comment, context))


if __name__ == "__main__":
    unittest.main()
