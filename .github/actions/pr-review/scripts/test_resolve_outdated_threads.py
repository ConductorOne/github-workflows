#!/usr/bin/env python3
"""Unit tests for reviewer-finding bookkeeping in resolve-outdated-threads.py.

The module file name contains hyphens, so it is loaded by path via importlib
rather than imported normally. Run with:

    python3 -m unittest discover -s .github/actions/pr-review/scripts -p 'test_*.py'

or directly:

    python3 .github/actions/pr-review/scripts/test_resolve_outdated_threads.py
"""

import importlib.util
import os
import unittest

_SCRIPT = os.path.join(os.path.dirname(__file__), "resolve-outdated-threads.py")
_spec = importlib.util.spec_from_file_location("resolve_outdated_threads", _SCRIPT)
rot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rot)


def thread(body="🟡 Suggestion: something", author="github-actions[bot]",
           replies=(), path="pkg/x.go", line=10, resolved=False, outdated=False):
    nodes = [{"body": body, "author": {"login": author}}]
    nodes.extend({"body": b, "author": {"login": a}} for a, b in replies)
    return {
        "id": f"T{path}{line}",
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "comments": {"totalCount": len(nodes), "nodes": nodes},
    }


class IsBotFindingThread(unittest.TestCase):
    def test_accepts_review_prefixed_bot_thread(self):
        self.assertTrue(rot.is_bot_finding_thread(thread()))

    def test_rejects_human_authored_thread(self):
        self.assertFalse(rot.is_bot_finding_thread(thread(author="felipe")))

    def test_rejects_bot_thread_without_review_prefix(self):
        self.assertFalse(rot.is_bot_finding_thread(thread(body="deploy preview ready")))

    def test_accepts_bot_thread_a_human_replied_to(self):
        # A human reply must not make the thread invisible: that is exactly the
        # thread we most need to avoid re-posting.
        self.assertTrue(
            rot.is_bot_finding_thread(thread(replies=[("felipe", "fixed, thanks")]))
        )

    def test_rejects_empty_thread(self):
        empty = thread()
        empty["comments"] = {"totalCount": 0, "nodes": []}
        self.assertFalse(rot.is_bot_finding_thread(empty))


class FindingDigest(unittest.TestCase):
    def test_carries_location_and_body(self):
        d = rot.finding_digest(thread(body="🔴 Security: leak", path="a/b.go", line=42))
        self.assertEqual(d["path"], "a/b.go")
        self.assertEqual(d["line"], 42)
        self.assertEqual(d["body"], "🔴 Security: leak")

    def test_truncates_long_bodies(self):
        d = rot.finding_digest(thread(body="🟠 Bug: " + "x" * 5000))
        self.assertEqual(len(d["body"]), rot.MAX_FINDING_BODY)

    def test_flags_human_reply(self):
        self.assertFalse(rot.finding_digest(thread())["has_human_reply"])
        self.assertTrue(
            rot.finding_digest(
                thread(replies=[("felipe", "disagree")])
            )["has_human_reply"]
        )

    def test_bot_reply_is_not_a_human_reply(self):
        self.assertFalse(
            rot.finding_digest(
                thread(replies=[("github-actions[bot]", "still open")])
            )["has_human_reply"]
        )

    def test_tolerates_missing_line(self):
        t = thread()
        del t["line"]
        self.assertIsNone(rot.finding_digest(t)["line"])


class ShouldResolveUnchanged(unittest.TestCase):
    """The dedup bookkeeping must not widen what gets auto-resolved."""

    def test_outdated_bot_thread_still_resolves(self):
        self.assertTrue(rot.should_resolve(thread(outdated=True)))

    def test_outdated_thread_with_human_reply_still_does_not_resolve(self):
        self.assertFalse(
            rot.should_resolve(thread(outdated=True, replies=[("felipe", "no")]))
        )

    def test_current_bot_thread_does_not_resolve(self):
        self.assertFalse(rot.should_resolve(thread(outdated=False)))


if __name__ == "__main__":
    unittest.main()
