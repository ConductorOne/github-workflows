#!/usr/bin/env python3
"""Native evidence -> required upload -> publication -> exact reset fixtures.

These exercise real host helpers with mocked GitHub/runner transport. They do
not prove deployed credential isolation, CI-token access or GitHub merge semantics.
"""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request
import zipfile

import _gh
import _review_result as rr
import _review_state as rs
from test_verdict_scaffolding import (
    ARTIFACT, BASE, ENV, FRESH, HEAD, HEADING, IDENTITY, RUN_START, T1, T2,
    accepted_fixture, comment, count_row, new_style_state, pub, report_body,
    superseded_body, verdict_review, working_body,
)

EVIDENCE = [{"path": "file.go", "symbol_or_range": "function F",
             "explanation": "Current implementation removes the original failure"}]


def payload_of(accepted):
    payload = copy.deepcopy(accepted["result"])
    for finding in payload["blocking_findings"]:
        finding.pop("finding_id")
    return payload


def archive_of(accepted, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in (("accepted.json", accepted), ("snapshot.json", accepted["snapshot"]),
                            ("completion.json", accepted["completion"])):
            archive.writestr(name, json.dumps(value))
        if extra:
            for name, value in extra:
                archive.writestr(name, value)
    raw = stream.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def messages_of(payload, *, delegated=False):
    messages = [{"type": "system", "subtype": "init", "tools": ["Read", "Glob", "Grep", "Agent", "StructuredOutput"],
                 "permissionMode": "default", "mcp_servers": []}]
    if delegated:
        messages.extend([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "audit-1", "name": "Agent",
                 "input": {"prompt": "Bounded read-only audit", "run_in_background": False}}]}},
            {"type": "system", "subtype": "task_started", "task_type": "local_agent",
             "is_backgrounded": False, "tool_use_id": "audit-1", "task_id": "task-1"},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "audit-1", "content": "Evidence returned", "is_error": False}]},
             "tool_use_result": {"status": "completed", "agentId": "agent-1",
                                 "content": [{"type": "text", "text": "Evidence returned"}],
                                 "toolStats": {"bashCount": 0, "editFileCount": 0}}},
        ])
    messages.append({"type": "result", "subtype": "success", "is_error": False,
                     "num_turns": 4, "structured_output": payload})
    return messages


class GitHubFixture:
    def __init__(self):
        self.comments = {2: dict(comment(2, working_body(0, provisional=True)), node_id="comment-2")}
        self.reviews = {}
        self.artifacts = {}
        self.archives = {}
        self.history_overrides = {}
        self.calls = []
        self.put_behavior = {}
        self.fail_publication = None
        self.pr = {"state": "open", "head": {"sha": HEAD, "repo": {"full_name": "example/repo"}},
                   "base": {"sha": BASE, "ref": "main"}}

    def add_artifact(self, artifact_id, accepted):
        raw, digest = archive_of(accepted)
        identity = accepted["identity"]
        self.archives[str(artifact_id)] = raw
        self.artifacts[str(artifact_id)] = {
            "id": artifact_id, "name": rr.artifact_name(identity), "expired": False,
            "digest": "sha256:" + digest,
            "workflow_run": {"id": int(identity["run_id"]), "repository_id": 123,
                             "head_repository_id": 123, "head_sha": accepted["context"]["current_sha"]},
        }
        return {"result_artifact_id": str(artifact_id), "result_artifact_digest": digest}

    def add_block(self, rid=10, report_id=20, run_id="123", *, superseded=False):
        identity = dict(IDENTITY, run_id=run_id, run_attempt="1")
        original = accepted_fixture(working_body(1), identity=identity)
        original["snapshot"]["started_at"] = T1
        reference = self.add_artifact(rid + 100, original)
        state = new_style_state(**identity, **reference, started_at=T1)
        body = report_body(1, state)
        if superseded:
            body = superseded_body(body)
        self.comments[report_id] = dict(comment(report_id, body), node_id=f"comment-{report_id}")
        review = verdict_review(rid, report_id=report_id, state="CHANGES_REQUESTED", **identity, **reference)
        review.update(node_id=f"review-{rid}", pull_request_url=f"{_gh.API_ROOT}/repos/example/repo/pulls/42")
        self.reviews[rid] = review
        return original["result"]["blocking_findings"][0]["finding_id"]

    def paginate(self, path, **kwargs):
        self.calls.append(("LIST", path, None))
        if path.endswith("/reviews"):
            return copy.deepcopy(list(self.reviews.values()))
        if path.endswith("/comments"):
            return copy.deepcopy(list(self.comments.values()))
        raise AssertionError(path)

    def rest(self, method, path, **kwargs):
        data = kwargs.get("data")
        self.calls.append((method, path, data))
        if method == "GET":
            if path == "repos/example/repo":
                return {"id": 123}
            if path == "repos/example/repo/pulls/42":
                return copy.deepcopy(self.pr)
            if "/issues/comments/" in path:
                value = self.comments.get(int(path.rsplit("/", 1)[1]))
            elif "/pulls/42/reviews/" in path:
                value = self.reviews.get(int(path.rsplit("/", 1)[1]))
            elif "/actions/artifacts/" in path:
                value = self.artifacts.get(path.rsplit("/", 1)[1])
            elif "/actions/runs/" in path:
                pieces = path.split("/")
                value = {"id": int(pieces[-3]), "run_attempt": int(pieces[-1]), "head_sha": HEAD,
                         "repository": {"id": 123}, "head_repository": {"id": 123}}
            else:
                raise AssertionError(path)
            if value is None:
                raise _gh.TerminalError("missing original evidence", status=404)
            return copy.deepcopy(value)
        if method == "POST" and path.endswith("/comments"):
            if self.fail_publication == "report":
                raise _gh.TerminalError("report denied", status=403)
            self.comments[900] = dict(comment(900, data["body"]), node_id="comment-900")
            return copy.deepcopy(self.comments[900])
        if method == "POST" and path.endswith("/reviews"):
            if self.fail_publication == "review":
                raise _gh.TerminalError("review denied", status=403)
            self.reviews[77] = dict(verdict_review(77), node_id="review-77", body=data["body"],
                                    commit_id=data["commit_id"], state={"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[data["event"]],
                                    pull_request_url=f"{_gh.API_ROOT}/repos/example/repo/pulls/42")
            return copy.deepcopy(self.reviews[77])
        if method == "PATCH":
            if self.fail_publication == "complete" and path.endswith("/900"):
                raise _gh.TerminalError("completion denied", status=403)
            cid = int(path.rsplit("/", 1)[1])
            self.comments[cid]["body"] = data["body"]
            return copy.deepcopy(self.comments[cid])
        if method == "PUT" and path.endswith("/dismissals"):
            if kwargs.get("max_attempts") != 1:
                raise AssertionError("mutation must have one transport attempt")
            rid = int(path.split("/")[-2])
            behavior = self.put_behavior.get(rid)
            if behavior == "denied":
                raise _gh.TerminalError("dismissal denied", status=403)
            if behavior != "lost_before":
                self.reviews[rid]["state"] = "DISMISSED"
            if callable(behavior):
                behavior()
            if behavior in {"lost_before", "lost_after"}:
                raise _gh.TransientOutageError("lost dismissal response")
            return copy.deepcopy(self.reviews[rid])
        raise AssertionError((method, path))

    def history(self, query, variables, **kwargs):
        node_id = variables["id"]
        collection = self.reviews if node_id.startswith("review-") else self.comments
        item = collection[int(node_id.split("-")[1])]
        parent = "pullRequest"
        node = {"id": node_id, "fullDatabaseId": str(item["id"]), "body": item["body"],
                "author": item["user"], "lastEditedAt": None, "editor": None, "includesCreatedEdit": False,
                parent: {"number": 42, "repository": {"nameWithOwner": "example/repo"}},
                "userContentEdits": {"totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        node.update(copy.deepcopy(self.history_overrides.get(node_id, {})))
        return {"node": node}

    def download(self, repo, artifact_id):
        return self.archives[str(artifact_id)]

    def puts(self):
        return [(path, data) for method, path, data in self.calls if method == "PUT"]


class ResultFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "checkout"
        self.runner = root / "runner"
        self.workspace.mkdir()
        self.runner.mkdir()
        (self.workspace / ".github").mkdir()
        self.old_cwd = os.getcwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.old_cwd)
        self.github = GitHubFixture()
        self.env = dict(ENV, RUNNER_TEMP=str(self.runner), GITHUB_WORKSPACE=str(self.workspace),
                        GITHUB_OUTPUT=str(self.runner / "output"), REVIEW_MODEL_CONCLUSION="success")
        self.enterContext(mock.patch.dict(os.environ, self.env))
        self.enterContext(mock.patch.object(rr, "publisher", return_value=pub))
        self.enterContext(mock.patch.object(_gh, "rest", side_effect=self.github.rest))
        self.enterContext(mock.patch.object(_gh, "rest_paginate", side_effect=self.github.paginate))
        self.enterContext(mock.patch.object(_gh, "graphql", side_effect=self.github.history))
        self.enterContext(mock.patch.object(_gh, "request", return_value=(200, {}, b"diff --git a/file.go b/file.go\n@@ -1 +1 @@\n-old\n+new\n")))
        self.enterContext(mock.patch.object(rr, "download_archive", side_effect=self.github.download))
        self.enterContext(mock.patch.object(pub.subprocess, "run", side_effect=lambda args, **kwargs:
            SimpleNamespace(stdout=(b"100644 blob " + b"a" * 40 + b"\tfile.go\0") if "ls-tree" in args else HEAD + "\n")))
        context = accepted_fixture(working_body(0))["context"]
        rr.write_json(self.workspace / ".github/pr-context.json", context)

    def prepare(self):
        rr.prepare()
        self.directory = Path(os.environ["REVIEW_RESULT_DIR"])
        return rr.read_json(self.directory / "snapshot.json")

    def accept(self, snapshot, *, dispositions=None, blockers=0, summary=None):
        payload = payload_of(accepted_fixture(summary or working_body(blockers)))
        payload["prior_finding_audit"] = [
            {"finding_id": item["finding_id"], "disposition": (dispositions or {}).get(item["finding_id"], "fixed"),
             "evidence": copy.deepcopy(EVIDENCE)} for item in snapshot["obligations"]]
        for audit in payload["prior_finding_audit"]:
            if audit["disposition"] == "still_present":
                payload["blocking_findings"].append({"prior_finding_id": audit["finding_id"], "title": "Prior failure",
                                                    "rationale": "Still present", "evidence": copy.deepcopy(EVIDENCE)})
        count = len(payload["blocking_findings"])
        payload["report"]["summary"] = (summary or working_body(count))
        execution = self.runner / "claude-execution-output.json"
        execution.write_text(json.dumps(messages_of(payload)), encoding="utf-8")
        os.environ.update(REVIEW_STRUCTURED_OUTPUT=json.dumps(payload), REVIEW_EXECUTION_FILE=str(execution))
        rr.validate()
        accepted = rr.read_json(self.directory / "accepted.json")
        reference = self.github.add_artifact(999, accepted)
        os.environ.update(REVIEW_RESULT_UPLOAD_CONCLUSION="success", REVIEW_RESULT_ARTIFACT_ID="999",
                          REVIEW_RESULT_ARTIFACT_DIGEST=reference["result_artifact_digest"])
        return accepted, reference

    def publish(self):
        try:
            pub.main()
            return 0
        except SystemExit as error:
            return error.code

    def test_success_dismisses_exact_targets_after_comment_and_completion(self):
        first = self.github.add_block(superseded=True)
        second = self.github.add_block(rid=11, report_id=21, run_id="124")
        snapshot = self.prepare()
        self.assertEqual({x["finding_id"] for x in snapshot["obligations"]}, {first, second})
        self.accept(snapshot)
        # Even a human-edited progress count is NOT final authority.
        self.github.comments[2]["body"] = working_body(99)
        self.assertEqual(self.publish(), 0)
        self.assertEqual([path.split("/")[-2] for path, _ in self.github.puts()], ["10", "11"])
        methods = self.github.calls
        put_index = next(i for i, call in enumerate(methods) if call[0] == "PUT")
        review_index = next(i for i, call in enumerate(methods) if call[0] == "POST" and call[1].endswith("/reviews"))
        completion_index = next(i for i, call in enumerate(methods) if call[0] == "PATCH" and call[1].endswith("/900"))
        self.assertLess(review_index, completion_index)
        self.assertLess(completion_index, put_index)
        self.assertEqual(methods[review_index][2]["event"], "COMMENT")
        self.assertTrue(all(call[2].get("event") != "APPROVE" for call in methods if isinstance(call[2], dict)))
        receipt = rr.read_json(self.directory / "reset-receipt.json")
        self.assertTrue(receipt["reset_completed"])
        self.assertEqual([item["outcome"] for item in receipt["outcomes"]], ["dismissed", "dismissed"])

    def test_full_diff_preserves_non_utf8_vendor_bytes(self):
        _gh.request.return_value = (200, {}, b"diff --git a/vendor/file b/vendor/file\n-old\n+\xff\n")
        self.prepare()
        self.assertIn(r"\xff", (self.workspace / ".github/pr-review.diff").read_text())
        self.assertIn("vendor/file", (self.workspace / ".github/pr-review.diff").read_text())

    def test_full_diff_406_or_size_failure_is_explicit_coverage_failure(self):
        for error in (_gh.TerminalError("HTTP 406: diff too large", status=406),
                      _gh.TerminalError("response exceeds 20971520 bytes")):
            with self.subTest(error=error):
                _gh.request.side_effect = error
                with self.assertRaisesRegex(_gh.TerminalError, "Full PR diff coverage unavailable"):
                    self.prepare()
                self.assertEqual(self.github.puts(), [])

    def test_64_bit_review_and_report_ids_keep_exact_graphql_binding(self):
        self.github.add_block(rid=5484497165, report_id=5835049422)
        snapshot = self.prepare()
        self.assertEqual([target["review_id"] for target in snapshot["targets"]], [5484497165])
        self.assertFalse(snapshot["unavailable"])

    def test_first_clean_is_comment_and_no_op(self):
        self.accept(self.prepare())
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.github.puts(), [])
        self.assertTrue(rr.read_json(self.directory / "reset-receipt.json")["no_op"])
        self.assertEqual(self.github.reviews[77]["state"], "COMMENTED")

    def test_current_blocker_keeps_every_old_block(self):
        self.github.add_block()
        self.accept(self.prepare(), blockers=1)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.github.puts(), [])
        self.assertEqual(self.github.reviews[77]["state"], "CHANGES_REQUESTED")
        self.assertEqual(self.github.reviews[10]["state"], "CHANGES_REQUESTED")

    def test_unverified_and_still_present_retain(self):
        for disposition in ("unverified", "still_present"):
            with self.subTest(disposition=disposition):
                finding_id = self.github.add_block()
                snapshot = self.prepare()
                self.accept(snapshot, dispositions={finding_id: disposition})
                self.assertEqual(self.publish(), 0)
                self.assertEqual(self.github.puts(), [])
                self.github.comments = {2: self.github.comments[2]}
                self.github.reviews.pop(77, None)
                self.github.calls.clear()
                (self.runner / "claude-execution-output.json").unlink()

    def test_missing_one_original_blocks_whole_reset_not_convenient_subset(self):
        self.github.add_block()
        self.github.add_block(rid=11, report_id=21, run_id="124")
        self.github.artifacts.pop("111")
        snapshot = self.prepare()
        self.assertEqual(len(snapshot["targets"]), 1)
        self.assertEqual(snapshot["unavailable"][0]["review_id"], 11)
        self.accept(snapshot)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.github.puts(), [])

    def test_foreign_human_and_none_reviews_are_never_targets(self):
        self.github.add_block()
        self.github.add_block(rid=11, report_id=21, run_id="124")
        self.github.reviews[11]["user"]["login"] = "human"
        foreign = self.github.add_block(rid=12, report_id=22, run_id="125")
        reference = rr.marker(self.github.reviews[12]["body"], pub.VERDICT_MARKER_PATTERN)
        reference["workflow_ref"] = "other/workflow@main"
        self.github.reviews[12]["body"] = f"<!-- review-publication: {json.dumps(reference)} -->"
        self.github.add_block(rid=13, report_id=23, run_id="126")
        reference["workflow_ref"] = IDENTITY["workflow_ref"]
        reference["verdict_mode"] = "none"
        self.github.reviews[13]["body"] = f"<!-- review-publication: {json.dumps(reference)} -->"
        snapshot = self.prepare()
        self.assertEqual([target["review_id"] for target in snapshot["targets"]], [10])
        self.assertNotIn(foreign, {obligation["finding_id"] for obligation in snapshot["obligations"]})
        self.accept(snapshot)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.github.puts()), 1)
        self.assertEqual(self.github.reviews[11]["state"], "CHANGES_REQUESTED")

    def test_report_edit_or_review_edit_cannot_replace_original_pointer(self):
        for node_id, overrides in (
            ("review-10", {"lastEditedAt": FRESH}),
            ("comment-20", {"lastEditedAt": FRESH, "editor": {"login": "human"}}),
            ("comment-20", {"userContentEdits": None}),
            ("comment-20", {"userContentEdits": {"totalCount": 1, "nodes": [None], "pageInfo": {"hasNextPage": False}}}),
            ("comment-20", {"userContentEdits": {"totalCount": 1, "nodes": [{"editor": {"login": "github-actions[bot]"}, "deletedAt": FRESH}],
                                                "pageInfo": {"hasNextPage": False}}}),
        ):
            with self.subTest(node=node_id, overrides=overrides):
                self.github.add_block()
                self.github.history_overrides = {node_id: overrides}
                snapshot = self.prepare()
                self.assertEqual(snapshot["targets"], [])
                self.assertEqual(len(snapshot["unavailable"]), 1)

    def test_bot_report_edits_and_deleted_inline_context_do_not_shrink_obligations(self):
        finding_id = self.github.add_block(superseded=True)
        self.github.history_overrides["comment-20"] = {
            "lastEditedAt": FRESH, "editor": {"login": "github-actions[bot]"}, "includesCreatedEdit": True,
            "userContentEdits": {"totalCount": 1, "nodes": [{"editor": {"login": "github-actions[bot]"}, "deletedAt": None, "deletedBy": None}],
                                 "pageInfo": {"hasNextPage": False}}}
        self.github.comments[20]["body"] = self.github.comments[20]["body"].replace(count_row(1), count_row(0))
        (self.workspace / ".github/prior-findings.json").write_text('{"prior_findings": []}')
        snapshot = self.prepare()
        self.assertEqual([item["finding_id"] for item in snapshot["obligations"]], [finding_id])

    def test_missing_expired_forged_or_contradictory_artifact_retains(self):
        changes = (
            lambda: self.github.artifacts["110"].update(expired=True),
            lambda: self.github.artifacts["110"]["workflow_run"].update(repository_id=999),
            lambda: self.github.artifacts["110"]["workflow_run"].update(id=999),
            lambda: self.github.artifacts["110"]["workflow_run"].update(head_sha="f" * 40),
            lambda: self.github.archives.update({"110": b"corrupt archive"}),
            lambda: self.github.comments.pop(20),
            lambda: self.github.reviews[10].update(pull_request_url=f"{_gh.API_ROOT}/repos/example/repo/pulls/43"),
        )
        for change in changes:
            with self.subTest(change=change):
                self.github.add_block()
                change()
                snapshot = self.prepare()
                self.assertEqual(snapshot["targets"], [])
                self.assertTrue(snapshot["unavailable"])

    def test_duplicate_claim_and_unknown_legacy_keep_whole_reset(self):
        self.github.add_block()
        self.github.reviews[11] = dict(self.github.reviews[10], id=11, node_id="review-11")
        snapshot = self.prepare()
        self.assertTrue(snapshot["unavailable"])
        self.github.reviews[11]["body"] = "Legacy request: please fix the old blocker"
        snapshot = self.prepare()
        self.assertTrue(snapshot["unavailable"])

    def test_failed_required_upload_cannot_be_rescued_by_diagnostics(self):
        self.github.add_block()
        self.accept(self.prepare())
        os.environ["REVIEW_RESULT_UPLOAD_CONCLUSION"] = "failure"
        self.assertEqual(self.publish(), 1)
        self.assertFalse(any(method in {"POST", "PATCH", "PUT"} for method, _, _ in self.github.calls))

    def test_publication_failure_never_dismisses(self):
        for boundary in ("report", "review", "complete"):
            with self.subTest(boundary=boundary):
                self.github.comments = {2: dict(comment(2, working_body(0, provisional=True)), node_id="comment-2")}
                self.github.reviews = {}
                self.github.calls.clear()
                self.github.add_block()
                self.github.fail_publication = boundary
                self.accept(self.prepare())
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.github.puts(), [])
                self.assertEqual(self.github.reviews[10]["state"], "CHANGES_REQUESTED")

    def test_stale_head_base_ref_base_sha_closed_and_new_block_abort(self):
        changes = (
            lambda: self.github.pr["head"].update(sha="f" * 40),
            lambda: self.github.pr["base"].update(sha="f" * 40),
            lambda: self.github.pr["base"].update(ref="release"),
            lambda: self.github.pr.update(state="closed"),
            lambda: self.github.add_block(rid=11, report_id=21, run_id="124"),
        )
        for change in changes:
            with self.subTest(change=change):
                self.github.pr = {"state": "open", "head": {"sha": HEAD, "repo": {"full_name": "example/repo"}}, "base": {"sha": BASE, "ref": "main"}}
                self.github.reviews = {}
                self.github.comments = {2: dict(comment(2, working_body(0, provisional=True)), node_id="comment-2")}
                self.github.calls.clear()
                self.github.add_block()
                self.accept(self.prepare())
                change()
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.github.puts(), [])

    def test_newer_completed_attempt_aborts_before_put(self):
        self.github.add_block()
        self.accept(self.prepare())
        self.github.comments[950] = comment(950, report_body(0, new_style_state(run_id="555", started_at=T2)))
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.github.puts(), [])

    def test_target_pointer_edit_after_snapshot_aborts(self):
        self.github.add_block()
        self.accept(self.prepare())
        reference = rr.marker(self.github.reviews[10]["body"], pub.VERDICT_MARKER_PATTERN)
        reference["result_artifact_id"] = "999"
        self.github.reviews[10]["body"] = f"<!-- review-publication: {json.dumps(reference)} -->"
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.github.puts(), [])

    def test_push_between_targets_retains_remaining_block(self):
        self.github.add_block()
        self.github.add_block(rid=11, report_id=21, run_id="124")
        self.accept(self.prepare())
        self.github.put_behavior[10] = lambda: self.github.pr["head"].update(sha="f" * 40)
        self.assertEqual(self.publish(), 1)
        self.assertEqual(len(self.github.puts()), 1)
        self.assertEqual(self.github.reviews[11]["state"], "CHANGES_REQUESTED")

    def test_denied_partial_and_repeated_reset_is_idempotent(self):
        self.github.add_block()
        self.github.add_block(rid=11, report_id=21, run_id="124")
        self.accept(self.prepare())
        self.github.put_behavior[11] = "denied"
        self.assertEqual(self.publish(), 1)
        receipt = rr.read_json(self.directory / "reset-receipt.json")
        self.assertEqual([x["outcome"] for x in receipt["outcomes"]], ["dismissed", "failed"])
        self.github.put_behavior.clear()
        previous_puts = len(self.github.puts())
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.github.puts()), previous_puts + 1)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.github.puts()), previous_puts + 1)
        receipt = rr.read_json(self.directory / "reset-receipt.json")
        self.assertEqual([x["outcome"] for x in receipt["outcomes"]], ["already_dismissed", "already_dismissed"])

    def test_response_loss_after_success_reconciles_without_second_put(self):
        self.github.add_block()
        self.accept(self.prepare())
        self.github.put_behavior[10] = "lost_after"
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.github.puts()), 1)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.github.puts()), 1)

    def test_response_loss_before_success_fails_and_retains(self):
        self.github.add_block()
        self.accept(self.prepare())
        self.github.put_behavior[10] = "lost_before"
        self.assertEqual(self.publish(), 1)
        self.assertEqual(len(self.github.puts()), 1)
        self.assertEqual(self.github.reviews[10]["state"], "CHANGES_REQUESTED")

    def test_human_dismissal_is_not_recreated(self):
        self.github.add_block()
        self.accept(self.prepare())
        self.github.reviews[10]["state"] = "DISMISSED"
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.github.puts(), [])

    def test_current_run_envelope_and_files_cannot_be_substituted(self):
        self.accept(self.prepare())
        for key, value in (("GITHUB_RUN_ID", "77"), ("GITHUB_RUN_ATTEMPT", "7"),
                           ("GITHUB_REPOSITORY", "other/repo"), ("PR_NUMBER", "43"),
                           ("GITHUB_WORKFLOW_REF", "other/workflow"), ("SUMMARY_MARKER", "### Other Review:")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                with self.assertRaises(_gh.TerminalError):
                    rr.load_current_result()
        accepted_path = self.directory / "accepted.json"
        accepted = rr.read_json(accepted_path)
        accepted["context"]["current_sha"] = "f" * 40
        accepted_path.write_text(json.dumps(accepted))
        with self.assertRaises(_gh.TerminalError):
            rr.load_current_result()


class NativeResultTest(unittest.TestCase):
    def setUp(self):
        self.accepted = accepted_fixture(working_body(0))
        self.payload = payload_of(self.accepted)
        self.snapshot = self.accepted["snapshot"]
        self.snapshot["obligations"] = [{"finding_id": "123-1-1", "versions": []}]
        self.payload["prior_finding_audit"] = [{"finding_id": "123-1-1", "disposition": "fixed", "evidence": copy.deepcopy(EVIDENCE)}]

    def test_every_current_prior_id_even_after_previous_fixed_result(self):
        self.snapshot["previous_fixed"] = ["123-1-1"]  # Context cannot authorize skipping this run.
        self.payload["prior_finding_audit"] = []
        with self.assertRaises(_gh.TerminalError):
            rr.validate_payload(self.payload, self.snapshot)

    def test_all_withdrawable_dispositions_accept(self):
        for disposition in rr.WITHDRAWABLE:
            with self.subTest(disposition=disposition):
                self.payload["prior_finding_audit"][0]["disposition"] = disposition
                self.assertEqual(rr.validate_payload(self.payload, self.snapshot)["blocking_findings"], [])

    def test_missing_duplicate_foreign_invalid_and_contradictory_audits_reject(self):
        for audits in (
            [], [self.payload["prior_finding_audit"][0]] * 2,
            [{"finding_id": "999-1-1", "disposition": "fixed", "evidence": EVIDENCE}],
            [{"finding_id": "123-1-1", "disposition": "fixed", "evidence": []}],
            [{"finding_id": "123-1-1", "disposition": "still_present", "evidence": EVIDENCE}],
            [{"finding_id": "123-1-1", "disposition": "resolved_thread", "evidence": EVIDENCE}],
        ):
            with self.subTest(audits=audits):
                payload = dict(self.payload, prior_finding_audit=audits)
                with self.assertRaises(_gh.TerminalError):
                    rr.validate_payload(payload, self.snapshot)

    def test_carried_ids_preserved_new_ids_host_issued(self):
        payload = payload_of(accepted_fixture(working_body(2)))
        payload["blocking_findings"][0]["prior_finding_id"] = "123-1-1"
        payload["prior_finding_audit"] = [{"finding_id": "123-1-1", "disposition": "still_present", "evidence": EVIDENCE}]
        result = rr.validate_payload(payload, self.snapshot)
        self.assertEqual([item["finding_id"] for item in result["blocking_findings"]], ["123-1-1", "87654321-2-2"])

    def test_schema_coverage_count_markers_paths_and_provisional_veto(self):
        mutations = (
            lambda p: p.update(dismiss=True),
            lambda p: p.update(schema_version=True),
            lambda p: p["model_assessment"].update(review_complete=False),
            lambda p: p["model_assessment"].update(coverage_gaps=["unread security surface"]),
            lambda p: p["report"].update(summary=working_body(1)),
            lambda p: p["report"].update(summary=working_body(0, provisional=True)),
            lambda p: p["report"].update(summary=working_body(0) + "<!-- review-state: {} -->"),
            lambda p: p["report"].update(summary=working_body(0) + "&#60;!-- review-state: {} -->"),
            lambda p: p["prior_finding_audit"][0]["evidence"][0].update(path="../output"),
            lambda p: p["prior_finding_audit"][0]["evidence"][0].update(explanation=" "),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(self.payload)
                mutation(payload)
                with self.assertRaises(_gh.TerminalError):
                    rr.validate_payload(payload, self.snapshot)

    def test_native_terminal_pending_error_and_inventory_integrity(self):
        messages = messages_of(self.payload, delegated=True)
        receipt = rr.completion_receipt(messages, self.payload)
        self.assertEqual(receipt["audits"][0]["tool_use_id"], "audit-1")
        mutations = (
            lambda m: m.pop(-2),
            lambda m: m[-1].update(is_error=True),
            lambda m: m[-1].update(subtype="error_max_turns"),
            lambda m: m[-1].update(structured_output={}),
            lambda m: m[-1].update(num_turns=101),
            lambda m: m[0]["tools"].append("Bash"),
            lambda m: m[0].update(permissionMode="bypassPermissions"),
            lambda m: m[0]["mcp_servers"].append({"name": "github", "status": "connected"}),
            lambda m: m[-2]["message"]["content"][0].update(is_error=True),
            lambda m: m[-2]["tool_use_result"].update(status="async_launched"),
            lambda m: m[-2]["tool_use_result"].update(toolStats={"bashCount": 1}),
            lambda m: m[1]["message"]["content"][0]["input"].update(run_in_background=True),
            lambda m: m[2].update(is_backgrounded=True),
            lambda m: m.insert(-1, {"type": "system", "subtype": "task_notification", "status": "failed"}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(messages)
                mutation(changed)
                with self.assertRaises(_gh.TerminalError):
                    rr.completion_receipt(changed, self.payload)

    def test_audit_prose_quoting_error_literals_is_not_execution_failure(self):
        messages = messages_of(self.payload, delegated=True)
        prose = 'Error: is the original API message; code example {"is_error": true, "isError": true}.'
        messages[-2]["message"]["content"][0]["content"] = prose
        messages[-2]["tool_use_result"]["content"][0]["text"] = prose
        self.assertEqual(len(rr.completion_receipt(messages, self.payload)["audits"]), 1)

    def test_optional_task_started_fields_do_not_replace_completed_result_proof(self):
        messages = messages_of(self.payload, delegated=True)
        messages[2].pop("task_type")
        messages[2].pop("is_backgrounded")
        self.assertEqual(len(rr.completion_receipt(messages, self.payload)["audits"]), 1)
        messages[-2]["tool_use_result"].pop("status")
        with self.assertRaises(_gh.TerminalError):
            rr.completion_receipt(messages, self.payload)

    def test_shell_looking_markdown_is_only_data(self):
        text = working_body(0) + '\n$(touch /tmp/never-created)\n"; exit 0; #\nGITHUB_OUTPUT=forged\n'
        payload = copy.deepcopy(self.payload)
        payload["report"]["summary"] = text
        with mock.patch.object(rr.subprocess, "run") as shell:
            result = rr.validate_payload(payload, self.snapshot)
        shell.assert_not_called()
        self.assertIn("$(touch", result["report"]["summary"])

    def test_sanitizer_preserves_pinned_redaction_and_hidden_text_removal(self):
        token = "ghs_" + "A" * 36
        text = ('x\u200b ![hidden](url) [link](url "hidden") <span title="hidden" data-x="hidden">ok</span> '
                '&#65; &#x42; ' + token + ' sk-ant-' + 'a' * 24 + ' xoxb-' + 'b' * 20 + '\nAKIA' + 'C' * 16)
        sanitized = rr.sanitize_markdown(text)
        self.assertNotIn("hidden", sanitized)
        self.assertNotIn(token, sanitized)
        for value in ("[REDACTED_GITHUB_TOKEN]", "[REDACTED_ANTHROPIC_KEY]", "[REDACTED_SLACK_TOKEN]", "[REDACTED_AWS_KEY_ID]"):
            self.assertIn(value, sanitized)
        self.assertIn("A B", sanitized)

    def test_sanitization_is_canonical_and_accepted_artifact_revalidates(self):
        samples = (
            "&#38;#60; &x",
            '<span title="&#38;#34;hidden&#38;#34;">visible</span>',
            "&#103;hs_" + "A" * 36,
            '[link](url "one" "two")',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = rr.sanitize_markdown(sample)
                self.assertEqual(rr.sanitize_markdown(cleaned), cleaned)
                accepted = accepted_fixture(working_body(0) + "\n" + sample)
                rr.check_accepted(accepted)

    def test_nested_encoded_host_markers_rejected_before_they_are_stripped(self):
        for marker in ("&#60;!-- review-state: {} -->", "&#38;#60;!-- review-publication: {} -->"):
            with self.subTest(marker=marker):
                payload = copy.deepcopy(self.payload)
                payload["report"]["summary"] += "\n" + marker
                with self.assertRaises(_gh.TerminalError):
                    rr.validate_payload(payload, self.snapshot)

    def test_excessive_entity_nesting_fails_explicitly(self):
        nested = "&#38;" + "#38;" * 30 + "#60;"
        with self.assertRaisesRegex(_gh.TerminalError, "did not converge"):
            rr.sanitize_markdown(nested)

    def test_source_refs_are_tracked_blobs_or_known_removed_original_paths(self):
        result = rr.validate_payload(self.payload, self.snapshot)
        with mock.patch.object(rr.subprocess, "run", return_value=SimpleNamespace(stdout=b"")):
            with self.assertRaises(_gh.TerminalError):
                rr.verify_source_references(result, self.snapshot)
            self.snapshot["obligations"][0]["versions"] = [{"evidence": copy.deepcopy(EVIDENCE)}]
            result["prior_finding_audit"][0]["disposition"] = "no_longer_applicable"
            rr.verify_source_references(result, self.snapshot)

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        for raw in ('{"schema_version":1,"schema_version":2}', '{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(_gh.TerminalError):
                rr.strict_json(raw)


class ArchiveBoundaryTest(unittest.TestCase):
    def test_digest_is_zip_not_inner_json(self):
        accepted = accepted_fixture(working_body(1))
        raw, digest = archive_of(accepted)
        self.assertEqual(rr.archive_result(raw, "sha256:" + digest), accepted)
        inner_digest = hashlib.sha256(json.dumps(accepted).encode()).hexdigest()
        with self.assertRaises(_gh.TerminalError):
            rr.archive_result(raw, inner_digest)

    def test_extra_duplicate_traversal_symlink_or_oversize_members_rejected(self):
        accepted = accepted_fixture(working_body(1))
        for extra in ([('../accepted.json', '{}')], [('accepted.json', '{}')], [('extra.json', '{}')]):
            with self.subTest(extra=extra):
                raw, digest = archive_of(accepted, extra)
                with self.assertRaises(_gh.TerminalError):
                    rr.archive_result(raw, digest)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for filename, value in (("accepted.json", accepted), ("snapshot.json", accepted["snapshot"]), ("completion.json", accepted["completion"])):
                info = zipfile.ZipInfo(filename)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, json.dumps(value))
        raw = stream.getvalue()
        with self.assertRaises(_gh.TerminalError):
            rr.archive_result(raw, hashlib.sha256(raw).hexdigest())
        raw, digest = archive_of(accepted)
        with mock.patch.object(rr, "MAX_RESULT_BYTES", 10), self.assertRaises(_gh.TerminalError):
            rr.archive_result(raw, digest)

    def test_archive_record_mismatch_is_not_accepted(self):
        accepted = accepted_fixture(working_body(1))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("accepted.json", json.dumps(accepted))
            archive.writestr("snapshot.json", '{}')
            archive.writestr("completion.json", json.dumps(accepted["completion"]))
        raw = stream.getvalue()
        with self.assertRaises(_gh.TerminalError):
            rr.archive_result(raw, hashlib.sha256(raw).hexdigest())

    def test_redirect_does_not_forward_github_credential(self):
        request = urllib.request.Request("https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
                                         headers={"Authorization": "Bearer fixture"})
        redirected = rr._ArchiveRedirect().redirect_request(request, None, 302, "Found", {}, "https://blob.example/archive.zip")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(_gh.TerminalError):
            rr._ArchiveRedirect().redirect_request(request, None, 302, "Found", {}, "http://blob.example/archive.zip")

    def test_host_evidence_symlinks_hardlinks_checkout_and_size_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_text('{}')
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaises(_gh.TerminalError):
                rr.safe_file(link, 100)
            os.link(source, root / "hardlink")
            with self.assertRaises(_gh.TerminalError):
                rr.safe_file(source, 100)
            with mock.patch.dict(os.environ, {"REVIEW_RESULT_DIR": str(root / "result"), "RUNNER_TEMP": str(root), "GITHUB_WORKSPACE": str(root)}):
                with self.assertRaises(_gh.TerminalError):
                    rr.result_dir()


if __name__ == "__main__":
    unittest.main()
