#!/usr/bin/env python3
"""Run-local review evidence and conservative, exact-target baseline resets.

Only trusted host steps call this module. The model has a native read-only tool
inventory, not a shell, and supplies only RESULT_SCHEMA through structured_output.
Historical association uses the repository's existing same-repository automation
trust; markers/digests are consistency checks, not exact workflow attestation.
"""

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import _gh
import _review_state

MAX_RESULT_BYTES = 1024 * 1024
MAX_EXECUTION_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_DIFF_BYTES = 20 * 1024 * 1024
REVIEWER_KEYS = ("workflow_ref", "summary_marker", "verdict_mode")
IDENTITY_KEYS = REVIEWER_KEYS + ("run_id", "run_attempt")
CONTEXT_KEYS = ("repository", "pr_number", "current_sha", "current_base_sha",
                "current_base_ref", "workflow_ref", "summary_heading", "summary_comment_id")
DISPOSITIONS = ("fixed", "no_longer_applicable", "original_finding_incorrect",
                "still_present", "unverified")
WITHDRAWABLE = set(DISPOSITIONS[:3])
READ_TOOLS = {"Read", "Glob", "Grep", "Agent", "Task"}
MODEL_TOOLS = READ_TOOLS | {
    "mcp__github_comment__update_claude_comment",
    "mcp__github_inline_comment__create_inline_comment",
    # Native --json-schema transport; it does not write host files.
    "StructuredOutput",
}
ID_PATTERN = r"[0-9]+-[0-9]+-[1-9][0-9]*"
DIGEST_PATTERN = r"(?:sha256:)?[0-9a-f]{64}"


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _text(max_length=20000):
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _array(items, maximum=500, minimum=0):
    return {"type": "array", "items": items, "maxItems": maximum, "minItems": minimum}


EVIDENCE_SCHEMA = _array(_object({
    "path": _text(1000), "symbol_or_range": _text(1000), "explanation": _text(),
}), maximum=50, minimum=1)
RESULT_SCHEMA = _object({
    "schema_version": {"type": "integer", "enum": [1]},
    "model_assessment": _object({
        "review_complete": {"type": "boolean"},
        "coverage_gaps": _array(_text()),
    }),
    "blocking_findings": _array(_object({
        "prior_finding_id": {"type": ["string", "null"], "pattern": "^" + ID_PATTERN + "$"},
        "title": _text(1000), "rationale": _text(), "evidence": EVIDENCE_SCHEMA,
    })),
    "prior_finding_audit": _array(_object({
        "finding_id": {"type": "string", "pattern": "^" + ID_PATTERN + "$"},
        "disposition": {"type": "string", "enum": list(DISPOSITIONS)},
        "evidence": EVIDENCE_SCHEMA,
    })),
    "report": _object({"summary": _text(60000), "suggestions": _array(_text())}),
})

_PUBLISHER = None


def publisher():
    # Lazy import avoids a circular import when the publisher calls us.
    global _PUBLISHER
    if _PUBLISHER is None:
        spec = importlib.util.spec_from_file_location(
            "_result_publisher", Path(__file__).with_name("publish-review-report.py"))
        _PUBLISHER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_PUBLISHER)
    return _PUBLISHER


def require(condition, message):
    if not condition:
        raise _gh.TerminalError(message)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(_value):
        raise _gh.TerminalError("non-finite JSON value")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as e:
        raise _gh.TerminalError("invalid review JSON") from e


def validate_schema(value, schema, location="result"):
    """Validate the single bounded schema used by both native output and host."""
    kinds = schema["type"]
    kinds = kinds if isinstance(kinds, list) else [kinds]
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    require(any(type(value) is types[kind] for kind in kinds), f"{location}: wrong type")
    if "enum" in schema:
        require(value in schema["enum"], f"{location}: invalid value")
    if isinstance(value, dict):
        require(set(value) == set(schema["properties"]), f"{location}: missing/unknown fields")
        for key, child in schema["properties"].items():
            validate_schema(value[key], child, f"{location}.{key}")
    elif isinstance(value, list):
        require(schema["minItems"] <= len(value) <= schema["maxItems"], f"{location}: size")
        for index, child in enumerate(value):
            validate_schema(child, schema["items"], f"{location}[{index}]")
    elif isinstance(value, str):
        require(bool(value.strip()), f"{location}: empty text")
        require(len(value) <= schema.get("maxLength", 1000), f"{location}: size")
        if "pattern" in schema:
            require(re.fullmatch(schema["pattern"], value) is not None, f"{location}: invalid ID")


def sanitize_markdown(content):
    """Preserve pinned upstream redactSecrets(sanitizeContent(...)) semantics.

    Port of anthropics/claude-code-action@9171db3 src/github/utils/sanitizer.ts;
    apply to model text BEFORE adding host markers. No model HTML comments survive.
    """
    content = re.sub(r"<!--[\s\S]*?-->", "", content)
    content = re.sub(r"[\u200b\u200c\u200d\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u202a-\u202e\u2066-\u2069]", "", content)
    content = re.sub(r"!\[[^\]]*\]\(", "![](", content)
    content = re.sub(r"!\[[^\]]*\](\[[^\]]*\])", r"![]\1", content)
    content = re.sub(r'''(\[[^\]]*\]\([^)]+)\s+"[^"]*"''', r"\1", content)
    content = re.sub(r"(\[[^\]]*\]\([^)]+)\s+'[^']*'", r"\1", content)
    for attribute in ("alt", "title", "aria-label", r"data-[a-zA-Z0-9-]+", "placeholder"):
        for value in (r'''"[^"]*"''', r"'[^']*'", r"[^\s>]+"):
            content = re.sub(r"\s" + attribute + r"\s*=\s*" + value, "", content, flags=re.I)
    def entity(match):
        number = int(match.group(1), 16 if match.re.pattern.startswith("&#x") else 10)
        return chr(number) if 32 <= number <= 126 else ""
    content = re.sub(r"&#(\d+);", entity, content)
    content = re.sub(r"&#x([0-9a-fA-F]+);", entity, content)
    content = re.sub(r"gh[pousr]_[A-Za-z0-9]{36}\b|github_pat_[A-Za-z0-9_]{11,221}\b",
                     "[REDACTED_GITHUB_TOKEN]", content)
    content = re.sub(r"sk-ant-[A-Za-z0-9_-]{20,}", "[REDACTED_ANTHROPIC_KEY]", content)
    content = re.sub(r'''(\b|\\[nrtbf"\\/]|\\u[0-9a-fA-F]{4}|\[[0-9;]*m)(?:AKIA|ASIA)[A-Z0-9]{16}\b''',
                     lambda match: match.group(1) + "[REDACTED_AWS_KEY_ID]", content)
    content = re.sub(r"xox[abpsr]-[A-Za-z0-9-]{10,}", "[REDACTED_SLACK_TOKEN]", content)
    return re.sub(r"eyJ[A-Za-z0-9_-]{10,2000}\.eyJ[A-Za-z0-9_-]{10,4000}\.[A-Za-z0-9_-]{10,2000}\b",
                  "[REDACTED_JWT]", content)


def _sanitize(value):
    if isinstance(value, str):
        return sanitize_markdown(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    return value


def checked_path(path):
    path = Path(path).absolute()
    require(".." not in path.parts, "path traversal in host evidence")
    require(not any(part.is_symlink() for part in (path, *path.parents)), "symlink in host evidence path")
    return path


def guard_paths():
    # Run before existing host writers: PR-authored symlinks must not redirect
    # generated context/diff/criteria into host output or the action checkout.
    directory = checked_path(".github")
    require(not directory.exists() or directory.is_dir(), "not a regular .github directory")
    for name in ("pr-context.json", "resolved-threads.json", "prior-findings.json",
                 "incremental.diff", "review-criteria.md", "review-criteria.json",
                 "pr-review.diff", "prior-obligations.json"):
        path = checked_path(Path(".github") / name)
        if path.exists():
            info = path.stat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe generated context file")


def write_text(path, text):
    path = checked_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe host output file")
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(text)
    finally:
        os.close(fd)


def safe_file(path, maximum):
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "not a regular owned evidence file")
        require(info.st_size <= maximum, "evidence file exceeds size bound")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "evidence file exceeds size bound")
        return raw
    finally:
        os.close(fd)


def result_dir():
    path = checked_path(os.environ["REVIEW_RESULT_DIR"])
    temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
    require(path.is_relative_to(temp) and not path.is_relative_to(workspace),
            "host result directory must be in RUNNER_TEMP outside checkout")
    return path


def write_json(path, value):
    # Host evidence is never overwritten (receipts deliberately use another name).
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read_json(path, maximum=MAX_RESULT_BYTES):
    return strict_json(safe_file(path, maximum))


def reviewer_identity(identity):
    return {key: identity[key] for key in REVIEWER_KEYS}


def artifact_name(identity):
    namespace = json.dumps(reviewer_identity(identity), sort_keys=True).encode()
    return f"pr-review-result-{identity['run_id']}-{identity['run_attempt']}-{hashlib.sha256(namespace).hexdigest()[:16]}"


def marker(body, pattern):
    matches = pattern.findall(body or "")
    require(len(matches) == 1, "missing or ambiguous host marker")
    value = strict_json(matches[0])
    require(isinstance(value, dict), "invalid host marker")
    return value


def check_edit_history(item, kind):
    """Reviews must be unedited; reports may have only visible bot edits.

    Missing/deleted/null history is uncertainty, not an empty edit history.
    Reports can be superseded: their exact original marker is still addressable.
    """
    require(bool(item.get("node_id")), "missing edit-history node ID")
    cursor = None
    edits = []
    seen = set()
    while True:
        parent = "pullRequest" if kind == "PullRequestReview" else "issue"
        query = """query($id:ID!, $after:String) { node(id:$id) { ... on KIND {
          id databaseId body author { login } lastEditedAt editor { login } includesCreatedEdit
          PARENT { number repository { nameWithOwner } }
          userContentEdits(first:100, after:$after) {
            totalCount nodes { editor { login } deletedAt deletedBy { login } }
            pageInfo { hasNextPage endCursor }
          }
        } } }""".replace("KIND", kind).replace("PARENT", parent)
        node = _gh.graphql(query, {"id": item["node_id"], "after": cursor}).get("node")
        require(isinstance(node, dict) and node.get("id") == item["node_id"]
                and node.get("databaseId") == item["id"] and node.get("body") == item.get("body"),
                "edit-history object/body disagrees with REST binding")
        require((node.get("author") or {}).get("login") in _review_state.BOT_LOGINS,
                "foreign history author")
        binding = node.get(parent) or {}
        require(str(binding.get("number")) == os.environ["PR_NUMBER"]
                and (binding.get("repository") or {}).get("nameWithOwner") == os.environ["GITHUB_REPOSITORY"],
                "edit history belongs to another PR")
        history = node.get("userContentEdits")
        require(isinstance(history, dict) and isinstance(history.get("nodes"), list)
                and type(history.get("totalCount")) is int, "unavailable edit history")
        for edit in history["nodes"]:
            require(isinstance(edit, dict) and edit.get("deletedAt") is None
                    and edit.get("deletedBy") is None
                    and (edit.get("editor") or {}).get("login") in _review_state.BOT_LOGINS,
                    "deleted, unknown or human edit history")
            edits.append(edit)
        page = history.get("pageInfo") or {}
        require(type(page.get("hasNextPage")) is bool, "incomplete history pagination")
        if not page["hasNextPage"]:
            require(len(edits) == history["totalCount"], "incomplete edit history")
            break
        cursor = page.get("endCursor")
        require(cursor and cursor not in seen and len(edits) < 10000, "invalid history pagination")
        seen.add(cursor)
    if kind == "PullRequestReview":
        require(not edits and node.get("lastEditedAt") is None and node.get("editor") is None
                and node.get("includesCreatedEdit") is False, "formal review was edited")
    else:
        require("lastEditedAt" in node and "editor" in node and "includesCreatedEdit" in node,
                "incomplete report edit metadata")
        if node["lastEditedAt"] is not None:
            require(edits and node["includesCreatedEdit"] is True
                    and (node["editor"] or {}).get("login") in _review_state.BOT_LOGINS,
                    "inconsistent report edit metadata")
        else:
            require(not edits and node["editor"] is None and node["includesCreatedEdit"] is False,
                    "inconsistent report edit metadata")


class _ArchiveRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        require(url.startswith("https://"), "non-HTTPS artifact redirect")
        redirected = super().redirect_request(request, fp, code, message, headers, url)
        if redirected is not None:
            # Never forward the GitHub credential to signed object storage.
            redirected.remove_header("Authorization")
        return redirected


def download_archive(repo, artifact_id):
    opener = urllib.request.build_opener(_ArchiveRedirect()).open
    _, _, raw = _gh.request("GET", f"{_gh.API_ROOT}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
                             opener=opener, max_bytes=MAX_ARCHIVE_BYTES)
    return raw


def archive_result(raw, digest):
    require(re.fullmatch(DIGEST_PATTERN, digest or "") is not None, "invalid artifact digest")
    require(hashlib.sha256(raw).hexdigest() == digest.removeprefix("sha256:"), "artifact ZIP digest mismatch")
    require(len(raw) <= MAX_ARCHIVE_BYTES, "artifact ZIP too large")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            require({entry.filename for entry in members} == {"accepted.json", "snapshot.json", "completion.json"}
                    and len(members) == 3, "unexpected or duplicate artifact members")
            for entry in members:
                require(not stat.S_ISLNK(entry.external_attr >> 16)
                        and not entry.is_dir() and not entry.flag_bits & 1
                        and entry.file_size <= MAX_RESULT_BYTES, "unsafe artifact member")
            accepted = strict_json(archive.read("accepted.json"))
            require(accepted.get("snapshot") == strict_json(archive.read("snapshot.json"))
                    and accepted.get("completion") == strict_json(archive.read("completion.json")),
                    "artifact evidence records disagree")
            return accepted
    except (zipfile.BadZipFile, RuntimeError, KeyError) as e:
        raise _gh.TerminalError("unreadable accepted-result archive") from e


def artifact_metadata(repo, reference, context, repository_id):
    artifact_id = reference.get("result_artifact_id")
    digest = reference.get("result_artifact_digest")
    require(str(artifact_id).isdigit() and re.fullmatch(DIGEST_PATTERN, digest or ""), "missing original artifact reference")
    metadata = _gh.rest("GET", f"repos/{repo}/actions/artifacts/{artifact_id}")
    require(isinstance(metadata, dict) and metadata.get("id") == int(artifact_id)
            and metadata.get("expired") is False and metadata.get("name") == artifact_name(reference)
            and isinstance(metadata.get("digest"), str)
            and metadata["digest"].removeprefix("sha256:") == digest.removeprefix("sha256:"),
            "missing, expired or changed original artifact")
    run = metadata.get("workflow_run") or {}
    require(str(run.get("id")) == reference.get("run_id")
            and run.get("repository_id") == repository_id
            and run.get("head_repository_id") == repository_id
            and run.get("head_sha") == context["current_sha"], "artifact producer association mismatch")
    attempt = reference.get("run_attempt")
    require(str(attempt).isdigit() and int(attempt) > 0, "invalid producing attempt")
    producing = _gh.rest("GET", f"repos/{repo}/actions/runs/{reference['run_id']}/attempts/{attempt}")
    require(str(producing.get("id")) == reference["run_id"]
            and str(producing.get("run_attempt")) == attempt
            and (producing.get("repository") or {}).get("id") == repository_id
            and (producing.get("head_repository") or {}).get("id") == repository_id
            and producing.get("head_sha") == context["current_sha"], "producing run/attempt mismatch")
    return metadata


def verify_target(repo, pr_number, review, identity, repository_id, expected=None):
    pub = publisher()
    require(type(review.get("id")) is int and review["id"] > 0, "invalid exact target ID")
    require((review.get("user") or {}).get("login") in _review_state.BOT_LOGINS,
            "target is not this bot's review")
    require(review.get("pull_request_url") == f"{_gh.API_ROOT}/repos/{repo}/pulls/{pr_number}", "target belongs to another PR")
    reference = marker(review.get("body"), pub.VERDICT_MARKER_PATTERN)
    require(all(reference.get(key) == identity[key] for key in REVIEWER_KEYS), "foreign reviewer tuple")
    require(all(isinstance(reference.get(key), str) and reference[key] for key in IDENTITY_KEYS), "incomplete publication identity")
    require(re.fullmatch(r"[0-9a-f]{40}", review.get("commit_id") or ""), "missing target commit")
    check_edit_history(review, "PullRequestReview")
    report_id = reference.get("report_comment_id")
    require(type(report_id) is int and report_id > 0, "missing source report ID")
    report = _gh.rest("GET", f"repos/{repo}/issues/comments/{report_id}")
    require(report.get("id") == report_id
            and (report.get("user") or {}).get("login") in _review_state.BOT_LOGINS,
            "missing/foreign original report")
    check_edit_history(report, "IssueComment")
    state = marker(report.get("body"), _review_state.REVIEW_STATE_PATTERN)
    require(all(state.get(key) == reference[key] for key in IDENTITY_KEYS)
            and state.get("publication") == "completed"
            and state.get("last_reviewed_sha") == review["commit_id"]
            and all(state.get(key) == reference.get(key) for key in ("result_artifact_id", "result_artifact_digest")),
            "original review/report/artifact binding mismatch")
    context = {"current_sha": review["commit_id"]}
    artifact_metadata(repo, reference, context, repository_id)
    target = {"review_id": review["id"], "report_comment_id": report_id,
              "commit_id": review["commit_id"], "reference": reference}
    if expected is not None:
        require(target == expected, "snapshotted target binding changed")
        return None
    accepted = archive_result(download_archive(repo, reference["result_artifact_id"]),
                              reference["result_artifact_digest"])
    original_context = accepted.get("context") or {}
    require(accepted.get("identity") == {key: reference[key] for key in IDENTITY_KEYS}
            and original_context.get("repository") == repo
            and str(original_context.get("pr_number")) == str(pr_number)
            and original_context.get("current_sha") == review["commit_id"]
            and original_context.get("current_base_sha") == state.get("base_sha")
            and original_context.get("summary_heading") == identity["summary_marker"],
            "original accepted result envelope mismatch")
    check_accepted(accepted)
    findings = accepted["result"]["blocking_findings"]
    require(bool(findings), "original change request has no structured blockers")
    return target, findings


def check_pr(pr, context):
    require(pr.get("state") == "open"
            and (pr.get("head") or {}).get("sha") == context["current_sha"]
            and (pr.get("base") or {}).get("sha") == context["current_base_sha"]
            and (pr.get("base") or {}).get("ref") == context["current_base_ref"]
            and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == context["repository"],
            "PR closed, head/base changed, or fork boundary changed")


def prepare():
    pub = publisher()
    guard_paths()
    context = read_json(pub.PR_CONTEXT_PATH)
    identity = pub.publication_identity()
    require(all(identity.values()), "missing host publication identity")
    require(context["repository"] == os.environ["GITHUB_REPOSITORY"]
            and str(context["pr_number"]) == os.environ["PR_NUMBER"]
            and context["workflow_ref"] == identity["workflow_ref"]
            and context["summary_heading"] == identity["summary_marker"]
            and context["current_sha"] == pub.current_head_sha(), "context/checkout/host identity mismatch")
    repo, pr_number = context["repository"], context["pr_number"]
    check_pr(_gh.rest("GET", f"repos/{repo}/pulls/{pr_number}"), context)
    repository_id = _gh.rest("GET", f"repos/{repo}")["id"]
    snapshot = {"context": {key: context[key] for key in CONTEXT_KEYS}, "identity": identity,
                "repository_id": repository_id, "started_at": pub.run_started_raw(),
                "targets": [], "obligations": [], "unavailable": []}
    obligations = {}
    claims = set()
    for review in _gh.rest_paginate(f"repos/{repo}/pulls/{pr_number}/reviews"):
        if review.get("state") != "CHANGES_REQUESTED" or (review.get("user") or {}).get("login") not in _review_state.BOT_LOGINS:
            continue
        try:
            reference = marker(review.get("body"), pub.VERDICT_MARKER_PATTERN)
            if any(reference.get(key) not in (None, identity[key]) for key in REVIEWER_KEYS):
                continue
            require(all(reference.get(key) == identity[key] for key in REVIEWER_KEYS),
                    "incomplete legacy blocking identity")
            claim = tuple(reference.get(key) for key in IDENTITY_KEYS)
            require(claim not in claims, "duplicate blocking publication identity")
            claims.add(claim)
            target, findings = verify_target(repo, pr_number, review, identity, repository_id)
            snapshot["targets"].append(target)
            for finding in findings:
                finding_id = finding["finding_id"]
                # Carry-forward prose can evolve, but never omit another obligation.
                if finding_id not in obligations:
                    obligations[finding_id] = {"finding_id": finding_id, "versions": []}
                obligations[finding_id]["versions"].append(finding)
        except (_gh.GitHubError, ValueError, KeyError, TypeError) as e:
            snapshot["unavailable"].append({"review_id": review.get("id"), "reason": str(e)})
    snapshot["obligations"] = list(obligations.values())
    # Fetch the FULL diff, not the filtered incremental compare artifact. Size or
    # encoding uncertainty fails preparation rather than quietly narrowing review.
    _, _, raw = _gh.request("GET", f"{_gh.API_ROOT}/repos/{repo}/pulls/{pr_number}",
                             headers={"Accept": "application/vnd.github.diff"}, max_bytes=MAX_DIFF_BYTES)
    try:
        diff = raw.decode("utf-8")
    except UnicodeError as e:
        raise _gh.TerminalError("full diff is not readable UTF-8") from e
    require(bool(diff.strip()), "full PR diff unavailable")
    write_text(".github/pr-review.diff", diff)
    write_text(".github/prior-obligations.json",
               json.dumps({"obligations": snapshot["obligations"], "unavailable": snapshot["unavailable"]}, indent=2))
    directory = tempfile.mkdtemp(prefix=f"pr-review-{identity['run_id']}-{identity['run_attempt']}-",
                                 dir=os.environ["RUNNER_TEMP"])
    os.environ["REVIEW_RESULT_DIR"] = directory
    result_dir()  # Check the storage boundary before any authoritative write.
    write_json(Path(directory) / "snapshot.json", snapshot)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"result_dir={directory}\n")
        stream.write(f"artifact_name={artifact_name(identity)}\n")
        stream.write("json_schema=" + json.dumps(RESULT_SCHEMA, separators=(",", ":")) + "\n")


def completion_receipt(messages, native):
    require(isinstance(messages, list) and messages, "missing runner messages")
    terminal = messages[-1]
    require(terminal.get("type") == "result" and terminal.get("subtype") == "success"
            and terminal.get("is_error") is False and terminal.get("structured_output") == native,
            "native output does not match successful terminal runner result")
    require(type(terminal.get("num_turns")) is int and 0 < terminal["num_turns"] <= 100,
            "invalid terminal turn count")
    init = [m for m in messages if m.get("type") == "system" and m.get("subtype") == "init"]
    require(len(init) == 1 and isinstance(init[0].get("tools"), list)
            and set(init[0]["tools"]).issubset(MODEL_TOOLS)
            and "Read" in init[0]["tools"] and "Bash" not in init[0]["tools"],
            "runner did not use the constrained model tool inventory")
    require(init[0].get("permissionMode") == "default", "unexpected model permission mode")
    servers = init[0].get("mcp_servers")
    require(isinstance(servers, list)
            and all(s.get("name") in {"github_comment", "github_inline_comment"}
                    and s.get("status") == "connected" for s in servers), "unexpected MCP inventory")
    pending = {}
    finished = []
    uses = set()
    native_tasks = {}
    for message in messages[:-1]:
        require(message.get("type") != "result", "multiple terminal results")
        if message.get("type") == "system" and message.get("subtype") == "task_started":
            require(message.get("task_type") == "local_agent"
                    and message.get("is_backgrounded") is False
                    and message.get("tool_use_id"), "untracked or background native audit")
            native_tasks[message["task_id"]] = message["tool_use_id"]
        if message.get("type") == "system" and message.get("subtype") == "task_notification":
            require(message.get("status") == "completed", "native audit failed/stopped")
        if message.get("type") == "system" and message.get("subtype") == "task_updated":
            patch = message.get("patch") or {}
            require(not patch.get("is_backgrounded") and patch.get("status") not in
                    {"failed", "killed", "paused"} and not patch.get("error"),
                    "native audit failed or moved to background")
        blocks = (message.get("message") or {}).get("content") or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if block.get("type") == "tool_use":
                require(block.get("name") in MODEL_TOOLS, "unexpected executed model tool")
                if block["name"] in {"Task", "Agent"}:
                    tool_id = block.get("id")
                    require(tool_id and tool_id not in uses and not (block.get("input") or {}).get("run_in_background"),
                            "duplicate or background audit invocation")
                    uses.add(tool_id)
                    pending[tool_id] = block["name"]
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                require(block.get("is_error", False) is False, "audit tool returned an error")
                output = message.get("tool_use_result")
                require(isinstance(output, dict) and output.get("status") == "completed"
                        and output.get("agentId"), "missing completed native Agent/Task result")
                stats = output.get("toolStats") or {}
                require(not stats.get("bashCount") and not stats.get("editFileCount")
                        and not output.get("worktreePath"), "audit used a write/shell path")
                content = block.get("content")
                require(bool(content) and isinstance(output.get("content"), list)
                        and bool(output["content"]), "empty native audit result")
                texts = [content] if isinstance(content, str) else [
                    item.get("text", "") for item in content if isinstance(item, dict)]
                texts.extend(item.get("text", "") for item in output["content"] if isinstance(item, dict))
                require(texts and all(isinstance(text, str) and text.strip() for text in texts),
                        "missing audit report text")
                for text in texts:
                    require(not re.search(r'(?i)(?:"isError"\s*:\s*true|"is_error"\s*:\s*true|^\s*Error:)', text),
                            "audit result contains an error")
                finished.append({"tool_use_id": block["tool_use_id"], "agent_id": output["agentId"],
                                 "tool": pending.pop(block["tool_use_id"]), "is_error": False,
                                 "status": "completed"})
    require(not pending, "required spawned audits have not returned")
    require(set(native_tasks.values()).issubset({item["tool_use_id"] for item in finished}),
            "native spawned audit is still pending")
    return {"terminal_success": True, "num_turns": terminal["num_turns"],
            "tools": init[0]["tools"], "audits": finished}


def validate_payload(payload, snapshot):
    validate_schema(payload, RESULT_SCHEMA)
    require("<!--" not in payload["report"]["summary"], "model-authored HTML/host marker")
    result = _sanitize(copy.deepcopy(payload))
    validate_schema(result, RESULT_SCHEMA)
    require("<!--" not in result["report"]["summary"], "encoded model-authored HTML/host marker")
    require(result["model_assessment"]["review_complete"] is True
            and not result["model_assessment"]["coverage_gaps"], "review is incomplete or has coverage gaps")
    require(not _review_state.is_provisional(result["report"]["summary"]), "provisional final result")
    expected = {item["finding_id"] for item in snapshot["obligations"]}
    audits = result["prior_finding_audit"]
    require(len(audits) == len(expected) and {item["finding_id"] for item in audits} == expected,
            "missing, duplicate or foreign current prior-finding audit ID")
    dispositions = {item["finding_id"]: item["disposition"] for item in audits}
    carried = [item["prior_finding_id"] for item in result["blocking_findings"] if item["prior_finding_id"] is not None]
    require(len(carried) == len(set(carried)) and set(carried).issubset(expected), "duplicate or foreign carried finding ID")
    require({key for key, disposition in dispositions.items() if disposition == "still_present"} == set(carried),
            "audit dispositions contradict structured blockers")
    for group in (audits, result["blocking_findings"]):
        for item in group:
            for evidence in item["evidence"]:
                path = evidence["path"]
                require(not Path(path).is_absolute() and ".." not in Path(path).parts
                        and not any(ord(char) < 32 for char in path), "unsafe source evidence path")
    require(publisher().parse_blocking_count(result["report"]["summary"], snapshot["context"]["summary_heading"])
            == len(result["blocking_findings"]), "Markdown count disagrees with structured blockers")
    identity = snapshot["identity"]
    for ordinal, finding in enumerate(result["blocking_findings"], 1):
        finding["finding_id"] = finding["prior_finding_id"] or f"{identity['run_id']}-{identity['run_attempt']}-{ordinal}"
    return result


def verify_source_references(result, snapshot):
    # Inspect immutable git metadata, never execute PR code. CLI preparation
    # restores some config files, so workspace existence alone is not binding.
    tree = subprocess.run(["git", "ls-tree", "-rz", snapshot["context"]["current_sha"]],
                          check=True, capture_output=True).stdout
    require(len(tree) <= MAX_DIFF_BYTES, "source tree metadata exceeds size bound")
    paths = {entry.split(b"\t", 1)[1] for entry in tree.split(b"\0")
             if b"\t" in entry and entry.split(b"\t", 1)[0].split()[1] == b"blob"}
    prior_paths = {
        item["finding_id"]: {Path(evidence["path"]).as_posix() for version in item["versions"] for evidence in version["evidence"]}
        for item in snapshot["obligations"]
    }
    for item in result["blocking_findings"] + result["prior_finding_audit"]:
        if item.get("disposition") == "unverified":
            continue
        for evidence in item["evidence"]:
            path = Path(evidence["path"]).as_posix()
            removed_original = (item.get("disposition") == "no_longer_applicable"
                                and path in prior_paths[item["finding_id"]])
            require(path.encode("utf-8") in paths or removed_original,
                    "evidence reference is not in the reviewed source snapshot")


def validate():
    require(os.environ.get("REVIEW_MODEL_CONCLUSION") == "success", "model step did not complete successfully")
    directory = result_dir()
    snapshot = read_json(directory / "snapshot.json")
    raw = os.environ.get("REVIEW_STRUCTURED_OUTPUT", "")
    require(bool(raw) and len(raw.encode()) <= MAX_RESULT_BYTES, "missing/oversize native structured output")
    payload = strict_json(raw)
    execution_path = checked_path(os.environ["REVIEW_EXECUTION_FILE"])
    require(execution_path.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
            and not execution_path.is_relative_to(Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()),
            "native execution record outside trusted runner storage")
    completion = completion_receipt(read_json(execution_path, MAX_EXECUTION_BYTES), payload)
    result = validate_payload(payload, snapshot)
    verify_source_references(result, snapshot)
    action_root = Path(__file__).parents[4]
    action_sha = None
    if (action_root / ".git").exists():
        action_sha = subprocess.run(["git", "-C", str(action_root), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
    accepted = {"schema_version": 1, "context": snapshot["context"], "identity": snapshot["identity"],
                "snapshot": snapshot, "completion": completion, "result": result,
                "provenance": {"workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA", ""),
                               "action_sha": action_sha,
                               "action_ref": os.environ.get("REVIEW_ACTION_REF", ""),
                               "action_repository": os.environ.get("REVIEW_ACTION_REPOSITORY", "")}}
    require(len(json.dumps(accepted).encode()) <= MAX_RESULT_BYTES, "accepted record exceeds size bound")
    write_json(directory / "completion.json", completion)
    write_json(directory / "accepted.json", accepted)


def check_accepted(accepted):
    require(accepted.get("schema_version") == 1 and accepted.get("identity") == accepted["snapshot"]["identity"]
            and accepted.get("context") == accepted["snapshot"]["context"]
            and accepted["completion"].get("terminal_success") is True, "invalid accepted evidence envelope")
    receipt = accepted["completion"]
    require(type(receipt.get("num_turns")) is int and 0 < receipt["num_turns"] <= 100
            and isinstance(receipt.get("tools"), list)
            and set(receipt["tools"]).issubset(MODEL_TOOLS)
            and "Read" in receipt["tools"] and isinstance(receipt.get("audits"), list),
            "invalid historical completion receipt")
    audit_ids = set()
    for audit in receipt["audits"]:
        require(audit.get("status") == "completed" and audit.get("is_error") is False
                and audit.get("agent_id") and audit.get("tool") in {"Agent", "Task"}
                and audit.get("tool_use_id") and audit["tool_use_id"] not in audit_ids,
                "invalid historical audit receipt")
        audit_ids.add(audit["tool_use_id"])
    payload = copy.deepcopy(accepted["result"])
    finding_ids = [finding.pop("finding_id") for finding in payload["blocking_findings"]]
    result = validate_payload(payload, accepted["snapshot"])
    require(finding_ids == [finding["finding_id"] for finding in result["blocking_findings"]], "invalid host-issued finding IDs")
    require(accepted["result"] == result, "accepted result was altered after validation")


def load_current_result():
    require(os.environ.get("REVIEW_RESULT_UPLOAD_CONCLUSION") == "success", "required accepted-result upload did not succeed")
    directory = result_dir()
    accepted = read_json(directory / "accepted.json")
    check_accepted(accepted)
    require(accepted["snapshot"] == read_json(directory / "snapshot.json")
            and accepted["completion"] == read_json(directory / "completion.json"), "run-local evidence was changed")
    pub = publisher()
    identity = pub.publication_identity()
    context = accepted["context"]
    require(identity == accepted["identity"] and context["repository"] == os.environ["GITHUB_REPOSITORY"]
            and str(context["pr_number"]) == os.environ["PR_NUMBER"]
            and context["current_sha"] == pub.current_head_sha()
            and accepted["snapshot"]["started_at"] == pub.run_started_raw(), "accepted result is not this host run")
    reference = dict(identity, result_artifact_id=os.environ.get("REVIEW_RESULT_ARTIFACT_ID", ""),
                     result_artifact_digest=os.environ.get("REVIEW_RESULT_ARTIFACT_DIGEST", ""))
    artifact_metadata(context["repository"], reference, context, accepted["snapshot"]["repository_id"])
    return accepted, {key: reference[key] for key in ("result_artifact_id", "result_artifact_digest")}


def reset_reviews(accepted, artifact, report):
    """Last operation: no mutation before accepted upload and completed COMMENT."""
    pub = publisher()
    snapshot, result = accepted["snapshot"], accepted["result"]
    context, identity = accepted["context"], accepted["identity"]
    repo, pr_number = context["repository"], context["pr_number"]
    receipt = {"review_completed": True, "reset_completed": False, **artifact,
               "report_comment_id": report["id"], "context": context, "outcomes": []}
    reasons = [item["reason"] for item in snapshot["unavailable"]]
    if result["blocking_findings"]:
        reasons.append("current blocking findings remain")
    if any(audit["disposition"] not in WITHDRAWABLE for audit in result["prior_finding_audit"]):
        reasons.append("prior finding remains or could not be verified in this run")
    if reasons:
        receipt["retained_reasons"] = reasons
        ids = {target["review_id"] for target in snapshot["targets"]}
        ids.update(item["review_id"] for item in snapshot["unavailable"] if type(item.get("review_id")) is int)
        receipt["outcomes"] = [{"review_id": review_id, "outcome": "retained"} for review_id in sorted(ids)]
        links = "\n".join(f"- https://github.com/{repo}/pull/{pr_number}#pullrequestreview-{review_id}" for review_id in sorted(ids))
        _gh._job_summary("### Review completed; block retained\n\n" + "\n".join(f"- {reason}" for reason in reasons)
                         + f"\n\nReport: {report.get('html_url')}\n\n"
                         + f"A maintainer may manually dismiss these historical reviews after checking the report:\n{links}\n")
    elif not snapshot["targets"]:
        receipt["reset_completed"] = True
        receipt["no_op"] = True
    else:
        error = None
        try:
            for target in snapshot["targets"]:
                check_pr(_gh.rest("GET", f"repos/{repo}/pulls/{pr_number}"), context)
                comments = pub.summary_comments(_gh.rest_paginate(f"repos/{repo}/issues/{pr_number}/comments"), identity["summary_marker"])
                require(pub.later_completed_attempt(comments, pub.run_started_at(), identity["workflow_ref"]) is None,
                        "a newer owned attempt completed")
                current_report = _gh.rest("GET", f"repos/{repo}/issues/comments/{report['id']}")
                require(current_report.get("body") == report.get("body")
                        and (current_report.get("user") or {}).get("login") in _review_state.BOT_LOGINS,
                        "current completed publication changed")
                reviews = _gh.rest_paginate(f"repos/{repo}/pulls/{pr_number}/reviews")
                matched, conflict = pub.find_verdict_review(reviews, identity, current_report, context["current_sha"], "COMMENT")
                require(matched is not None and conflict is None and not pub.is_pending(current_report), "completed COMMENT publication unavailable")
                current_reference = marker(matched.get("body"), pub.VERDICT_MARKER_PATTERN)
                require(all(current_reference.get(key) == value for key, value in artifact.items()),
                        "current formal review artifact binding changed")
                check_edit_history(matched, "PullRequestReview")
                frozen = {item["review_id"] for item in snapshot["targets"]}
                for review in reviews:
                    if review.get("state") != "CHANGES_REQUESTED" or (review.get("user") or {}).get("login") not in _review_state.BOT_LOGINS:
                        continue
                    other = marker(review.get("body"), pub.VERDICT_MARKER_PATTERN)
                    if all(other.get(key) == identity[key] for key in REVIEWER_KEYS):
                        require(review["id"] in frozen, "new owned blocking review appeared after snapshot")
                current = _gh.rest("GET", f"repos/{repo}/pulls/{pr_number}/reviews/{target['review_id']}")
                verify_target(repo, pr_number, current, identity, snapshot["repository_id"], expected=target)
                check_pr(_gh.rest("GET", f"repos/{repo}/pulls/{pr_number}"), context)
                if current.get("state") == "DISMISSED":
                    outcome = "already_dismissed"
                else:
                    require(current.get("state") == "CHANGES_REQUESTED", "unexpected target review state")
                    path = f"repos/{repo}/pulls/{pr_number}/reviews/{target['review_id']}/dismissals"
                    message = (f"Prior blocking findings reverified in the completed review at {context['current_sha']}. "
                               f"Evidence: {report.get('html_url')} ; artifact {artifact['result_artifact_id']}. "
                               f"Run: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{identity['run_id']}/attempts/{identity['run_attempt']}")
                    try:
                        _gh.rest("PUT", path, data={"message": message}, max_attempts=1)
                    except _gh.TransientOutageError:
                        # Read exact state, never blindly retry an ambiguous mutation.
                        pass
                    observed = _gh.rest("GET", f"repos/{repo}/pulls/{pr_number}/reviews/{target['review_id']}")
                    require(observed.get("state") == "DISMISSED"
                            and observed.get("id") == target["review_id"]
                            and observed.get("commit_id") == target["commit_id"]
                            and marker(observed.get("body"), pub.VERDICT_MARKER_PATTERN) == target["reference"],
                            "dismissal was not observed on exact target")
                    outcome = "dismissed"
                receipt["outcomes"].append({"review_id": target["review_id"], "outcome": outcome})
            receipt["reset_completed"] = True
        except (_gh.GitHubError, KeyError, TypeError, ValueError) as e:
            error = str(e)
            done = {item["review_id"] for item in receipt["outcomes"]}
            for target in snapshot["targets"]:
                if target["review_id"] not in done:
                    receipt["outcomes"].append({"review_id": target["review_id"], "outcome": "failed", "reason": error})
            _gh._annotate("error", "Self-dismissal failed; remaining blocks retained: " + error)
            links = "\n".join(f"- https://github.com/{repo}/pull/{pr_number}#pullrequestreview-{target['review_id']}" for target in snapshot["targets"])
            _gh._job_summary(f"### Review completed; reset failed\n\n{error}\n\nReport: {report.get('html_url')}\n\n"
                             f"A maintainer may manually dismiss the verified historical review(s):\n{links}\n")
        finally:
            # Separate from immutable accepted evidence; diagnostics upload harvests it.
            write_text(result_dir() / "reset-receipt.json", json.dumps(receipt, indent=2))
        if error is not None:
            raise _gh.TerminalError(error)
        return
    write_text(result_dir() / "reset-receipt.json", json.dumps(receipt, indent=2))


def main():
    try:
        {"guard-paths": guard_paths, "prepare": prepare, "validate": validate}[sys.argv[1]]()
    except (_gh.GitHubError, OSError, KeyError, TypeError, ValueError) as e:
        _gh._annotate("error", f"Review result preparation/validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
