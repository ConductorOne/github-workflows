#!/usr/bin/env python3
"""Publish structured PR review output after verifying the PR head is unchanged."""

import json
import os
import posixpath
import subprocess
import sys
from typing import Any


REVIEW_STATE_TEMPLATE = (
    '<!-- review-state: {{"last_reviewed_sha": "{current_sha}", '
    '"base_sha": "{base_sha}", "workflow_ref": "{workflow_ref}"}} -->'
)


def load_json(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def load_context() -> dict[str, Any]:
    return load_json(os.path.join(".github", "pr-context.json"))


def load_review_output() -> dict[str, Any]:
    raw = os.environ.get("CLAUDE_REVIEW_OUTPUT", "")
    if not raw.strip():
        print("CLAUDE_REVIEW_OUTPUT is empty", file=sys.stderr)
        sys.exit(2)
    try:
        output = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"CLAUDE_REVIEW_OUTPUT is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(output, dict):
        print("CLAUDE_REVIEW_OUTPUT must be a JSON object", file=sys.stderr)
        sys.exit(2)
    return output


def gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True)


def verify_head(context: dict[str, Any]) -> None:
    endpoint = f"repos/{context['repository']}/pulls/{context['pr_number']}"
    pr = json.loads(gh(["api", endpoint]).stdout)
    actual_sha = pr["head"]["sha"]
    expected_sha = context["current_sha"]
    if actual_sha != expected_sha:
        print(
            f"PR head changed from {expected_sha} to {actual_sha}; not posting",
            file=sys.stderr,
        )
        sys.exit(3)


def safe_path(path: Any) -> str:
    text = str(path or "").strip()
    normalized = posixpath.normpath(text)
    if (
        not text
        or text.startswith("/")
        or normalized.startswith("../")
        or normalized == ".."
    ):
        raise ValueError(f"unsafe path {text!r}")
    return normalized


def line_number(line: Any) -> int:
    value = int(line)
    if value < 1:
        raise ValueError("line must be positive")
    return value


def clean_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("text field must not be empty")
    return text[:limit].rstrip()


def issue_entries(output: dict[str, Any], key: str) -> list[dict[str, Any]]:
    entries = output.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")
    return entries


def validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("finding entries must be objects")
    return {
        "path": safe_path(entry.get("path")),
        "line": line_number(entry.get("line")),
        "confidence": clean_text(entry.get("confidence"), 40),
        "summary": clean_text(entry.get("summary"), 500),
        "details": clean_text(entry.get("details") or entry.get("summary"), 2000),
    }


def validated_output(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_summary": clean_text(output.get("review_summary"), 1200),
        "security_issues": [
            validate_entry(entry) for entry in issue_entries(output, "security_issues")
        ],
        "correctness_issues": [
            validate_entry(entry) for entry in issue_entries(output, "correctness_issues")
        ],
        "suggestions": [
            validate_entry(entry) for entry in issue_entries(output, "suggestions")
        ],
    }


def criteria_status() -> str:
    status_path = os.path.join(".github", "review-criteria.json")
    if not os.path.exists(status_path):
        return "none loaded - criteria status file missing"
    status = load_json(status_path)
    return str(status.get("message") or "none loaded - criteria status unknown")


def resolved_count() -> int:
    resolved_path = os.path.join(".github", "resolved-threads.json")
    if not os.path.exists(resolved_path):
        return 0
    resolved = load_json(resolved_path)
    return int(resolved.get("resolved_count") or 0)


def post_inline_comment(
    context: dict[str, Any],
    entry: dict[str, Any],
    prefix: str,
) -> None:
    body = (
        f"{prefix} {entry['summary']}\n\n"
        f"Confidence: {entry['confidence']}\n\n"
        f"{entry['details']}"
    )
    gh(
        [
            "api",
            f"repos/{context['repository']}/pulls/{context['pr_number']}/comments",
            "-f",
            f"body={body}",
            "-f",
            f"commit_id={context['current_sha']}",
            "-f",
            f"path={entry['path']}",
            "-F",
            f"line={entry['line']}",
            "-f",
            "side=RIGHT",
        ]
    )


def format_lines(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "None found."
    return "\n".join(
        f"- `{entry['path']}:{entry['line']}` [{entry['confidence']}] {entry['summary']}"
        for entry in entries
    )


def format_agent_prompt(
    security: list[dict[str, Any]],
    correctness: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
) -> str:
    if not security and not correctness and not suggestions:
        return ""

    def section(title: str, entries: list[dict[str, Any]]) -> list[str]:
        if not entries:
            return [f"## {title}", "", "None."]
        lines = [f"## {title}", ""]
        for entry in entries:
            lines.extend(
                [
                    f"In `{entry['path']}`:",
                    f"- Around line {entry['line']}: {entry['details']}",
                    "",
                ]
            )
        return lines

    lines = [
        "<details>",
        "<summary>Prompt for AI agents</summary>",
        "",
        "```",
        "Verify each finding against the current code and only fix it if needed.",
        "",
    ]
    lines.extend(section("Security Issues", security))
    lines.append("")
    lines.extend(section("Correctness Issues", correctness))
    lines.append("")
    lines.extend(section("Suggestions", suggestions))
    lines.extend(["```", "", "</details>"])
    return "\n".join(lines)


def review_mode_line(context: dict[str, Any]) -> str:
    if context.get("review_mode") == "incremental" and context.get("last_reviewed_sha"):
        return f"_Review mode: incremental since `{str(context['last_reviewed_sha'])[:12]}`_"
    return "_Review mode: full_"


def review_run_line(context: dict[str, Any]) -> str:
    url = context.get("review_run_url")
    return f"[View review run]({url})" if url else ""


def render_summary(context: dict[str, Any], output: dict[str, Any]) -> str:
    security = output["security_issues"]
    correctness = output["correctness_issues"]
    suggestions = output["suggestions"]
    blocking_count = len(security) + len(correctness)
    suggestion_count = len(suggestions)
    state = REVIEW_STATE_TEMPLATE.format(
        current_sha=context["current_sha"],
        base_sha=context["current_base_sha"],
        workflow_ref=context["workflow_ref"],
    )
    lines = [
        f"{context['summary_heading']} {context.get('pr_title') or 'PR review'}",
        "",
        (
            f"**Blocking Issues: {blocking_count}** | "
            f"**Suggestions: {suggestion_count}** | "
            f"**Threads Resolved: {resolved_count()}**"
        ),
        review_mode_line(context),
        f"**Criteria:** Criteria status: {criteria_status()}.",
    ]
    run_line = review_run_line(context)
    if run_line:
        lines.append(run_line)
    lines.extend(
        [
            "",
            "### Review Summary",
            output["review_summary"],
            "",
            "### Security Issues",
            format_lines(security),
            "",
            "### Correctness Issues",
            format_lines(correctness),
            "",
            "### Suggestions",
            "None." if not suggestions else format_lines(suggestions),
            "",
            state,
        ]
    )
    agent_prompt = format_agent_prompt(security, correctness, suggestions)
    if agent_prompt:
        lines.extend(["", agent_prompt])
    return "\n".join(lines).rstrip() + "\n"


def post_summary(context: dict[str, Any], body: str) -> None:
    comment_id = context.get("summary_comment_id")
    if comment_id:
        gh(
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{context['repository']}/issues/comments/{comment_id}",
                "-f",
                f"body={body}",
            ]
        )
    else:
        gh(
            [
                "api",
                f"repos/{context['repository']}/issues/{context['pr_number']}/comments",
                "-f",
                f"body={body}",
            ]
        )


def post_verdict(context: dict[str, Any], has_blockers: bool) -> None:
    flag = "--request-changes" if has_blockers else "--comment"
    body = (
        "Blocking issues found - see review comments."
        if has_blockers
        else "No blocking issues found."
    )
    gh(
        [
            "pr",
            "review",
            str(context["pr_number"]),
            "--repo",
            context["repository"],
            flag,
            "-b",
            body,
        ]
    )


def main() -> None:
    context = load_context()
    output = validated_output(load_review_output())
    os.makedirs(".github", exist_ok=True)
    with open(os.path.join(".github", "review-output.json"), "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")
    verify_head(context)
    for entry in output["security_issues"]:
        post_inline_comment(context, entry, "🔴 Security:")
    for entry in output["correctness_issues"]:
        post_inline_comment(context, entry, "🟠 Bug:")
    for entry in output["suggestions"]:
        post_inline_comment(context, entry, "🟡 Suggestion:")
    post_summary(context, render_summary(context, output))
    post_verdict(
        context,
        bool(output["security_issues"] or output["correctness_issues"]),
    )


if __name__ == "__main__":
    main()
