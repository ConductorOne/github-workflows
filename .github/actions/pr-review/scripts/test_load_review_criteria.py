#!/usr/bin/env python3

import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock


_SCRIPT = os.path.join(os.path.dirname(__file__), "load-review-criteria.py")
_spec = importlib.util.spec_from_file_location("load_review_criteria", _SCRIPT)
lrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lrc)


def payload_for(text):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


class ValidateCriteriaTest(unittest.TestCase):
    def test_accepts_plain_markdown(self):
        self.assertIsNone(
            lrc.validate_criteria(
                "<!-- managed -->\n\n## Review Checks\n\n- Check pagination.\n"
            )
        )

    def test_rejects_frontmatter(self):
        reason = lrc.validate_criteria("---\nallowed-tools: Read\n---\n# Criteria\n")
        self.assertIn("frontmatter", reason)

    def test_rejects_executable_markdown_keys(self):
        for key in ("allowed-tools", "hooks"):
            reason = lrc.validate_criteria(f"## Criteria\n\n{key}: value\n")
            self.assertIn("not allowed", reason)

    def test_accepts_context_and_agent_as_plain_labels(self):
        self.assertIsNone(
            lrc.validate_criteria(
                "## Criteria\n\nContext: verify pagination.\n\n- agent: confirm auth flows.\n"
            )
        )

    def test_rejects_shell_directives(self):
        reason = lrc.validate_criteria("## Criteria\n\n! gh pr diff\n")
        self.assertIn("shell !", reason)

    def test_rejects_non_plain_markdown_shapes(self):
        for text in (
            "![diagram](https://example.com/image.png)\n",
            "<script>alert(1)</script>\n",
            "::set-output name=x::y\n",
        ):
            reason = lrc.validate_criteria(text)
            self.assertIn("not allowed", reason)


class LoadCriteriaTest(unittest.TestCase):
    def write_context(
        self,
        directory,
        base_sha="abcdef1234567890",
        base_ref="main",
        base_default_branch="main",
    ):
        path = os.path.join(directory, "context.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "repository": "ConductorOne/example",
                    "current_base_sha": base_sha,
                    "current_base_ref": base_ref,
                    "base_default_branch": base_default_branch,
                },
                f,
            )
        return path

    def test_loaded_criteria_renders_prompt_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = self.write_context(tmpdir)
            with mock.patch.object(
                lrc,
                "gh_api_json",
                return_value=payload_for("## Extra Criteria\n\n- Check auth.\n"),
            ):
                result = lrc.load_result(context_path, ".claude/skills/ci-review.md")

            section = lrc.render_prompt_section(result)

        self.assertEqual(result.status, "loaded")
        self.assertIn("trusted base `abcdef123456`", result.message)
        self.assertIn("Criteria status: loaded", section)
        self.assertIn("## Extra Criteria", section)
        self.assertIn("not a Claude skill", section)

    def test_missing_criteria_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = self.write_context(tmpdir)
            error = subprocess.CalledProcessError(
                1,
                ["gh"],
                stderr="Not Found",
            )
            with mock.patch.object(lrc, "gh_api_json", side_effect=error):
                result = lrc.load_result(context_path, ".claude/skills/ci-review.md")

        self.assertEqual(result.status, "missing")
        self.assertIn("none loaded", result.message)

    def test_non_default_base_ref_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = self.write_context(
                tmpdir,
                base_ref="review-config-experiment",
                base_default_branch="main",
            )
            with mock.patch.object(lrc, "gh_api_json") as gh_api_json:
                result = lrc.load_result(context_path, ".claude/skills/ci-review.md")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("is not the default branch", result.message)
        gh_api_json.assert_not_called()

    def test_invalid_criteria_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = self.write_context(tmpdir)
            with mock.patch.object(
                lrc,
                "gh_api_json",
                return_value=payload_for("---\nagent: review\n---\n"),
            ):
                result = lrc.load_result(context_path, ".claude/skills/ci-review.md")

        self.assertEqual(result.status, "invalid")
        self.assertIn("was invalid", result.message)
        self.assertIsNone(result.content)

    def test_malformed_fetch_output_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = self.write_context(tmpdir)
            with mock.patch.object(
                lrc,
                "gh_api_json",
                side_effect=json.JSONDecodeError("bad", "not-json", 0),
            ):
                result = lrc.load_result(context_path, ".claude/skills/ci-review.md")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("could not read", result.message)

    def test_writes_prompt_and_status_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = lrc.CriteriaResult(
                status="missing",
                message="none loaded - missing",
                criteria_path=".claude/skills/ci-review.md",
                base_sha="abcdef1234567890",
            )
            prompt_path = os.path.join(tmpdir, ".github", "review-criteria.md")
            status_path = os.path.join(tmpdir, ".github", "review-criteria.json")

            lrc.write_outputs(result, prompt_path, status_path)

            with open(prompt_path) as f:
                prompt = f.read()
            with open(status_path) as f:
                status = json.load(f)

        self.assertIn("Criteria status: none loaded - missing.", prompt)
        self.assertEqual(status["status"], "missing")


if __name__ == "__main__":
    unittest.main()
