#!/usr/bin/env python3
"""Unit tests for the incremental-diff hardening in fetch-pr-context.py.

The module file name contains hyphens, so it is loaded by path via importlib
rather than imported normally. Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_fetch_pr_context.py
"""

import importlib.util
import os
import unittest

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


class FilterAndDecodeDiffTest(unittest.TestCase):
    def test_drops_vendor_keeps_go_without_raising(self):
        # Must not raise UnicodeDecodeError despite the 0xca bytes.
        text, meta = fpc.filter_and_decode_diff(VENDOR_SECTION + GO_SECTION)
        self.assertNotIn("vendor/example.com/pkg/secret.go", text)
        self.assertIn("internal/foo.go", text)
        self.assertIn("func New() {}", text)
        self.assertEqual(meta["dropped_sections"], 1)
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
        finally:
            fpc.DIFF_MAX_BYTES = original_cap

    def test_all_excluded_returns_empty_for_full_fallback(self):
        # When nothing reviewable remains, the text is empty so the caller can
        # fall back to full review mode.
        text, meta = fpc.filter_and_decode_diff(VENDOR_SECTION)
        self.assertEqual(text.strip(), "")
        self.assertEqual(meta["dropped_sections"], 1)

    def test_excluded_suffixes(self):
        for path in (
            "go.sum",
            "go.mod",
            "api/foo.pb.go",
            "internal/mock_gen.go",
            "package-lock.json",
            "yarn.lock",
        ):
            self.assertTrue(fpc._is_excluded(path), path)
        for path in ("internal/foo.go", "cmd/main.go", "README.md"):
            self.assertFalse(fpc._is_excluded(path), path)

    def test_diff_path_extraction(self):
        header = b"diff --git a/internal/foo.go b/internal/foo.go"
        self.assertEqual(fpc._diff_path(header), "internal/foo.go")


if __name__ == "__main__":
    unittest.main()
