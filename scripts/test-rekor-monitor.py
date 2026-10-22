#!/usr/bin/env python3
"""Synthetic fixtures only: no Rekor, GitHub or registry requests."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("monitor", ROOT / "scripts/rekor_digest_check.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
SAN = "https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@refs/tags/v4"
DIGEST = "a" * 64
NOW = 1700000000


def workflow_errors(workflow):
    errors = []
    if workflow["permissions"] != {"contents": "read", "actions": "read"}:
        errors.append("unexpected permissions")
    job = workflow["jobs"]["monitor"]
    if job["if"] != "github.ref == 'refs/heads/main'":
        errors.append("must run only on main")
    for step in job["steps"]:
        action = step.get("uses", "")
        if action and not action.startswith("./") and not re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", action):
            errors.append("mutable action")
    env = job["env"]
    if env["GOTOOLCHAIN"] != "local" or env["GOFLAGS"] != "-mod=readonly" or env["GOSUMDB"] != "sum.golang.org":
        errors.append("unprotected tool installation")
    if env["MONITOR_VERSION"] != "v0.0.0-20261019132539-47017b2379cb":
        errors.append("mutable monitor")
    return errors


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.entry = {"uuid": "fixture", "index": 42, "kind": "hashedrekord", "digest": DIGEST,
                      "integrated_time": NOW, "identity": {
                          "subjectAlternativeName": SAN, "issuer": monitor.ISSUER,
                          "sourceRepositoryURI": "https://github.com/ConductorOne/example",
                          "sourceRepositoryRef": "refs/tags/v1.2.3", "sourceRepositoryDigest": "c" * 40,
                          "sourceRepositoryVisibilityAtSigning": "public",
                          "runInvocationURI": "https://github.com/ConductorOne/example/actions/runs/123/attempts/1"}}
        self.run = {"repository": {"full_name": "ConductorOne/example"}, "head_sha": "c" * 40,
                    "status": "completed", "run_started_at": "2023-11-14T22:00:00Z",
                    "updated_at": "2023-11-14T22:20:00Z",
                    "referenced_workflows": [{"path": SAN.removeprefix("https://github.com/").replace("@refs/tags/v4", "@v4"), "ref": "refs/tags/v4"}]}
        self.manifest = {"org": "ConductorOne", "name": "example", "semver": "v1.2.3",
                         "assets": {"archive": {"sha256": DIGEST}}, "images": {}}
        self.index = {"semvers": {"v1.2.3": {"manifest": monitor.DIST + "/releases/ConductorOne/example/v1.2.3/manifest.json"}}}
        self.catalog = {"catalogs": {"ConductorOne/example": {"hidden": False}}}

    def get(self, url):
        if url.startswith(monitor.GITHUB):
            return json.dumps(self.run).encode()
        if url == monitor.DIST + "/index.json":
            return json.dumps(self.catalog).encode()
        if url.endswith("/example/index.json"):
            return json.dumps(self.index).encode()
        if url.endswith("/v1.2.3/manifest.json"):
            return json.dumps(self.manifest).encode()
        raise AssertionError("unexpected network boundary: " + url)

    def evaluate(self, entry=None, state=None, now=NOW):
        return monitor.evaluate(entry or self.entry, state if state is not None else {}, now, self.get)

    def test_match_and_replay_are_separate_verdicts(self):
        row = self.evaluate()
        self.assertEqual((row["status"], row["run_identity_match"], row["artifact_authorized"]),
                         ("MATCHED", "MATCH", "AUTHORIZED"))
        entry = dict(self.entry, digest="b" * 64)
        state = {"first_seen": {"fixture": NOW - monitor.WINDOW}}
        row = self.evaluate(entry, state)
        self.assertEqual((row["status"], row["run_identity_match"], row["artifact_authorized"]),
                         ("ANOMALY", "MATCH", "UNRECORDED"))

    def test_missing_run_and_unpinned_workflow_are_anomalies(self):
        def missing(url):
            raise HTTPError(url, 404, "missing", {}, None)
        row = monitor.evaluate(self.entry, {}, NOW, missing)
        self.assertEqual(row["status"], "ANOMALY")
        private = copy.deepcopy(self.entry)
        private["identity"]["sourceRepositoryVisibilityAtSigning"] = "private"
        self.assertEqual(monitor.evaluate(private, {}, NOW, missing)["status"], "UNKNOWN")
        entry = copy.deepcopy(self.entry)
        entry["identity"]["subjectAlternativeName"] = SAN.replace("refs/tags/v4", "refs/heads/untrusted")
        self.assertEqual(self.evaluate(entry)["status"], "ANOMALY")
        self.run["referenced_workflows"] = []
        self.assertEqual(self.evaluate()["status"], "ANOMALY")

    def test_inflight_and_unrecorded_digest_have_bounded_pending_window(self):
        self.run["status"] = "in_progress"
        state = {}
        self.assertEqual(self.evaluate(state=state)["status"], "PENDING")
        self.assertEqual(self.evaluate(state=state, now=NOW + monitor.WINDOW)["status"], "ANOMALY")
        self.run["status"] = "completed"
        self.assertEqual(self.evaluate(state=state, now=NOW + monitor.WINDOW)["status"], "MATCHED")
        self.manifest["assets"] = {}
        state = {}
        self.assertEqual(self.evaluate(state=state)["status"], "PENDING")
        self.manifest["assets"] = {"archive": {"sha256": DIGEST}}
        self.assertEqual(self.evaluate(state=state, now=NOW + 3600)["status"], "MATCHED")

    def test_opaque_image_budget_is_per_release_and_unique_hash(self):
        self.manifest["images"] = {
            "public": {"uri": "public.ecr.aws/example/image@sha256:" + DIGEST, "isIndex": True},
            "lambda": {"uri": "123.dkr.ecr.us-west-2.amazonaws.com/image@sha256:" + DIGEST, "isIndex": True}}
        state = {"first_seen": {"one": NOW - monitor.WINDOW, "two": NOW - monitor.WINDOW}}
        one = dict(self.entry, uuid="one", digest="b" * 64)
        two = dict(self.entry, uuid="two", digest="d" * 64)
        self.assertEqual(self.evaluate(one, state)["status"], "UNKNOWN")
        self.assertEqual(self.evaluate(one, state)["status"], "UNKNOWN", "duplicate must not consume another slot")
        self.assertEqual(self.evaluate(two, state)["status"], "ANOMALY")
        self.assertEqual(len(state["unmatched_hashes"]["ConductorOne/example@v1.2.3"]), 2)

    def test_no_invented_joins_or_payload_digest_aliases(self):
        for kind in ("dsse", "intoto"):
            row = self.evaluate(dict(self.entry, kind=kind))
            self.assertEqual((row["status"], row["artifact_authorized"]), ("UNKNOWN", "UNKNOWN"))
        self.catalog["catalogs"] = {}
        self.assertEqual(self.evaluate()["status"], "UNKNOWN")
        self.setUp()
        original = self.get
        def hidden(url):
            if url.endswith("/example/index.json"):
                raise HTTPError(url, 403, "hidden", {}, None)
            return original(url)
        self.assertEqual(monitor.evaluate(self.entry, {}, NOW, hidden)["status"], "UNKNOWN")
        entry = copy.deepcopy(self.entry)
        entry["identity"].pop("runInvocationURI")
        self.assertEqual(self.evaluate(entry)["status"], "UNKNOWN")
        row = self.evaluate(dict(self.entry, error="UNAUTHENTICATED-SAN"))
        self.assertEqual((row["status"], row["run_identity_match"]), ("INFORMATIONAL", "UNKNOWN"))
        self.assertEqual(self.evaluate(dict(self.entry, error="invalid inclusion proof"))["status"], "MONITOR-ERROR")

    def test_manifest_bytes_and_archive_hashes_are_authorized_not_image_digests(self):
        release, status, _ = monitor.release_data(self.entry["identity"], self.get)
        self.assertEqual(status, "READY")
        raw = json.dumps(self.manifest).encode()
        self.assertIn(monitor.hashlib.sha256(raw).hexdigest(), release["digests"])
        self.assertIn(DIGEST, release["digests"])

    def test_repeated_old_checkpoint_and_new_note_signature(self):
        checkpoint = "origin\\n10\\nroot\\n\\n— signer signature"
        state, stale = monitor.checkpoint_progress(checkpoint, {}, NOW)
        self.assertFalse(stale)
        _, stale = monitor.checkpoint_progress(checkpoint.replace("signature", "new-signature"), state, NOW + 7201)
        self.assertTrue(stale)
        _, stale = monitor.checkpoint_progress(checkpoint.replace("10", "11"), state, NOW + 7201)
        self.assertFalse(stale)
        with self.assertRaises(ValueError):
            monitor.checkpoint_progress(checkpoint, state, NOW - 1)

    def test_adapter_preserves_pending_across_actual_helper_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            state = directory / "state"
            state.mkdir()
            (state / "checkpoint.txt").write_text("origin\\n10\\nroot\\n")
            (state / "diagnostics.log").write_text("Certificate chain for log entry (UUID: fixture) could not be verified\n")
            observations = state / "identities.jsonl"
            observations.write_text(json.dumps({"UUID": "fixture", "Index": 42}) + "\n")
            helper = directory / "helper"
            helper.write_text("#!/usr/bin/env python3\nimport json,sys\nfor line in sys.stdin:\n print(" + repr(json.dumps(self.entry)) + ")\n")
            helper.chmod(0o755)
            argv = ["monitor", "--state-dir", str(state), "--identities", str(observations),
                    "--entry-helper", str(helper), "--bootstrap"]
            self.run["status"] = "in_progress"
            with patch.object(sys, "argv", argv), patch.object(monitor, "fetch", self.get), patch.object(monitor.time, "time", return_value=NOW), redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(), 0)
            saved = json.loads((state / "reconciliation.json").read_text())
            self.assertEqual(saved["pending"][0]["UUID"], "fixture")
            self.assertEqual(observations.read_text(), "")
            self.run["status"] = "completed"
            with patch.object(sys, "argv", argv[:-1]), patch.object(monitor, "fetch", self.get), patch.object(monitor.time, "time", return_value=NOW+3600), redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(), 0)
            self.assertEqual(json.loads((state / "report.json").read_text())["entries"][0]["status"], "MATCHED")
            self.assertEqual(json.loads((state / "reconciliation.json").read_text())["pending"], [])
            diagnostic = json.loads((state / "report.json").read_text())["unauthenticated_san_diagnostics"]
            self.assertEqual((diagnostic["label"], diagnostic["status"], diagnostic["count"]), ("UNAUTHENTICATED-SAN", "INFORMATIONAL", 1))
            # Failure after scan must retain the raw observation for the next run.
            observations.write_text(json.dumps({"UUID": "fixture", "Index": 42}) + "\n")
            helper.write_text("#!/bin/sh\nexit 1\n")
            with patch.object(sys, "argv", argv[:-1]), self.assertRaises(subprocess.CalledProcessError):
                monitor.main()
            self.assertIn("fixture", observations.read_text())

    def test_network_boundary_does_not_forward_credentials_or_follow_redirects(self):
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                self_test.assertEqual(timeout, 30)
                return io.BytesIO(b"{}")
        self_test = self
        with patch.object(monitor, "build_opener", return_value=Opener()), patch.dict(os.environ, GH_TOKEN="fixture-token"):
            monitor.fetch(monitor.GITHUB + "/repos/ConductorOne/example/actions/runs/123")
            monitor.fetch(monitor.DIST + "/index.json")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fixture-token")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertIsNone(monitor.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test/"))
        for url in ("http://api.github.com/", "https://api.github.com.evil/"):
            with self.assertRaises(ValueError):
                monitor.fetch(url)

    def test_workflow_pins_identity_config_and_negative_controls(self):
        workflow = json.loads(subprocess.check_output(["yq", ".", str(ROOT / ".github/workflows/rekor-monitor.yaml")], text=True))
        self.assertEqual(workflow_errors(workflow), [])
        self.assertIn("schedule", workflow["on"])
        for field, value in (("GOSUMDB", "off"), ("MONITOR_VERSION", "latest")):
            changed = copy.deepcopy(workflow)
            changed["jobs"]["monitor"]["env"][field] = value
            self.assertTrue(workflow_errors(changed))
        changed = copy.deepcopy(workflow)
        changed["jobs"]["monitor"]["steps"][0]["uses"] = "actions/checkout@v5"
        self.assertTrue(workflow_errors(changed))
        config = json.loads(subprocess.check_output(["yq", ".", str(ROOT / "tools/rekor-monitor.yaml")], text=True))
        for identity in monitor.PINNED:
            matches = [item for item in config["monitoredValues"]["certIdentities"] if re.fullmatch(item["certSubject"], identity)]
            self.assertEqual(len(matches), 1)
            self.assertTrue(re.fullmatch(matches[0]["issuers"][0], monitor.ISSUER))
            self.assertFalse(re.fullmatch(matches[0]["certSubject"], identity.replace("github.com/", "github.com.evil/")))
        self.assertEqual(config["caRootsFile"], "tools/rekor-fulcio-roots.pem")
        self.assertEqual(config["caIntermediatesFile"], "tools/rekor-fulcio-intermediates.pem")
        restore = next(step for step in workflow["jobs"]["monitor"]["steps"] if step.get("id") == "restore")
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fake = directory / "gh"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}", GITHUB_REPOSITORY="ConductorOne/github-workflows", GITHUB_OUTPUT=str(directory/"output"))
            for bootstrap, expected in (("false", 1), ("true", 0)):
                result = subprocess.run(["bash", "-c", restore["run"]], cwd=directory, env=dict(env, BOOTSTRAP=bootstrap), capture_output=True)
                self.assertEqual(result.returncode, expected)


if __name__ == "__main__":
    unittest.main()
