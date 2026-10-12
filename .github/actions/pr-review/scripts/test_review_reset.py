#!/usr/bin/env python3
"""Reset-only fixtures; not live workflow, model-quality or token-isolation proof."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _gh
import test_verdict_scaffolding as baseline

_spec = importlib.util.spec_from_file_location("review_reset", Path(__file__).with_name("review-reset.py"))
reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset)
pub = reset.pub


class ResetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = dict(baseline.ENV, GITHUB_OUTPUT=str(self.directory / "outputs"),
                        REVIEW_RESET_FILE=str(self.directory / "accepted.json"),
                        REVIEW_RESET_SNAPSHOT=str(self.directory / "host-snapshot.json"),
                        REVIEW_EXECUTION_FILE=str(self.directory / "execution.json"),
                        REVIEW_SESSION_ID="current-session")
        self.comments = {}
        self.reviews = {}
        self.calls = []
        self.history_edits = {}
        self.ambiguous = False
        self.ambiguous_lands = True
        self.pr = {"state": "open", "head": {"sha": baseline.HEAD, "ref": "topic", "repo": {"full_name": "example/repo"}},
                   "base": {"sha": baseline.BASE, "ref": "main"}}
        self.runs = []
        self.inventory_incomplete = False
        self.run_attempt = baseline.RUN_ATTEMPT
        self.workflow_url = f"{_gh.API_ROOT}/repos/example/repo/actions/workflows/50"
        self.context = {"repository": "example/repo", "pr_number": "42", "current_sha": baseline.HEAD,
                        "current_base_sha": baseline.BASE, "current_base_ref": "main",
                        "workflow_ref": baseline.WORKFLOW_REF, "summary_heading": baseline.HEADING,
                        "existing_findings": ["- `file.go:7`: A prior issue"]}
        self.prior = [{"path": "file.go", "line": 7, "severity": "bug", "excerpt": "🟠 Bug: Original failure",
                       "thread_resolved": True, "thread_outdated": True},
                      {"path": "file.go", "line": 12, "severity": "suggestion", "excerpt": "🟡 Suggestion: Consider a test",
                       "thread_resolved": False, "thread_outdated": False}]
        self.old_review = self.add_old(10, 1)
        reset.write_json(self.directory / "context.json", self.context)
        self.real_read = reset.read_json
        for patch in (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(reset, "SNAPSHOT", self.directory / "snapshot.json"),
            mock.patch.object(pub, "PR_CONTEXT_PATH", self.directory / "context.json"),
            mock.patch.object(pub, "current_head_sha", return_value=baseline.HEAD),
            mock.patch.object(pub, "current_base_sha", return_value=baseline.BASE),
            mock.patch.object(_gh, "rest", side_effect=self.rest),
            mock.patch.object(_gh, "rest_paginate", side_effect=self.paginate),
            mock.patch.object(_gh, "graphql", side_effect=self.graphql),
            mock.patch.object(reset, "read_json", side_effect=self.read),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def read(self, path, *args):
        if str(path) == ".github/prior-findings.json":
            return {"prior_findings": copy.deepcopy(self.prior)}
        return self.real_read(path, *args)

    def add_old(self, review_id, report_id, findings=None):
        findings = findings or ["- **New** `file.go:7`: dereference can panic"]
        identity = dict(baseline.IDENTITY, run_id=str(100 + review_id), run_attempt="1")
        body = (baseline.working_body(len(findings)) + "\n### Security Issues\nNone found.\n"
                + "### Correctness Issues\n" + "\n".join(findings) + "\n### Suggestions\nNone.\n")
        working = {"id": 80 + report_id, "body": body, "updated_at": baseline.T1}
        report = {"id": report_id, "node_id": f"C{report_id}", "user": {"login": "github-actions[bot]"},
                  "body": pub.compose_report_body(working, identity, baseline.HEAD, baseline.BASE, baseline.T1),
                  "updated_at": baseline.T1, "html_url": f"https://example/report/{report_id}"}
        state = reset._review_state.marker_state(report["body"])
        state["publication"] = "completed"
        report["body"] = reset._review_state.REVIEW_STATE_PATTERN.sub(lambda m: f"<!-- review-state: {json.dumps(state)} -->", report["body"])
        value = dict(identity, report_comment_id=report_id)
        review = {"id": review_id, "node_id": f"R{review_id}", "user": {"login": "github-actions[bot]"},
                  "commit_id": baseline.HEAD, "state": "CHANGES_REQUESTED", "submitted_at": baseline.T1,
                  "body": f"Blocking issues found\n<!-- review-publication: {json.dumps(value)} -->"}
        self.comments[report_id] = report
        self.reviews[review_id] = review
        return review

    def paginate(self, path, **kwargs):
        self.calls.append(("LIST", path, None))
        if path.endswith("/comments"):
            return copy.deepcopy(list(self.comments.values()))
        if path.endswith("/reviews"):
            return copy.deepcopy(list(self.reviews.values()))
        if "/actions/workflows/" in path or "/actions/required_workflows/" in path:
            return [{"workflow_runs": copy.deepcopy(self.runs),
                     "total_count": len(self.runs) + int(self.inventory_incomplete)}]
        self.fail(path)

    def rest(self, method, path, **kwargs):
        data = kwargs.get("data")
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "GET":
            if path.endswith("/pulls/42"):
                return copy.deepcopy(self.pr)
            if "/actions/runs/" in path:
                return {"workflow_id": 50, "workflow_url": self.workflow_url, "run_attempt": self.run_attempt}
            if "/actions/workflows/" in path:
                return {"workflow_runs": copy.deepcopy(self.runs)}
            if "/issues/comments/" in path:
                return copy.deepcopy(self.comments[int(path.rsplit("/", 1)[1])])
            if "/reviews/" in path:
                return copy.deepcopy(self.reviews[int(path.rsplit("/", 1)[1])])
        if method == "POST" and path.endswith("/comments"):
            report = {"id": 900, "node_id": "C900", "user": {"login": "github-actions[bot]"},
                      "body": data["body"], "updated_at": baseline.FRESH, "html_url": "https://example/report/900"}
            self.comments[900] = report
            return copy.deepcopy(report)
        if method == "POST" and path.endswith("/reviews"):
            review = {"id": 1000, "node_id": "R1000", "user": {"login": "github-actions[bot]"},
                      "body": data["body"], "state": pub.REVIEW_STATE_FOR_EVENT[data["event"]],
                      "commit_id": data["commit_id"], "submitted_at": baseline.FRESH}
            self.reviews[1000] = review
            return copy.deepcopy(review)
        if method == "PATCH":
            report_id = int(path.rsplit("/", 1)[1])
            self.comments[report_id]["body"] = data["body"]
            self.history_edits[self.comments[report_id]["node_id"]] = [{"editor": {"login": "github-actions[bot]"}, "deletedAt": None, "deletedBy": None}]
            return copy.deepcopy(self.comments[report_id])
        if method == "PUT":
            self.assertEqual(kwargs.get("max_attempts"), 1)
            review_id = int(path.split("/")[-2])
            if not self.ambiguous or self.ambiguous_lands:
                self.reviews[review_id]["state"] = "DISMISSED"
            if self.ambiguous:
                raise _gh.TransientOutageError("response lost")
            return copy.deepcopy(self.reviews[review_id])
        self.fail(f"{method} {path}")

    def graphql(self, query, variables):
        item = next(item for item in list(self.comments.values()) + list(self.reviews.values())
                    if item["node_id"] == variables["id"])
        edits = self.history_edits.get(item["node_id"], [])
        return {"node": {"id": item["node_id"], "fullDatabaseId": str(item["id"]), "body": item["body"],
                         "author": item["user"], "lastEditedAt": baseline.FRESH if edits else None,
                         "editor": edits[-1]["editor"] if edits else None, "includesCreatedEdit": bool(edits),
                         "pullRequest": {"number": 42, "repository": {"nameWithOwner": "example/repo"}},
                         "userContentEdits": {"totalCount": len(edits), "nodes": edits, "pageInfo": {"hasNextPage": False}}}}

    def freeze(self):
        reset.prepare()
        return self.real_read(reset.SNAPSHOT)

    def proof(self, snapshot):
        return {"run_id": baseline.RUN_ID, "run_attempt": baseline.RUN_ATTEMPT,
                "head_sha": baseline.HEAD, "base_sha": baseline.BASE, "review_complete": True,
                "coverage_gaps": [], "blocking_issues": 0,
                "audit": [{"id": entry["id"], "disposition": "fixed",
                           "evidence": [{"path": "file.go", "location": "7-12", "explanation": "Current nil guard resolves the failure"}]}
                          for entry in snapshot["audit"]]}

    def capture(self, snapshot, proof=None, terminal_extra=None):
        terminal = {"type": "result", "subtype": "success", "is_error": False,
                    "session_id": "current-session",
                    "result": "Review finished.\n```review-reset-proof\n" + json.dumps(proof or self.proof(snapshot)) + "\n```"}
        terminal.update(terminal_extra or {})
        reset.write_json(self.env["REVIEW_EXECUTION_FILE"], [{"type": "system", "subtype": "init", "session_id": "current-session"}, terminal])
        reset.capture()
        path = Path(self.env["REVIEW_RESET_FILE"])
        os.environ.update(REVIEW_RESET_SHA256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          REVIEW_RESET_UPLOAD_OUTCOME="success", REVIEW_RESET_ARTIFACT_ID="777",
                          REVIEW_RESET_ARTIFACT_DIGEST="b" * 64)
        self.calls.append(("UPLOAD", "accepted.json", None))
        return self.real_read(path)

    def publish(self, blockers=0, provisional=False):
        self.comments[2] = {"id": 2, "node_id": "C2", "user": {"login": "github-actions[bot]"},
                            "body": baseline.working_body(blockers, provisional=provisional), "updated_at": baseline.FRESH}
        pub._run()

    def puts(self):
        return [call for call in self.calls if call[0] == "PUT"]

    def test_complete_current_audit_dismisses_only_exact_targets_after_publication(self):
        self.add_old(11, 3)
        snapshot = self.freeze()
        self.assertEqual({entry["id"] for entry in snapshot["audit"]}, {"review-10-0", "review-11-0", "prior-0", "prior-1"})
        self.capture(snapshot)
        self.assertFalse(self.puts())
        self.publish()
        reset.reset()
        self.assertEqual([call[1] for call in self.puts()], ["repos/example/repo/pulls/42/reviews/10/dismissals", "repos/example/repo/pulls/42/reviews/11/dismissals"])
        first_put = next(index for index, call in enumerate(self.calls) if call[0] == "PUT")
        self.assertTrue(all(index < first_put for index, call in enumerate(self.calls) if call[0] in ("UPLOAD", "POST", "PATCH")))
        self.assertEqual(self.reviews[1000]["state"], "COMMENTED")
        self.assertEqual(reset._review_state.marker_state(self.comments[900]["body"])["publication"], "completed")
        reset.reset()  # Exact same attempt retry does not recreate or re-dismiss.
        self.assertEqual(len(self.puts()), 2)

    def test_no_prior_block_is_noop(self):
        self.reviews.clear()
        snapshot = self.freeze()
        self.assertEqual(snapshot["targets"], [])
        self.assertIn("has_targets=false\n", Path(self.env["GITHUB_OUTPUT"]).read_text())
        with self.assertRaises(_gh.TerminalError):
            reset.capture()
        self.assertFalse(Path(self.env["REVIEW_RESET_FILE"]).exists())
        self.publish()
        self.assertEqual(self.reviews[1000]["state"], "COMMENTED")
        self.assertFalse(self.puts())

    def test_reset_failures_do_not_break_ordinary_publication(self):
        snapshot = self.freeze()
        for extra in ({"result": "No optional proof"}, {"result": "```review-reset-proof\n{}\n```"},
                      {"subtype": "error_max_turns"}, {"session_id": "old-session"}):
            with self.subTest(extra=extra), self.assertRaises((_gh.TerminalError, ValueError)):
                self.capture(snapshot, terminal_extra=extra)
        self.publish()
        self.assertEqual(self.reviews[1000]["state"], "COMMENTED")
        self.assertEqual(reset._review_state.marker_state(self.comments[900]["body"])["publication"], "completed")
        self.assertFalse(self.puts())

    def test_failed_prepare_retains_and_publisher_still_requests_changes(self):
        self.prior = None
        with self.assertRaises(_gh.TerminalError):
            self.freeze()
        self.publish(1)
        self.assertEqual(self.reviews[1000]["state"], "CHANGES_REQUESTED")
        self.assertFalse(self.puts())

    def test_every_audit_id_and_evidence_is_required(self):
        snapshot = self.freeze()
        proof = self.proof(snapshot)
        mutations = []
        for key, value in (("review_complete", False), ("coverage_gaps", ["unread diff"]), ("blocking_issues", 1),
                           ("head_sha", "old"), ("base_sha", "old"), ("run_attempt", "1"), ("extra", "secret")):
            changed = copy.deepcopy(proof)
            changed[key] = value
            mutations.append(changed)
        for change in ("missing", "duplicate", "unknown", "unverified", "still_present", "empty_evidence", "empty_explanation"):
            changed = copy.deepcopy(proof)
            if change == "missing":
                changed["audit"].pop()
            elif change == "duplicate":
                changed["audit"][1] = changed["audit"][0]
            elif change == "unknown":
                changed["audit"][0]["id"] = "unfrozen"
            elif change in ("unverified", "still_present"):
                changed["audit"][0]["disposition"] = change
            elif change == "empty_evidence":
                changed["audit"][0]["evidence"] = []
            else:
                changed["audit"][0]["evidence"][0]["explanation"] = ""
            mutations.append(changed)
        for changed in mutations:
            with self.subTest(proof=changed), self.assertRaises(_gh.TerminalError):
                reset.validate_proof(changed, snapshot)
        # An audited known suggestion need not be fixed to clear blocking reviews.
        proof["audit"][2]["disposition"] = "still_present"
        reset.validate_proof(proof, snapshot)

    def test_upload_failure_or_changed_accepted_file_retains_after_clean_publication(self):
        self.capture(self.freeze())
        self.publish()
        for extra in ({"REVIEW_RESET_UPLOAD_OUTCOME": "failure"}, {"REVIEW_RESET_ARTIFACT_ID": ""},
                      {"REVIEW_RESET_ARTIFACT_DIGEST": ""}, {"REVIEW_RESET_SHA256": "c" * 64}):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, extra), self.assertRaises(_gh.TerminalError):
                reset.reset()
        self.assertEqual(self.reviews[1000]["state"], "COMMENTED")
        self.assertFalse(self.puts())

    def test_new_blocking_review_and_incomplete_summary_never_reset(self):
        accepted = self.capture(self.freeze())
        with self.assertRaises(SystemExit):
            self.publish(provisional=True)
        self.assertNotIn(1000, self.reviews)
        self.publish(1)
        with self.assertRaises(_gh.TerminalError):
            reset.dismiss(accepted)
        self.assertFalse(self.puts())

    def test_unknown_human_foreign_targets_are_not_adopted(self):
        unknown = self.add_old(20, 3)
        unknown["body"] = "legacy unmarked review"
        human = self.add_old(21, 4)
        human["user"] = {"login": "maintainer"}
        foreign = self.add_old(22, 5)
        foreign["body"] = foreign["body"].replace(baseline.HEADING, baseline.rs.GENERAL_REVIEW_SUMMARY_HEADING)
        self.assertEqual([target["id"] for target in self.freeze()["targets"]], [10])
        self.capture(self.real_read(reset.SNAPSHOT))
        self.publish()
        reset.reset()
        self.assertEqual({review_id for review_id, review in self.reviews.items() if review["state"] == "DISMISSED"}, {10})

    def test_original_missing_edited_or_unaccounted_evidence_never_vacuously_succeeds(self):
        original = copy.deepcopy(self.comments[1])
        for change in ("missing", "human_edit", "deleted_edit", "review_edit", "missing_findings", "missing_history"):
            self.comments[1] = copy.deepcopy(original)
            self.history_edits.clear()
            patch = mock.patch.object(_gh, "graphql", side_effect=self.graphql)
            if change == "missing":
                del self.comments[1]
            elif change in ("human_edit", "deleted_edit", "review_edit"):
                self.history_edits["R10" if change == "review_edit" else "C1"] = [{"editor": {"login": "human" if change == "human_edit" else "github-actions[bot]"},
                                                                                 "deletedAt": baseline.FRESH if change == "deleted_edit" else None, "deletedBy": None}]
            elif change == "missing_findings":
                self.comments[1]["body"] = self.comments[1]["body"].replace("- **New** `file.go:7`: dereference can panic", "None found.")
            else:
                patch = mock.patch.object(_gh, "graphql", return_value={"node": None})
            with self.subTest(change=change), patch:
                snapshot = self.freeze()
                self.assertEqual(snapshot["targets"], [])
                self.assertEqual([entry["id"] for entry in snapshot["retained"]], [10])

    def test_stale_head_base_and_newer_run_retain(self):
        accepted = self.capture(self.freeze())
        self.publish()
        for change in ("head", "base", "base_ref", "newer_run", "newer_attempt", "inventory"):
            pr = copy.deepcopy(self.pr)
            self.runs = []
            self.inventory_incomplete = False
            self.run_attempt = baseline.RUN_ATTEMPT
            if change in ("head", "base"):
                self.pr[change]["sha"] = "changed"
            elif change == "base_ref":
                self.pr["base"]["ref"] = "other"
            elif change == "newer_run":
                self.runs = [{"id": 9999, "head_sha": baseline.HEAD, "pull_requests": [{"number": 42}], "run_started_at": baseline.T2}]
            elif change == "newer_attempt":
                self.run_attempt = "3"
            else:
                self.inventory_incomplete = True
            with self.subTest(change=change), self.assertRaises(_gh.TerminalError):
                reset.dismiss(accepted)
            self.pr = pr
        self.assertFalse(self.puts())

    def test_original_changed_after_proof_retains(self):
        accepted = self.capture(self.freeze())
        self.publish()
        self.comments[1]["body"] = self.comments[1]["body"].replace("dereference can panic", "different obligation")
        with self.assertRaises(_gh.TerminalError):
            reset.dismiss(accepted)
        self.assertFalse(self.puts())

    def test_ambiguous_response_reconciles_exact_id_without_retry(self):
        self.capture(self.freeze())
        self.publish()
        self.ambiguous = True
        reset.reset()
        self.assertEqual(self.reviews[10]["state"], "DISMISSED")
        self.assertEqual(len(self.puts()), 1)

    def test_ambiguous_response_without_dismissal_retains(self):
        self.capture(self.freeze())
        self.publish()
        self.ambiguous = True
        self.ambiguous_lands = False
        with self.assertRaises(_gh.TerminalError):
            reset.reset()
        self.assertEqual(self.reviews[10]["state"], "CHANGES_REQUESTED")
        self.assertEqual(len(self.puts()), 1)

    def test_sanitization_redacts_all_new_proof_and_snapshot_strings(self):
        token = "ghp_" + "a" * 36
        self.prior[0]["excerpt"] += token
        snapshot = self.freeze()
        proof = self.proof(snapshot)
        proof["audit"][0]["evidence"][0]["explanation"] += token + "<!-- hidden -->"
        accepted = self.capture(snapshot, proof)
        raw = Path(self.env["REVIEW_RESET_FILE"]).read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn("<!-- hidden -->", raw)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", raw)
        reset.validate_proof(accepted["proof"], snapshot)

    def test_scraped_suggestions_and_fenced_context_do_not_block_reset(self):
        suggestion = "- **Prior — still present** `file.go:12`: consider a test"
        self.context["existing_findings"] = [suggestion, "- Around line 12: Add a test in `file.go`."]
        reset.write_json(self.directory / "context.json", self.context)
        self.comments[1]["body"] = self.comments[1]["body"].replace(
            "### Suggestions\nNone.", "### Suggestions\n" + suggestion
            + "\n```\n- Around line 12: Add a test in `file.go`.\n```"
        )
        snapshot = self.freeze()
        self.assertEqual({entry["id"] for entry in snapshot["audit"]}, {"review-10-0", "prior-0", "prior-1"})
        self.assertEqual(self.real_read(self.directory / "context.json")["existing_findings"], self.context["existing_findings"])
        proof = self.proof(snapshot)
        next(item for item in proof["audit"] if item["id"] == "prior-1")["disposition"] = "still_present"
        self.capture(snapshot, proof)
        self.publish()
        reset.reset()
        self.assertEqual(self.reviews[10]["state"], "DISMISSED")

    def test_target_ownership_changed_after_freeze_is_retained(self):
        accepted = self.capture(self.freeze())
        self.publish()
        self.reviews[10]["user"] = {"login": "human"}
        with self.assertRaises(_gh.TerminalError):
            reset.dismiss(accepted)
        self.assertFalse(self.puts())

    def test_newer_formal_publication_or_deleted_current_result_retains(self):
        accepted = self.capture(self.freeze())
        self.publish()
        other = self.add_old(12, 3)
        other["submitted_at"] = baseline.T2
        with self.assertRaises(_gh.TerminalError):
            reset.dismiss(accepted)
        del self.reviews[12]
        del self.reviews[1000]
        with self.assertRaises(_gh.TerminalError):
            reset.dismiss(accepted)
        self.assertFalse(self.puts())

    def test_malformed_native_record_duplicate_proof_and_unknown_fields_retain(self):
        snapshot = self.freeze()
        proof = json.dumps(self.proof(snapshot))
        for text in (
            "```review-reset-proof\n" + proof[:-1] + ',"blocking_issues":0}\n```',
            "```review-reset-proof\n" + proof + "\n```\n```review-reset-proof\n" + proof + "\n```",
            "```review-reset-proof\n" + proof[:-1] + ',"extra_secret":"not permitted"}\n```',
        ):
            with self.subTest(text=text), self.assertRaises(_gh.TerminalError):
                self.capture(snapshot, terminal_extra={"result": text})
        reset.write_json(self.env["REVIEW_EXECUTION_FILE"], {"result": proof})
        with self.assertRaises(_gh.TerminalError):
            reset.capture()
        self.assertFalse(self.puts())

    def test_only_eligible_original_targets_clear_when_another_is_unverifiable(self):
        self.add_old(11, 3)
        del self.comments[3]
        snapshot = self.freeze()
        self.assertEqual([item["id"] for item in snapshot["retained"]], [11])
        self.capture(snapshot)
        self.publish()
        reset.reset()
        self.assertEqual(self.reviews[10]["state"], "DISMISSED")
        self.assertEqual(self.reviews[11]["state"], "CHANGES_REQUESTED")

    def test_required_workflow_inventory_supports_late_exact_dismissal(self):
        self.capture(self.freeze())
        self.publish()
        self.workflow_url = f"{_gh.API_ROOT}/repos/example/repo/actions/required_workflows/50"
        reset.reset()
        self.assertEqual(self.reviews[10]["state"], "DISMISSED")
        self.assertIn(("LIST", self.workflow_url + "/runs?branch=topic", None), self.calls)

    def test_workflow_inventory_url_must_be_exact_api_and_repository(self):
        snapshot = self.freeze()
        prefix = f"{_gh.API_ROOT}/repos/example/repo/actions/"
        for url in (None, "", "https://github.com/example/repo/actions/workflows/50",
                    "https://api.github.com.evil/repos/example/repo/actions/workflows/50",
                    "https://foreign.example/repos/example/repo/actions/workflows/50",
                    f"{_gh.API_ROOT}/repos/foreign/repo/actions/workflows/50",
                    prefix + "workflows/not-numeric", prefix + "required_workflows/50?repo=foreign",
                    prefix + "required_workflows/50#fragment", prefix + "workflows/50/runs",
                    prefix + "workflows/../50"):
            self.workflow_url = url
            first_call = len(self.calls)
            with self.subTest(url=url), self.assertRaises(_gh.TerminalError):
                reset.check_newer_runs("example/repo", "42", snapshot)
            self.assertFalse(any(call[0] == "LIST" for call in self.calls[first_call:]))

    def test_both_workflow_url_shapes_preserve_complete_pagination_accounting(self):
        snapshot = self.freeze()
        for kind in ("workflows", "required_workflows"):
            self.workflow_url = f"{_gh.API_ROOT}/repos/example/repo/actions/{kind}/50"
            pages = [{"total_count": 2, "workflow_runs": [{"id": 1, "run_started_at": baseline.T1}]},
                     {"total_count": 2, "workflow_runs": [{"id": 2, "run_started_at": baseline.T1}]}]
            with self.subTest(kind=kind), mock.patch.object(_gh, "rest_paginate", return_value=pages) as paginate:
                reset.check_newer_runs("example/repo", "42", snapshot)
                paginate.assert_called_once_with(self.workflow_url + "/runs?branch=topic")
            with mock.patch.object(_gh, "rest_paginate", return_value=pages[:1]), self.assertRaises(_gh.TerminalError):
                reset.check_newer_runs("example/repo", "42", snapshot)

    def test_new_json_writes_replace_symlink_without_touching_sentinel(self):
        sentinel = self.directory / "sentinel.txt"
        sentinel.write_text("must remain unchanged")
        destination = self.directory / "owned.json"
        destination.symlink_to(sentinel)
        reset.write_json(destination, {"current": True})
        self.assertEqual(sentinel.read_text(), "must remain unchanged")
        self.assertFalse(destination.is_symlink())
        self.assertEqual(self.real_read(destination), {"current": True})

    def test_failed_atomic_write_preserves_existing_file_and_removes_temporary(self):
        destination = self.directory / "owned.json"
        destination.write_text("old contents")
        before = set(self.directory.iterdir())
        with mock.patch.object(reset.os, "replace", side_effect=OSError("replace failed")), self.assertRaises(OSError):
            reset.write_json(destination, {"current": True})
        self.assertEqual(destination.read_text(), "old contents")
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_failed_prepare_clears_planted_snapshot_and_preserves_normal_context(self):
        context = self.directory / "context.json"
        original = context.read_bytes()
        reset.SNAPSHOT.symlink_to(context)
        host = Path(self.env["REVIEW_RESET_SNAPSHOT"])
        reset.write_json(host, {"stale": True})
        self.prior = None
        with self.assertRaises(_gh.TerminalError):
            self.freeze()
        self.assertEqual(context.read_bytes(), original)
        self.assertFalse(reset.SNAPSHOT.exists())
        self.assertFalse(reset.SNAPSHOT.is_symlink())
        self.assertFalse(host.exists())
        # A new workspace-only fake cannot replace the absent host input.
        reset.write_json(reset.SNAPSHOT, {"fake": True})
        with self.assertRaises(FileNotFoundError):
            reset.capture()
        self.publish()
        self.assertEqual(self.reviews[1000]["state"], "COMMENTED")
        self.assertFalse(self.puts())

    def test_capture_uses_host_snapshot_not_mutable_workspace_copy(self):
        snapshot = self.freeze()
        changed = copy.deepcopy(snapshot)
        changed["audit"] = []
        reset.write_json(reset.SNAPSHOT, changed)
        with self.assertRaises(_gh.TerminalError):
            self.capture(changed)
        accepted = self.capture(snapshot)
        self.assertEqual(accepted["snapshot"], snapshot)


class ActionContractTest(unittest.TestCase):
    def test_original_model_options_and_reset_failure_split(self):
        action = Path(__file__).parents[1].joinpath("action.yml").read_text()
        args = next(line.strip() for line in action.splitlines() if line.strip().startswith("claude_args:"))
        self.assertEqual(args, 'claude_args: --model claude-opus-5-5 --max-turns 100 --setting-sources user --strict-mcp-config --disallowedTools "Skill,ScheduleWakeup,CronCreate,CronDelete,CronList" --allowedTools "Read,Glob,Grep,Task,mcp__github_inline_comment__create_inline_comment,mcp__github_comment__update_claude_comment,Bash(gh pr diff:*),Bash(gh pr view:*),Bash(gh api:*)"')
        self.assertNotIn("--json-schema", action)
        steps = action.split("\n  - name: ")[1:]
        publisher = next(step for step in steps if step.startswith("Publish review report"))
        self.assertIn("if: steps.claude_review.conclusion == 'success'", publisher)
        self.assertNotIn("reset-", publisher)
        for name in ("Freeze prior", "Capture optional", "Persist accepted", "Dismiss exactly"):
            step = next(step for step in steps if step.startswith(name))
            self.assertIn("continue-on-error: true", step)
        dismiss = steps[-1]
        self.assertIn("steps.reset-upload.outcome == 'success'", dismiss)
        self.assertNotIn("reset-upload.conclusion", dismiss)
        self.assertNotIn("retention-days:", next(step for step in steps if step.startswith("Persist accepted")))
        capture = next(step for step in steps if step.startswith("Capture optional"))
        self.assertIn("steps.reset-prepare.outputs.has_targets == 'true'", capture)


if __name__ == "__main__":
    unittest.main()
