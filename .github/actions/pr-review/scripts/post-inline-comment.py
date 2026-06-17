#!/usr/bin/env python3
"""Post an inline PR review comment after verifying the PR head is unchanged."""

import json
import os
import posixpath
import subprocess
import sys


def load_context() -> dict:
    with open(os.path.join(".github", "pr-context.json")) as f:
        return json.load(f)


def verify_head(context: dict) -> None:
    endpoint = f"repos/{context['repository']}/pulls/{context['pr_number']}"
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        check=True,
    )
    pr = json.loads(result.stdout)
    actual_sha = pr["head"]["sha"]
    expected_sha = context["current_sha"]
    if actual_sha != expected_sha:
        print(
            f"PR head changed from {expected_sha} to {actual_sha}; not posting",
            file=sys.stderr,
        )
        sys.exit(3)


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print("usage: post-inline-comment <path> <line> [RIGHT|LEFT]", file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1].strip()
    normalized = posixpath.normpath(path)
    if (
        not path
        or path.startswith("/")
        or normalized.startswith("../")
        or normalized == ".."
    ):
        print("refusing unsafe path", file=sys.stderr)
        sys.exit(2)

    try:
        line = int(sys.argv[2])
    except ValueError:
        print("line must be an integer", file=sys.stderr)
        sys.exit(2)

    side = sys.argv[3] if len(sys.argv) == 4 else "RIGHT"
    if side not in {"RIGHT", "LEFT"}:
        print("side must be RIGHT or LEFT", file=sys.stderr)
        sys.exit(2)

    body = sys.stdin.read().strip()
    if not body:
        print("comment body is required on stdin", file=sys.stderr)
        sys.exit(2)

    context = load_context()
    verify_head(context)
    subprocess.run(
        [
            "gh",
            "api",
            f"repos/{context['repository']}/pulls/{context['pr_number']}/comments",
            "-f",
            f"body={body}",
            "-f",
            f"commit_id={context['current_sha']}",
            "-f",
            f"path={normalized}",
            "-F",
            f"line={line}",
            "-f",
            f"side={side}",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
