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

    def test_empty_or_non_mapping_inputs_fail_cleanly(self):
        steps = policy.load_yaml(ROOT / ".github/workflows/release.yaml")["jobs"]["validate-inputs"]["steps"][:2]
        for inputs in (None, [], ["path", "_workflows"], "", "not-a-mapping", False, 0):
            with self.subTest(inputs=inputs):
                changed = copy.deepcopy(steps)
                changed[0]["with"] = inputs
                self.assertTrue(policy.check_local_bootstrap(changed, "fixture"))

    def test_later_checkout_cannot_overwrite_verified_workflows(self):
        steps = policy.load_yaml(ROOT / ".github/workflows/release.yaml")["jobs"]["validate-inputs"]["steps"][:2]
        for path in (None, ".", "", "_workflows", "./_workflows", "_workflows/", "_workflows/.",
                     "_workflows/.github", "${{ github.workspace }}/_workflows", "..", "../caller",
                     "/tmp/workflows"):
            with self.subTest(path=path):
                overwrite = copy.deepcopy(steps[0])
                overwrite["with"].update(path=path, repository="unreviewed/workflows", ref="main")
                if path is None:
                    overwrite["with"].pop("path")
                self.assertTrue(policy.check_local_bootstrap([steps[0], overwrite, steps[1]], "fixture"))
        other_checkout = copy.deepcopy(overwrite)
        other_checkout["with"]["path"] = "_caller"
        self.assertEqual(policy.check_local_bootstrap([steps[0], other_checkout, steps[1]], "fixture"), [])
        self.assertEqual(policy.check_local_bootstrap([steps[0], steps[0], steps[1]], "fixture"), [])

    def test_first_party_actions_require_sha(self):
        for name in ("checkout", "github-script", "upload-artifact", "download-artifact",
                     "setup-go", "cache", "cache/restore", "cache/save", "create-github-app-token"):
            self.assertEqual(policy.check_steps([{"uses": f"actions/{name}@" + "a" * 40}], "first-party"), [])
            self.assertTrue(policy.check_steps([{"uses": f"actions/{name}@v2"}], "first-party"))
        self.assertTrue(policy.check_steps([{"uses": "actions-impostor/checkout@" + "a" * 40}], "third-party"))


    def test_failure_reporting_preserves_fields_and_tolerates_api_failure(self):
        workflow = policy.load_yaml(ROOT / ".github/workflows/release.yaml")
        step = workflow["jobs"]["notify-release-failure"]["steps"][-1]
        self.assertEqual(workflow["jobs"]["notify-release-failure"]["timeout-minutes"], 5)
        self.assertTrue(step["continue-on-error"])
        for status in (0, 22, 28):
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
                calls = (directory / "calls").read_text().splitlines()
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertIn("--connect-timeout 10 --max-time 30", call)
                metric = json.loads((directory / "release-failure-metric.json").read_text())["series"][0]
                self.assertEqual(metric["metric"], "baton.connector.release.failure")
                self.assertEqual(metric["points"][0][1], 1)


if __name__ == "__main__":
    unittest.main()
