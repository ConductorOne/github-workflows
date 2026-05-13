#!/usr/bin/env python3
"""Post the final PR review verdict after verifying the PR head is unchanged."""

import json
import os
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
    if len(sys.argv) < 2:
        print("usage: post-verdict <comment|request-changes> [body]", file=sys.stderr)
        sys.exit(2)

    verdict = sys.argv[1]
    if verdict not in {"comment", "request-changes"}:
        print("verdict must be comment or request-changes", file=sys.stderr)
        sys.exit(2)

    body = " ".join(sys.argv[2:]).strip() or sys.stdin.read().strip()
    if not body:
        print("review body is required", file=sys.stderr)
        sys.exit(2)

    context = load_context()
    verify_head(context)
    flag = "--comment" if verdict == "comment" else "--request-changes"
    subprocess.run(
        [
            "gh",
            "pr",
            "review",
            str(context["pr_number"]),
            "--repo",
            context["repository"],
            flag,
            "-b",
            body,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
