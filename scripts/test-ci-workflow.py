#!/usr/bin/env python3
"""Keep repository checks on repository events, not reusable connector calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(relative_path):
    return json.loads(subprocess.check_output(["yq", ".", str(ROOT / relative_path)], text=True))


class RepositoryCiTest(unittest.TestCase):
    def test_repository_events_jobs_and_read_only_permissions(self):
        workflow = load_yaml(".github/workflows/ci.yaml")
        self.assertEqual(set(workflow["on"]), {"pull_request", "push"})
        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        expected_paths = {"cmd/**", ".github/actions/**", "actions/**", "scripts/**", "tools/**",
                          "go.*", "renovate.json", "templates/**", ".github/workflows/ci.yaml",
                          ".github/workflows/verify.yaml", "Makefile", ".gitattributes"}
        pull_paths = set(workflow["on"]["pull_request"]["paths"])
        self.assertTrue(expected_paths <= pull_paths)
        self.assertEqual(pull_paths, set(workflow["on"]["push"]["paths"]))
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", json.dumps(workflow))
        self.assertEqual(set(workflow["jobs"]), {"go-tests", "shell-tests", "shellcheck", "windows-installer-tests"})
        go_job = workflow["jobs"]["go-tests"]
        self.assertEqual(go_job["env"]["GOTOOLCHAIN"], "local")
        self.assertEqual([step["run"] for step in go_job["steps"] if "run" in step],
                         ["go vet ./...", "go test ./..."])
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                    if step["uses"].startswith("actions/checkout@"):
                        self.assertFalse(step["with"]["persist-credentials"])
        runs = [step.get("run", "") for step in workflow["jobs"]["shell-tests"]["steps"]]
        self.assertEqual(runs.count("make test-scripts"), 1)
        for test in ("test-deny-os.sh", "test-ca-trust-guard.sh", "test-tool-integrity.py", "test-check-image-locks.py"):
            self.assertIn(test, (ROOT / "Makefile").read_text())
            self.assertFalse(any(test in run for run in runs), "make target must not be duplicated inline")
        review = load_yaml(".github/workflows/general-pr-review.yaml")
        self.assertIn("image-locks", review["jobs"])
        self.assertIn("pull_request", review["on"])

    def test_windows_installer_fixture_runs_natively(self):
        job = load_yaml(".github/workflows/ci.yaml")["jobs"]["windows-installer-tests"]
        self.assertEqual(job["runs-on"], "windows-latest")
        self.assertEqual(job["permissions"], {"contents": "read"})
        fixture_steps = [step for step in job["steps"]
                         if "scripts/test-tool-integrity.py" in step.get("run", "")]
        self.assertEqual(len(fixture_steps), 1)
        self.assertEqual(fixture_steps[0]["shell"], "pwsh")
        self.assertEqual(fixture_steps[0]["run"],
                         "python scripts/test-tool-integrity.py IntegrityTests.test_windows_node_installer_fixture")

    def test_reusable_verify_contains_only_caller_jobs(self):
        workflow = load_yaml(".github/workflows/verify.yaml")
        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(set(workflow["jobs"]), {"lint", "test", "docs", "regression"})

    def test_shellcheck_inventory_includes_every_repository_shell_source(self):
        with tempfile.TemporaryDirectory(prefix="ci lint ") as directory:
            directory = Path(directory)
            recorded = directory / "args.json"
            fake_shellcheck = directory / "shellcheck"
            fake_shellcheck.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                "with open(os.environ['CI_LINT_ARGS'], 'w') as output:\n"
                "    json.dump(sys.argv[1:], output)\n"
            )
            fake_shellcheck.chmod(0o755)
            environment = dict(os.environ, SHELLCHECK=str(fake_shellcheck), CI_LINT_ARGS=str(recorded))
            subprocess.run(["bash", str(ROOT / "scripts/lint-shell.sh")], cwd=ROOT, env=environment,
                           check=True, capture_output=True, text=True)
            arguments = json.loads(recorded.read_text())
            self.assertEqual(arguments[:3], ["--shell=bash", "--external-sources", "--source-path=SCRIPTDIR"])
            tracked = subprocess.check_output(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.sh"], cwd=ROOT
            ).decode().rstrip("\0").split("\0")
            self.assertEqual(set(arguments[3:]), {str(ROOT / path) for path in tracked})
            self.assertIn(str(ROOT / ".github/actions/locked-tools/locked-tools.sh"), arguments)
            self.assertIn(str(ROOT / "scripts/ca-trust-guard.sh"), arguments)
            self.assertIn(str(ROOT / "scripts/check-image-locks.sh"), arguments)


if __name__ == "__main__":
    unittest.main()
