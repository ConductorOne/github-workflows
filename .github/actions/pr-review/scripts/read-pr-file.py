#!/usr/bin/env python3
"""Read a file from the PR head commit without checking out PR code."""

import json
import os
import posixpath
import subprocess
import sys
from urllib.parse import quote


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: read-pr-file <path>", file=sys.stderr)
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

    with open(os.path.join(".github", "pr-context.json")) as f:
        context = json.load(f)

    head_repo = context["head_repo"]
    current_sha = context["current_sha"]
    encoded_path = quote(normalized, safe="/")
    endpoint = f"repos/{head_repo}/contents/{encoded_path}?ref={current_sha}"
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw", endpoint],
        check=True,
        text=True,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
