#!/usr/bin/env python3
"""Optional, fail-closed self-dismissal after the unchanged baseline publisher.

The terminal runner proof plus the frozen inputs is the reset authority, not
editable report prose. This does not isolate the model's existing GitHub token.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote

import _gh
import _review_state

_spec = importlib.util.spec_from_file_location(
    "reset_publisher", Path(__file__).with_name("publish-review-report.py")
)
pub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pub)
SNAPSHOT = Path(".github/review-reset.json")
PROOF_PATTERN = re.compile(r"```review-reset-proof\s*\n(.*?)\n```\s*$", re.DOTALL)
REVIEWER_KEYS = ("workflow_ref", "summary_marker", "verdict_mode")


def require(condition, message):
    if not condition:
        raise _gh.TerminalError(message)


def read_json(path, maximum=2 * 1024 * 1024):
    raw = Path(path).read_bytes()
    require(len(raw) <= maximum, "reset input exceeds size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def write_json(path, value):
    """Replace only our destination entry, never follow a planted file symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sanitize(text):
    """Pinned native comment sanitizer/redactor rules, also for proof strings."""
    for _ in range(16):
        previous = text
        text = re.sub(r"<!--[\s\S]*?-->", "", text)
        text = re.sub(r"[\u200b\u200c\u200d\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u202a-\u202e\u2066-\u2069]", "", text)
        text = re.sub(r"!\[[^\]]*\]\(", "![](", text)
        text = re.sub(r"!\[[^\]]*\](\[[^\]]*\])", r"![]\1", text)
        text = re.sub(r'''(\[[^\]]*\]\([^)]+)\s+["'][^"']*["']''', r"\1", text)
        for attribute in ("alt", "title", "aria-label", r"data-[a-zA-Z0-9-]+", "placeholder"):
            for value in (r'''"[^"]*"''', r"'[^']*'", r"[^\s>]+"):
                text = re.sub(r"\s" + attribute + r"\s*=\s*" + value, "", text, flags=re.I)
        def entity(match):
            raw = match[0][2:-1]
            number = int(raw[1:], 16) if raw.startswith("x") else int(raw)
            return chr(number) if 32 <= number <= 126 else ""
        text = re.sub(r"&#(?:x[0-9a-fA-F]+|\d+);", entity, text)
        text = re.sub(r"gh[pousr]_[A-Za-z0-9]{36}\b|github_pat_[A-Za-z0-9_]{11,221}\b", "[REDACTED_GITHUB_TOKEN]", text)
        text = re.sub(r"sk-ant-[A-Za-z0-9_-]{20,}", "[REDACTED_ANTHROPIC_KEY]", text)
        text = re.sub(r'''(\b|\\[nrtbf"\\/]|\\u[0-9a-fA-F]{4}|\[[0-9;]*m)(?:AKIA|ASIA)[A-Z0-9]{16}\b''', lambda m: m[1] + "[REDACTED_AWS_KEY_ID]", text)
        text = re.sub(r"xox[abpsr]-[A-Za-z0-9-]{10,}", "[REDACTED_SLACK_TOKEN]", text)
        text = re.sub(r"eyJ[A-Za-z0-9_-]{10,2000}\.eyJ[A-Za-z0-9_-]{10,4000}\.[A-Za-z0-9_-]{10,2000}\b", "[REDACTED_JWT]", text)
        if text == previous:
            return text
    raise _gh.TerminalError("reset evidence sanitization did not converge")


def sanitized(value):
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()}
    return value


def marker(review):
    matches = pub.VERDICT_MARKER_PATTERN.findall(review.get("body") or "")
    if len(matches) != 1:
        return None
    try:
        value = json.loads(matches[0])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def owned(review, identity):
    value = marker(review)
    return ((review.get("user") or {}).get("login") in _review_state.BOT_LOGINS
            and value is not None
            and all(value.get(key) == identity[key] for key in REVIEWER_KEYS))


def check_history(item, kind, repo, pr_number):
    """Unknown/deleted/human edits retain; visible host-only report edits are OK."""
    require(item.get("node_id"), "missing original edit history")
    query = """query($id:ID!) { node(id:$id) { ... on KIND {
      id fullDatabaseId body author { login } lastEditedAt editor { login } includesCreatedEdit
      pullRequest { number repository { nameWithOwner } }
      userContentEdits(first:100) { totalCount
        nodes { editor { login } deletedAt deletedBy { login } }
        pageInfo { hasNextPage }
      }
    } } }""".replace("KIND", kind)
    node = _gh.graphql(query, {"id": item["node_id"]}).get("node")
    require(isinstance(node, dict) and node.get("id") == item["node_id"]
            and str(node.get("fullDatabaseId")) == str(item["id"])
            and node.get("body") == item.get("body"), "original history binding mismatch")
    binding = node.get("pullRequest") or {}
    require(str(binding.get("number")) == str(pr_number)
            and (binding.get("repository") or {}).get("nameWithOwner") == repo
            and (node.get("author") or {}).get("login") in _review_state.BOT_LOGINS,
            "foreign original evidence")
    history = node.get("userContentEdits") or {}
    edits = history.get("nodes")
    require(isinstance(edits, list) and type(history.get("totalCount")) is int
            and history["totalCount"] == len(edits)
            and (history.get("pageInfo") or {}).get("hasNextPage") is False,
            "incomplete original edit history")
    require(all(isinstance(edit, dict) and edit.get("deletedAt") is None
                and edit.get("deletedBy") is None
                and (edit.get("editor") or {}).get("login") in _review_state.BOT_LOGINS
                for edit in edits), "deleted/unknown/human original edit")
    require(all(key in node for key in ("lastEditedAt", "editor", "includesCreatedEdit")),
            "missing original edit metadata")
    if kind == "PullRequestReview" or node["lastEditedAt"] is None:
        require(not edits and node["lastEditedAt"] is None and node["editor"] is None
                and node["includesCreatedEdit"] is False, "formal review edited or unknown history")
    else:
        require(edits and node["includesCreatedEdit"] is True
                and (node["editor"] or {}).get("login") in _review_state.BOT_LOGINS,
                "inconsistent original report history")


def blocking_findings(body):
    """Group canonical top-level items without losing their supporting text."""
    sections = ("### Security Issues", "### Correctness Issues")
    groups = []
    current = None
    section = None
    seen = []
    empty = False
    fence_char = None
    fence_len = 0
    error = "original blocking findings missing or not count-accounted"
    for line in body.splitlines():
        # Preserve fences in a finding, including indented list-item examples.
        # Their bullets/headings are evidence, never new findings or sections.
        if fence_char is not None:
            if section in sections:
                require(current is not None, error)
                current.append(line)
            closer = pub._FENCE_CLOSE_PATTERN.match(line.lstrip(" \t"))
            if closer:
                delimiter = closer.group(1)
                if delimiter[0] == fence_char and len(delimiter) >= fence_len:
                    fence_char = None
            continue
        opener = pub._FENCE_OPEN_PATTERN.match(line.lstrip(" \t"))
        if opener and not (opener[1][0] == "`" and "`" in opener[2]):
            fence_char, fence_len = opener[1][0], len(opener[1])
            if section in sections:
                require(current is not None, error)
                current.append(line)
            continue
        if re.match(r"#{1,3}[ \t]+", line):
            section = line.strip()
            current = None
            empty = False
            if section in sections:
                seen.append(section)
            continue
        if section not in sections:
            continue
        if not line.strip():
            if current is not None:
                current.append(line)
            continue
        if line == "None found.":
            require(current is None and not empty, error)
            empty = True
            continue
        require(not empty, error)
        if re.match(r"(?:[-+*]|\d+[.)])[ \t]+\S", line):
            current = [line]
            groups.append(current)
        else:
            # Wrapped prose, nested items and quotes must have a real owner.
            require(current is not None, error)
            current.append(line)
    require(fence_char is None
            and sorted(seen) == ["### Correctness Issues", "### Security Issues"]
            and groups, error)
    return ["\n".join(group).strip() for group in groups]


def target_evidence(review, report, identity, repo, pr_number):
    require(owned(review, identity), "unknown target ownership")
    check_history(review, "PullRequestReview", repo, pr_number)
    check_history(report, "IssueComment", repo, pr_number)
    require(len(_review_state.REVIEW_STATE_PATTERN.findall(report.get("body", ""))) == 1,
            "ambiguous original report marker")
    state = _review_state.marker_state(report.get("body", "")) or {}
    value = marker(review)
    require(state.get("publication") == "completed"
            and all(state.get(key) == value.get(key) and value.get(key)
                    for key in pub.publication_identity())
            and state.get("last_reviewed_sha") == review.get("commit_id"),
            "original report/review binding mismatch")
    require(pub._parse_ts(state["started_at"]) < pub.run_started_at(), "target is not older")
    body = report["body"]
    rows = blocking_findings(body)
    require(pub.parse_blocking_count(body, identity["summary_marker"]) == len(rows),
            "original blocking findings missing or not count-accounted")
    return {"id": review["id"], "commit_id": review["commit_id"],
            "marker": value, "report_id": report["id"], "findings": rows}


def check_pr(pr, snapshot):
    context = snapshot["context"]
    require(pr.get("state") == "open" and pr["head"]["sha"] == context["current_sha"]
            and pr["base"]["sha"] == context["current_base_sha"]
            and pr["base"].get("ref") == context["current_base_ref"]
            and pr["head"].get("ref") == context["head_ref"]
            and (pr["head"].get("repo") or {}).get("full_name") == context["repository"],
            "PR closed or reviewed head/base/repository changed")


def prepare():
    # Clear only these new owned entries before any fallible preparation. A
    # planted workspace snapshot must not masquerade as this run's input.
    SNAPSHOT.unlink(missing_ok=True)
    host_snapshot = Path(os.environ["REVIEW_RESET_SNAPSHOT"])
    host_snapshot.unlink(missing_ok=True)
    context = read_json(pub.PR_CONTEXT_PATH)
    identity = pub.publication_identity()
    repo, number = context["repository"], context["pr_number"]
    require(repo == os.environ["GITHUB_REPOSITORY"] and str(number) == os.environ["PR_NUMBER"]
            and context["workflow_ref"] == identity["workflow_ref"]
            and context["summary_heading"] == identity["summary_marker"]
            and context["current_sha"] == pub.current_head_sha(), "reset context binding mismatch")
    snapshot = {"identity": identity, "context": {key: context[key] for key in (
        "repository", "pr_number", "current_sha", "current_base_sha", "current_base_ref")},
        "started_at": pub.run_started_raw(), "targets": [], "audit": [], "retained": []}
    pr = _gh.rest("GET", f"repos/{repo}/pulls/{number}")
    require(isinstance(pr["head"].get("ref"), str) and pr["head"]["ref"], "missing PR head branch")
    snapshot["context"]["head_ref"] = pr["head"]["ref"]
    check_pr(pr, snapshot)
    reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{number}/reviews")
    comments = {item["id"]: item for item in _gh.rest_paginate(f"repos/{repo}/issues/{number}/comments")}
    for review in reviews:
        if review.get("state") != "CHANGES_REQUESTED":
            continue
        if not owned(review, identity):
            # Human and foreign reviews are not ours, including unmarked legacy bots.
            continue
        try:
            report = comments[marker(review)["report_comment_id"]]
            target = target_evidence(review, report, identity, repo, number)
        except (_gh.GitHubError, KeyError, TypeError, ValueError) as error:
            snapshot["retained"].append({"id": review["id"], "reason": str(error)})
            continue
        snapshot["targets"].append(target)
        for index, finding in enumerate(target["findings"]):
            snapshot["audit"].append({"id": f"review-{review['id']}-{index}", "finding": finding,
                                      "blocking": True})
    # Include every supplied prior entry, not only target-associated or unresolved ones.
    prior = read_json(".github/prior-findings.json")["prior_findings"]
    require(isinstance(prior, list), "missing current prior-finding inventory")
    for index, finding in enumerate(prior):
        require(isinstance(finding, dict) and finding.get("excerpt") and finding.get("severity")
                in ("security", "bug", "suggestion"), "incomplete prior finding")
        snapshot["audit"].append({"id": f"prior-{index}", "finding": finding,
                                  "blocking": finding["severity"] != "suggestion"})
    snapshot = sanitized(snapshot)
    write_json(SNAPSHOT, snapshot)
    write_json(host_snapshot, snapshot)
    name = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"has_targets={'true' if snapshot['targets'] else 'false'}\n")
        output.write(f"artifact_name=pr-review-reset-{identity['run_id']}-{identity['run_attempt']}-{name}\n")
    print(f"Reset snapshot: {len(snapshot['targets'])} eligible, {len(snapshot['retained'])} retained; unmarked legacy reviews require manual dismissal")
    for retained in snapshot["retained"]:
        reason = " ".join(retained["reason"].split())[:240]
        print(f"Retained prior owned blocking review {retained['id']}: {reason}", file=sys.stderr)


def validate_proof(proof, snapshot):
    fields = {"run_id", "run_attempt", "head_sha", "base_sha", "review_complete",
              "coverage_gaps", "blocking_issues", "audit"}
    require(isinstance(proof, dict) and set(proof) == fields and proof.get("review_complete") is True
            and proof.get("coverage_gaps") == [] and type(proof.get("blocking_issues")) is int
            and proof["blocking_issues"] == 0, "incomplete coverage or remaining blockers")
    binding = {"run_id": snapshot["identity"]["run_id"], "run_attempt": snapshot["identity"]["run_attempt"],
               "head_sha": snapshot["context"]["current_sha"], "base_sha": snapshot["context"]["current_base_sha"]}
    require(all(proof.get(key) == value for key, value in binding.items()), "terminal proof run/head/base mismatch")
    audit = proof.get("audit")
    require(isinstance(audit, list) and all(isinstance(item, dict) for item in audit), "missing current audit")
    expected = {item["id"]: item for item in snapshot["audit"]}
    require(len(expected) == len(snapshot["audit"]), "duplicate frozen audit IDs")
    require(len(audit) == len(expected) and {item.get("id") for item in audit} == set(expected),
            "current audit does not cover every frozen ID exactly once")
    for item in audit:
        require(set(item) == {"id", "disposition", "evidence"}, "unexpected audit fields")
        allowed = ("fixed", "obsolete", "incorrect") if expected[item["id"]]["blocking"] else ("fixed", "obsolete", "incorrect", "still_present")
        require(item.get("disposition") in allowed, "blocking/unverified prior obligation remains")
        evidence = item.get("evidence")
        require(isinstance(evidence, list) and evidence and all(isinstance(entry, dict)
                and set(entry) == {"path", "location", "explanation"}
                and all(isinstance(entry.get(key), str) and entry[key].strip()
                        for key in ("path", "location", "explanation")) for entry in evidence),
                "missing affirmative current-code evidence")
    return proof


def capture():
    snapshot = read_json(os.environ["REVIEW_RESET_SNAPSHOT"])
    require(snapshot["targets"], "no eligible targets need reset proof")
    require(snapshot["identity"] == pub.publication_identity(), "stale reset snapshot")
    messages = read_json(os.environ["REVIEW_EXECUTION_FILE"], 32 * 1024 * 1024)
    require(isinstance(messages, list) and messages and isinstance(messages[-1], dict), "missing terminal runner result")
    terminal = messages[-1]
    session = os.environ.get("REVIEW_SESSION_ID")
    init = [item for item in messages if isinstance(item, dict) and item.get("type") == "system" and item.get("subtype") == "init"]
    require(session and len(init) == 1 and init[0].get("session_id") == session
            and terminal.get("session_id") == session and terminal.get("type") == "result"
            and terminal.get("subtype") == "success" and terminal.get("is_error") is False,
            "unsuccessful or unbound terminal result")
    text = terminal.get("result")
    require(isinstance(text, str) and text.count("```review-reset-proof") == 1, "missing/ambiguous optional terminal proof")
    match = PROOF_PATTERN.search(text)
    require(match is not None, "malformed terminal proof")
    # Parse via the same duplicate-key rejecting reader without a second evidence file.
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate proof key")
            result[key] = value
        return result
    proof = validate_proof(sanitized(json.loads(match[1], object_pairs_hook=pairs)), snapshot)
    accepted = {"snapshot": snapshot, "proof": proof, "completion": {"session_id": session, "terminal_success": True}}
    require(len(json.dumps(accepted).encode()) <= 2 * 1024 * 1024, "accepted reset evidence exceeds size limit")
    path = Path(os.environ["REVIEW_RESET_FILE"])
    write_json(path, accepted)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"sha256={digest}\n")


def check_newer_runs(repo, number, snapshot):
    identity = snapshot["identity"]
    run = _gh.rest("GET", f"repos/{repo}/actions/runs/{identity['run_id']}")
    require(str(run.get("run_attempt")) == identity["run_attempt"], "newer attempt supersedes this run")
    workflow_url = run.get("workflow_url")
    expected = re.escape(f"{_gh.API_ROOT}/repos/{repo}/actions/") + r"(?:workflows|required_workflows)/[0-9]+"
    require(isinstance(workflow_url, str) and re.fullmatch(expected, workflow_url),
            "missing or foreign workflow inventory URL")
    branch = quote(snapshot["context"]["head_ref"], safe="")
    pages = _gh.rest_paginate(f"{workflow_url}/runs?branch={branch}")
    require(pages and all(isinstance(page, dict) and isinstance(page.get("workflow_runs"), list)
                         and type(page.get("total_count")) is int for page in pages),
            "missing competing-run inventory")
    entries = [item for page in pages for item in page["workflow_runs"]]
    require(len({item["id"] for item in entries}) == len(entries),
            "duplicate competing-run inventory")
    require(all(page["total_count"] == len(entries) for page in pages),
            "incomplete competing-run inventory")
    started = pub._parse_ts(snapshot["started_at"])
    for other in entries:
        if str(other.get("id")) == identity["run_id"]:
            continue
        require(pub._parse_ts(other["run_started_at"]) <= started, "newer branch review run exists")


def dismiss(accepted):
    snapshot = accepted["snapshot"]
    identity = pub.publication_identity()
    require(snapshot["identity"] == identity and snapshot["started_at"] == pub.run_started_raw()
            and accepted["completion"].get("terminal_success") is True,
            "accepted evidence does not belong to this completed run")
    validate_proof(accepted["proof"], snapshot)
    repo, number = snapshot["context"]["repository"], snapshot["context"]["pr_number"]
    require(repo == os.environ["GITHUB_REPOSITORY"] and str(number) == os.environ["PR_NUMBER"], "reset repository/PR mismatch")
    if not snapshot["targets"]:
        print("No positively owned eligible blocking reviews to dismiss")
        return
    comments = _gh.rest_paginate(f"repos/{repo}/issues/{number}/comments")
    report = pub.find_published_report(pub.summary_comments(comments, identity["summary_marker"]), identity, snapshot["context"]["current_sha"])
    require(report is not None and not pub.is_pending(report), "ordinary publication did not complete")
    reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{number}/reviews")
    current, conflict = pub.find_verdict_review(reviews, identity, report, snapshot["context"]["current_sha"], "COMMENT")
    require(current is not None and conflict is None, "current publication is not a clean COMMENT")
    for target in snapshot["targets"]:
        check_pr(_gh.rest("GET", f"repos/{repo}/pulls/{number}"), snapshot)
        check_newer_runs(repo, number, snapshot)
        live_reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{number}/reviews")
        matched, conflict = pub.find_verdict_review(
            live_reviews, identity, report, snapshot["context"]["current_sha"], "COMMENT"
        )
        require(matched is not None and matched["id"] == current["id"] and conflict is None,
                "current clean publication disappeared or changed")
        require(not any(owned(item, identity) and item.get("id") != current["id"]
                        and pub._parse_ts(item["submitted_at"]) >= pub._parse_ts(snapshot["started_at"])
                        for item in live_reviews), "competing publication exists")
        review = _gh.rest("GET", f"repos/{repo}/pulls/{number}/reviews/{target['id']}")
        require(review.get("id") == target["id"] and owned(review, identity)
                and marker(review) == target["marker"] and review.get("commit_id") == target["commit_id"],
                "frozen target changed ownership/binding")
        if review.get("state") == "DISMISSED":
            continue  # Never recreate a human-dismissed block; retry is a no-op.
        require(review.get("state") == "CHANGES_REQUESTED", "target no longer blocking")
        original = _gh.rest("GET", f"repos/{repo}/issues/comments/{target['report_id']}")
        require(original.get("id") == target["report_id"], "original report ID mismatch")
        require(target_evidence(review, original, identity, repo, number) == target, "original obligation set changed")
        check_pr(_gh.rest("GET", f"repos/{repo}/pulls/{number}"), snapshot)
        path = f"repos/{repo}/pulls/{number}/reviews/{target['id']}/dismissals"
        try:
            _gh.rest("PUT", path, data={"message": "Superseded by a completed review with no blocking issues; current audit evidence persisted in CI.", "event": "DISMISS"}, max_attempts=1)
        except _gh.TransientOutageError:
            print("::warning::Ambiguous dismissal response; reading the exact target")
        result = _gh.rest("GET", f"repos/{repo}/pulls/{number}/reviews/{target['id']}")
        require(result.get("id") == target["id"] and result.get("state") == "DISMISSED"
                and owned(result, identity) and marker(result) == target["marker"]
                and result.get("commit_id") == target["commit_id"],
                "dismissal did not reconcile to the exact target")
        print(f"Dismissed prior owned blocking review {target['id']}")


def reset():
    # The action passes upload OUTCOME, not the continue-on-error conclusion.
    require(os.environ.get("REVIEW_RESET_UPLOAD_OUTCOME") == "success"
            and os.environ.get("REVIEW_RESET_ARTIFACT_ID", "").isdigit()
            and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", os.environ.get("REVIEW_RESET_ARTIFACT_DIGEST", "")),
            "accepted proof upload did not succeed")
    path = Path(os.environ["REVIEW_RESET_FILE"])
    require(hashlib.sha256(path.read_bytes()).hexdigest() == os.environ.get("REVIEW_RESET_SHA256"),
            "accepted evidence changed after upload")
    dismiss(read_json(path))


def main():
    try:
        {"prepare": prepare, "capture": capture, "dismiss": reset}[sys.argv[1]]()
    except (_gh.GitHubError, OSError, KeyError, TypeError, ValueError) as error:
        # Reset-only failure: no impact on the baseline publisher or its state.
        print(f"::warning::Prior reviews retained: {sanitize(str(error))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
