#!/usr/bin/env python3
"""Offline positive and adverse controls for privileged action policy."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load_module("check-release-actions")
DATADOG_ACTION = "masci/datadog@93a16378298f4e1980f13f6b412c0b8e66b96185"
DATADOG_KEY = "${{ secrets.DATADOG_API_KEY }}"


def notification_errors(workflow):
    errors = []
    job = workflow["jobs"]["notify-release-failure"]
    notification = job["steps"][-1]
    if job.get("if") != "failure()" or job.get("timeout-minutes") != 5:
        errors.append("failure notification must remain conditional and bounded")
    if job.get("permissions") != {"checks": "read", "actions": "read", "contents": "read"}:
        errors.append("notification job must remain non-signing and read-only")
    if notification.get("uses") != DATADOG_ACTION or notification.get("continue-on-error") is not True:
        errors.append("notification action must remain pinned and best-effort")
    if notification.get("with", {}).get("api-key") != DATADOG_KEY:
        errors.append("the notification step must receive the key directly")
    if "DATADOG_API_KEY" in json.dumps(workflow.get("env", {})).upper():
        errors.append("the notification key must not be workflow-scoped")
    for name, candidate_job in workflow["jobs"].items():
        if "DATADOG_API_KEY" in json.dumps({key: value for key, value in candidate_job.items() if key != "steps"}).upper():
            errors.append("the notification key must not be job-scoped")
        for step in candidate_job.get("steps", []):
            remainder = copy.deepcopy(step)
            if name == "notify-release-failure" and step is notification:
                remainder.get("with", {}).pop("api-key", None)
            elif step.get("uses", "").startswith("masci/datadog@"):
                errors.append("the Datadog action is notification-only")
            if "DATADOG_API_KEY" in json.dumps(remainder).upper():
                errors.append("the notification key escaped its single action input")
    return errors


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


    def test_failure_reporting_restores_the_pinned_action_and_event(self):
        workflow = policy.load_yaml(ROOT / ".github/workflows/release.yaml")
        self.assertEqual(notification_errors(workflow), [])
        step = workflow["jobs"]["notify-release-failure"]["steps"][-1]
        self.assertEqual(set(step["with"]), {"api-key", "api-url", "events"})
        self.assertEqual(step["with"]["api-url"], "https://us3.datadoghq.com/")
        self.assertNotIn("run", step)
        self.assertNotIn("env", step)
        events = json.loads(subprocess.check_output(["yq", "."], input=step["with"]["events"], text=True))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(set(event), {"title", "text", "alert_type", "host", "tags"})
        self.assertEqual(event["title"], "Baton Connector Release Failed")
        self.assertEqual(event["alert_type"], "error")
        self.assertEqual(event["host"], "${{ github.repository_owner }}")
        self.assertEqual(event["tags"], [
            "github_repository:${{ github.event.repository.full_name }}",
            "github_release_tag:${{ inputs.tag }}", "github_workflow:baton-connector-release"])
        self.assertIn("${{ steps.collect-ann.outputs.annotations }}", event["text"])
        self.assertIn(DATADOG_ACTION, policy.ALLOWED_ACTIONS)
        self.assertNotIn("masci/datadog", policy.ALLOWED_ACTIONS)
        self.assertEqual(policy.check_steps([step], "notify-release-failure"), [])
        for ref in ("v1", "a" * 40):
            self.assertTrue(policy.check_steps([{"uses": f"masci/datadog@{ref}"}], "notify-release-failure"))

    def test_notification_custody_and_best_effort_negative_controls(self):
        workflow = policy.load_yaml(ROOT / ".github/workflows/release.yaml")
        changed = copy.deepcopy(workflow)
        changed["env"]["DD_KEY"] = DATADOG_KEY
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["notify-release-failure"]["env"] = {"DD_KEY": DATADOG_KEY}
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["goreleaser-binaries"]["steps"].append({"env": {"DD_KEY": DATADOG_KEY}, "run": "true"})
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["notify-release-failure"]["steps"][-1]["env"] = {"DD_KEY": DATADOG_KEY}
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["notify-release-failure"]["steps"][-1]["continue-on-error"] = False
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["notify-release-failure"]["timeout-minutes"] = 10
        self.assertTrue(notification_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["goreleaser-binaries"]["steps"].append({"uses": DATADOG_ACTION})
        self.assertTrue(notification_errors(changed))


if __name__ == "__main__":
    unittest.main()
