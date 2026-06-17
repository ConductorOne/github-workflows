#!/usr/bin/env python3

import importlib.util
import os
import sys
import unittest
from unittest import mock


def load_script(name):
    path = os.path.join(os.path.dirname(__file__), name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


post_verdict = load_script("post-verdict.py")
post_inline_comment = load_script("post-inline-comment.py")


class PostVerdictTest(unittest.TestCase):
    def test_rejects_approve_verdict(self):
        with mock.patch.object(sys, "argv", ["post-verdict", "approve"]):
            with self.assertRaises(SystemExit) as exit_info:
                post_verdict.main()

        self.assertEqual(exit_info.exception.code, 2)

    def test_posts_only_request_changes_for_blocking_verdict(self):
        context = {
            "repository": "ConductorOne/example",
            "pr_number": 42,
            "current_sha": "head-sha",
        }
        with mock.patch.object(sys, "argv", ["post-verdict", "request-changes", "body"]):
            with mock.patch.object(post_verdict, "load_context", return_value=context):
                with mock.patch.object(post_verdict, "verify_head") as verify_head:
                    with mock.patch.object(post_verdict.subprocess, "run") as run:
                        post_verdict.main()

        verify_head.assert_called_once_with(context)
        command = run.call_args.args[0]
        self.assertIn("--request-changes", command)
        self.assertNotIn("--approve", command)


class PostInlineCommentTest(unittest.TestCase):
    def test_rejects_unsafe_paths(self):
        with mock.patch.object(
            sys,
            "argv",
            ["post-inline-comment", "../README.md", "1"],
        ):
            with self.assertRaises(SystemExit) as exit_info:
                post_inline_comment.main()

        self.assertEqual(exit_info.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
