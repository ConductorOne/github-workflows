#!/usr/bin/env python3
"""Unit tests for the incremental-diff hardening in fetch-pr-context.py.

The module file name contains hyphens, so it is loaded by path via importlib
rather than imported normally. Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_fetch_pr_context.py
"""

import importlib.util
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

_SCRIPT = os.path.join(os.path.dirname(__file__), "fetch-pr-context.py")
_spec = importlib.util.spec_from_file_location("fetch_pr_context", _SCRIPT)
fpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpc)


# A vendored section that inlines a raw non-UTF-8 byte (0xca), exactly the shape
# that crashes a strict UTF-8 decode of the whole compare diff.
VENDOR_SECTION = (
    b"diff --git a/vendor/example.com/pkg/secret.go b/vendor/example.com/pkg/secret.go\n"
    b"index 1111111..2222222 100644\n"
    b"--- a/vendor/example.com/pkg/secret.go\n"
    b"+++ b/vendor/example.com/pkg/secret.go\n"
    b"@@ -1 +1 @@\n"
    b"-old\n"
    b"+\xca\xca\xca not-actually-utf8 payload\n"
)

GO_SECTION = (
    b"diff --git a/internal/foo.go b/internal/foo.go\n"
    b"index 3333333..4444444 100644\n"
    b"--- a/internal/foo.go\n"
    b"+++ b/internal/foo.go\n"
    b"@@ -1 +1 @@\n"
    b"-func old() {}\n"
    b"+func New() {}\n"
)


def rename_section(old_path, new_path):
    return (
        f"diff --git a/{old_path} b/{new_path}\n"
        "similarity index 88%\n"
        f"rename from {old_path}\n"
        f"rename to {new_path}\n"
        "@@ -1 +1 @@\n"
        "-package old\n"
        "+package new\n"
    ).encode("utf-8")


class FilterAndDecodeDiffTest(unittest.TestCase):
    def test_drops_vendor_keeps_go_without_raising(self):
        # Must not raise UnicodeDecodeError despite the 0xca bytes.
        text, meta = fpc.filter_and_decode_diff(VENDOR_SECTION + GO_SECTION)
        self.assertNotIn("vendor/example.com/pkg/secret.go", text)
        self.assertIn("internal/foo.go", text)
        self.assertIn("func New() {}", text)
        self.assertEqual(meta["dropped_sections"], 1)
        self.assertEqual(meta["dropped_paths"], ["vendor/example.com/pkg/secret.go"])
        self.assertFalse(meta["truncated"])
        self.assertGreater(meta["kept_bytes"], 0)

    def test_non_utf8_bytes_in_kept_section_are_lossless_not_dropped(self):
        # A retained (.go) section carrying a stray non-UTF-8 byte must decode
        # via backslashreplace rather than raise or be silently lost.
        go_with_bad_byte = (
            b"diff --git a/internal/bar.go b/internal/bar.go\n"
            b"@@ -1 +1 @@\n"
            b"+// \xca marker\n"
        )
        text, meta = fpc.filter_and_decode_diff(go_with_bad_byte)
        self.assertIn("internal/bar.go", text)
        self.assertIn("\\xca", text)
        self.assertEqual(meta["dropped_sections"], 0)
        self.assertEqual(meta["dropped_paths"], [])

    def test_truncation_marker_present_when_over_cap(self):
        original_cap = fpc.DIFF_MAX_BYTES
        fpc.DIFF_MAX_BYTES = len(GO_SECTION) + 10  # room for one section only
        try:
            second = GO_SECTION.replace(b"foo.go", b"baz.go")
            text, meta = fpc.filter_and_decode_diff(GO_SECTION + second)
            self.assertTrue(meta["truncated"])
            self.assertIn("diff truncated to", text)
            self.assertIn("internal/foo.go", text)
            self.assertNotIn("internal/baz.go", text)
            self.assertEqual(meta["dropped_paths"], [])
        finally:
            fpc.DIFF_MAX_BYTES = original_cap

    def test_marker_only_truncation_reports_zero_kept_bytes(self):
        original_cap = fpc.DIFF_MAX_BYTES
        fpc.DIFF_MAX_BYTES = 1
        try:
            text, meta = fpc.filter_and_decode_diff(GO_SECTION)
            self.assertTrue(meta["truncated"])
            self.assertEqual(meta["kept_bytes"], 0)
            self.assertEqual(meta["dropped_sections"], 0)
            self.assertEqual(meta["dropped_paths"], [])
            self.assertEqual(
                text.strip(),
                "[diff truncated to 1 bytes for review context]",
            )
        finally:
            fpc.DIFF_MAX_BYTES = original_cap

    def test_all_excluded_returns_empty_for_full_fallback(self):
        # When nothing reviewable remains, the text is empty so the caller can
        # fall back to full review mode.
        text, meta = fpc.filter_and_decode_diff(VENDOR_SECTION)
        self.assertEqual(text.strip(), "")
        self.assertEqual(meta["dropped_sections"], 1)
        self.assertEqual(meta["dropped_paths"], ["vendor/example.com/pkg/secret.go"])

    def test_rename_from_vendor_to_internal_is_kept(self):
        diff = rename_section(
            "vendor/example.com/pkg/secret.go",
            "internal/secret.go",
        )

        text, meta = fpc.filter_and_decode_diff(diff)

        self.assertIn("rename from vendor/example.com/pkg/secret.go", text)
        self.assertIn("rename to internal/secret.go", text)
        self.assertEqual(meta["dropped_sections"], 0)
        self.assertEqual(meta["dropped_paths"], [])

    def test_rename_from_internal_to_vendor_is_kept(self):
        diff = rename_section(
            "internal/secret.go",
            "vendor/example.com/pkg/secret.go",
        )

        text, meta = fpc.filter_and_decode_diff(diff)

        self.assertIn("rename from internal/secret.go", text)
        self.assertIn("rename to vendor/example.com/pkg/secret.go", text)
        self.assertEqual(meta["dropped_sections"], 0)
        self.assertEqual(meta["dropped_paths"], [])

    def test_excluded_suffixes(self):
        for path in (
            "api/foo.pb.go",
            "internal/mock_gen.go",
            "package-lock.json",
            "yarn.lock",
        ):
            self.assertTrue(fpc._is_excluded(path), path)
        for path in ("go.sum", "go.mod", "internal/foo.go", "cmd/main.go", "README.md"):
            self.assertFalse(fpc._is_excluded(path), path)

    def test_diff_path_extraction(self):
        header = b"diff --git a/internal/foo.go b/internal/foo.go"
        self.assertEqual(fpc._diff_path(header), "internal/foo.go")

    def test_diff_path_extraction_with_spaces(self):
        header = b"diff --git a/internal/space name.go b/internal/space name.go"
        self.assertEqual(fpc._diff_path(header), "internal/space name.go")


class FetchCompareDiffTest(unittest.TestCase):
    def test_mixed_excluded_and_kept_diff_returns_metadata(self):
        with (
            mock.patch.object(
                fpc,
                "gh_api",
                return_value=SimpleNamespace(stdout='{"status":"ahead"}'),
            ),
            mock.patch.object(
                fpc,
                "gh_api_bytes",
                return_value=VENDOR_SECTION + GO_SECTION,
            ),
        ):
            text, meta = fpc.fetch_compare_diff("owner/repo", "base", "head")

        self.assertIsNotNone(text)
        self.assertIn("[incremental diff partial coverage]", text)
        self.assertIn("internal/foo.go", text)
        self.assertIn("vendor/example.com/pkg/secret.go", text)
        self.assertNotIn("not-actually-utf8 payload", text)
        self.assertEqual(meta["dropped_sections"], 1)
        self.assertEqual(meta["dropped_paths"], ["vendor/example.com/pkg/secret.go"])
        self.assertTrue(meta["partial"])

    def test_marker_only_truncation_falls_back_to_full_mode(self):
        original_cap = fpc.DIFF_MAX_BYTES
        fpc.DIFF_MAX_BYTES = 1
        try:
            with (
                mock.patch.object(
                    fpc,
                    "gh_api",
                    return_value=SimpleNamespace(stdout='{"status":"ahead"}'),
                ),
                mock.patch.object(fpc, "gh_api_bytes", return_value=GO_SECTION),
            ):
                text, meta = fpc.fetch_compare_diff("owner/repo", "base", "head")
        finally:
            fpc.DIFF_MAX_BYTES = original_cap

        self.assertIsNone(text)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["kept_bytes"], 0)


class MainContextTest(unittest.TestCase):
    def test_incremental_diff_metadata_written_to_context(self):
        metadata = {
            "dropped_sections": 1,
            "dropped_paths": ["vendor/example.com/pkg/secret.go"],
            "dropped_paths_omitted": 0,
            "truncated": False,
            "kept_bytes": len(GO_SECTION),
            "partial": True,
        }
        workflow_ref = "ConductorOne/github-workflows/.github/workflows/pr-review.yaml@refs/heads/main"
        state = json.dumps(
            {
                "last_reviewed_sha": "old-sha",
                "base_sha": "base-sha",
                "workflow_ref": workflow_ref,
            }
        )
        raw_comments = [
            {
                "id": 123,
                "author_association": "MEMBER",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": f"{fpc.DEFAULT_REVIEW_SUMMARY_HEADING} Previous\n<!-- review-state: {state} -->",
            }
        ]
        pr = {
            "head": {
                "sha": "head-sha",
                "repo": {"full_name": "ConductorOne/example"},
            },
            "base": {"sha": "base-sha"},
        }

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "GITHUB_REPOSITORY": "ConductorOne/example",
                            "PR_NUMBER": "42",
                            "PR_HEAD_SHA": "head-sha",
                            "GITHUB_WORKFLOW_REF": workflow_ref,
                            "GITHUB_RUN_ID": "99",
                            "GITHUB_SERVER_URL": "https://github.com",
                        },
                        clear=False,
                    ),
                    mock.patch.object(fpc, "gh_api_paginate", return_value=raw_comments),
                    mock.patch.object(
                        fpc,
                        "gh_api",
                        return_value=SimpleNamespace(stdout=json.dumps(pr)),
                    ),
                    mock.patch.object(fpc, "current_checkout_sha", return_value="head-sha"),
                    mock.patch.object(
                        fpc,
                        "fetch_compare_diff",
                        return_value=("diff text", metadata),
                    ),
                ):
                    fpc.main()

                with open(".github/pr-context.json") as f:
                    context = json.load(f)
                self.assertEqual(context["review_mode"], "incremental")
                self.assertEqual(context["incremental_diff_path"], ".github/incremental.diff")
                self.assertEqual(context["incremental_diff_metadata"], metadata)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
