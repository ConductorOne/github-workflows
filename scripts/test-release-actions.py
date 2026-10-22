#!/usr/bin/env python3
"""Offline positive and adverse controls for privileged action policy."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load_module("check-release-actions")


class ReleaseActionsTest(unittest.TestCase):
    def test_repository_policy(self):
        self.assertEqual(policy.check_repository(ROOT), [])

    def test_each_guarded_job_rejects_unreviewed_action_and_mutable_pin(self):
        workflow = policy.load_yaml(ROOT / ".github/workflows/release.yaml")
        for job in policy.PRIVILEGED_JOBS:
            for action in ("unreviewed/action@" + "a" * 40, "actions/checkout@v5",
                           "./_workflows/.github/actions/unreviewed", "docker://unreviewed:latest"):
                with self.subTest(job=job, action=action):
                    changed = copy.deepcopy(workflow)
                    changed["jobs"][job]["steps"].append({"uses": action})
                    self.assertTrue(policy.check_workflow(changed))

    def test_new_credential_jobs_cannot_escape_inventory(self):
        for job in ({"permissions": {"id-token": "write"}},
                    {"env": {"TOKEN": "${{ secrets.NEW_TOKEN }}"}},
                    {"env": {"TOKEN": "${{ secrets['NEW_TOKEN'] }}"}},
                    {"env": {"TOKEN": "${{ secrets\n ['NEW_TOKEN'] }}"}},
                    {"env": {"TOKEN": "${{ toJSON(secrets) }}"}},
                    {"permissions": "write-all"}):
            job["steps"] = [{"uses": "unreviewed/action@" + "a" * 40}]
            self.assertTrue(policy.check_workflow({"jobs": {"new-job": job}}))
        self.assertTrue(policy.check_workflow({"permissions": {"id-token": "write"},
                                              "jobs": {"new-job": {"steps": job["steps"]}}}))
        self.assertTrue(policy.check_workflow({"env": {"TOKEN": "${{ secrets.RELEASE_TOKEN }}"},
                                              "jobs": {"new-job": {"steps": job["steps"]}}}))
        self.assertTrue(policy.check_workflow({"jobs": {"new-job": {"secrets": "inherit",
                                                                  "uses": "unreviewed/workflow@v1"}}}))

    def test_local_actions_require_immutable_checkout(self):
        steps = policy.load_yaml(ROOT / ".github/workflows/release.yaml")["jobs"]["validate-inputs"]["steps"][:2]
        self.assertEqual(policy.check_local_bootstrap(steps, "fixture"), [])
        for field, value in (("ref", "main"), ("persist-credentials", True), ("token", "secret")):
            changed = copy.deepcopy(steps)
            changed[0]["with"][field] = value
            self.assertTrue(policy.check_local_bootstrap(changed, "fixture"))
        self.assertTrue(policy.check_local_bootstrap(list(reversed(steps)), "fixture"))

    def test_first_party_actions_require_sha(self):
        for name in ("checkout", "github-script", "upload-artifact", "download-artifact",
                     "setup-go", "cache", "cache/restore", "cache/save", "create-github-app-token"):
            self.assertEqual(policy.check_steps([{"uses": f"actions/{name}@" + "a" * 40}], "first-party"), [])
            self.assertTrue(policy.check_steps([{"uses": f"actions/{name}@v2"}], "first-party"))
        self.assertTrue(policy.check_steps([{"uses": "actions-impostor/checkout@" + "a" * 40}], "third-party"))


    def test_failure_reporting_preserves_fields_and_tolerates_api_failure(self):
        workflow = policy.load_yaml(ROOT / ".github/workflows/release.yaml")
        step = workflow["jobs"]["notify-release-failure"]["steps"][-1]
        self.assertTrue(step["continue-on-error"])
        for status in (0, 22):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                fake = directory / "curl"
                fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RUNNER_TEMP/calls"\nexit ' + str(status) + '\n')
                fake.chmod(0o755)
                environment = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}",
                                   RUNNER_TEMP=str(directory), DATADOG_API_KEY="fixture-key",
                                   REPOSITORY="ConductorOne/example", REPOSITORY_NAME="example",
                                   REPOSITORY_OWNER="ConductorOne", RUN_ID="42", RELEASE_TAG="v1.2.3",
                                   RUN_URL="https://github.com/ConductorOne/example/actions/runs/42",
                                   ANNOTATIONS='failure with "quotes"\nand newline')
                subprocess.run(["bash", "-c", step["run"]], env=environment, check=True,
                               capture_output=True, text=True)
                event = json.loads((directory / "release-failure-event.json").read_text())
                self.assertEqual(event["title"], "Baton Connector Release Failed")
                self.assertEqual(event["alert_type"], "error")
                self.assertEqual(event["host"], "ConductorOne")
                self.assertEqual(event["tags"], ["github_repository:ConductorOne/example",
                                                "github_release_tag:v1.2.3", "github_workflow:baton-connector-release"])
                self.assertIn(environment["ANNOTATIONS"], event["text"])
                self.assertEqual(len((directory / "calls").read_text().splitlines()), 2)
                metric = json.loads((directory / "release-failure-metric.json").read_text())["series"][0]
                self.assertEqual(metric["metric"], "baton.connector.release.failure")
                self.assertEqual(metric["points"][0][1], 1)


if __name__ == "__main__":
    unittest.main()
