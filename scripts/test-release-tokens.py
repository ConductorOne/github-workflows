#!/usr/bin/env python3
"""Check least-privilege token wiring without release credentials or network."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path):
    return json.loads(subprocess.check_output(["yq", ".", str(ROOT / path)], text=True))


def token_errors(workflow):
    errors = []
    if workflow["permissions"] != {}:
        errors.append("workflow-wide permissions must remain empty")
    residuals = []
    for name, job in workflow["jobs"].items():
        expected = "write" if name == "goreleaser-binaries" else "read"
        if job["permissions"].get("contents") != expected:
            errors.append(f"{name}: unexpected contents permission")
        for step in job.get("steps", []):
            if "secrets.RELENG_GITHUB_TOKEN" in json.dumps(step):
                residuals.append((name, step["name"]))
            if step.get("run", "").startswith("goreleaser release "):
                if step.get("env", {}).get("GITHUB_TOKEN") != "${{ github.token }}":
                    errors.append(f"{name}: GoReleaser must use the job token")
            if "gh api" in step.get("run", ""):
                if step.get("env", {}).get("GH_TOKEN") != "${{ github.token }}":
                    errors.append(f"{name}: metadata reads must use the job token")
    if residuals != [("goreleaser-binaries", "Publish Homebrew formula")]:
        errors.append("only the deferred Homebrew tap step may consume the legacy token")
    return errors


class ReleaseTokenTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load_yaml(".github/workflows/release.yaml")

    def test_release_job_permissions_and_tokens(self):
        self.assertEqual(token_errors(self.workflow), [])
        release_steps = [step for job in self.workflow["jobs"].values() for step in job.get("steps", [])
                         if step.get("run", "").startswith("goreleaser release ")]
        self.assertEqual(len(release_steps), 4)
        tap = next(step for step in self.workflow["jobs"]["goreleaser-binaries"]["steps"]
                   if step["name"] == "Publish Homebrew formula")
        self.assertEqual(tap["if"], "inputs.brew == true")
        self.assertEqual(tap["env"]["GITHUB_TOKEN"], "${{ secrets.RELENG_GITHUB_TOKEN }}")

    def test_only_binaries_publish_github_release(self):
        binaries = load_yaml("templates/.goreleaser-binaries-template.yaml.tmpl")
        self.assertNotEqual(binaries["release"].get("disable"), True)
        self.assertEqual(binaries["brews"][0]["skip_upload"], "true")
        for template in ("windows", "docker-oci", "docker-lambda"):
            config = load_yaml(f"templates/.goreleaser-{template}-template.yaml.tmpl")
            self.assertTrue(config["release"]["disable"])

    def test_read_only_publisher_negative_control(self):
        changed = copy.deepcopy(self.workflow)
        changed["jobs"]["goreleaser-binaries"]["permissions"]["contents"] = "read"
        self.assertIn("goreleaser-binaries: unexpected contents permission", token_errors(changed))

    def test_no_broader_grants_negative_controls(self):
        for name in self.workflow["jobs"]:
            if name == "goreleaser-binaries":
                continue
            changed = copy.deepcopy(self.workflow)
            changed["jobs"][name]["permissions"]["contents"] = "write"
            self.assertTrue(token_errors(changed), name)
        changed = copy.deepcopy(self.workflow)
        changed["permissions"] = {"contents": "write"}
        self.assertTrue(token_errors(changed))

    def test_legacy_release_token_negative_control(self):
        for name, job in self.workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", [])):
                if not step.get("run", "").startswith("goreleaser release "):
                    continue
                changed = copy.deepcopy(self.workflow)
                changed["jobs"][name]["steps"][index]["env"]["GITHUB_TOKEN"] = "${{ secrets.RELENG_GITHUB_TOKEN }}"
                self.assertTrue(token_errors(changed), name)


if __name__ == "__main__":
    unittest.main()
