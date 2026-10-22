#!/usr/bin/env python3
"""Reject unreviewed action downloads in credential-bearing release jobs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ACTIONS = {
    line.strip() for line in (ROOT / "tools/privileged-actions.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}
LOCAL_ACTIONS = {
    "./_workflows/.github/actions/ca-trust-guard",
    "./_workflows/.github/actions/locked-tools",
}
PRIVILEGED_JOBS = {
    "validate-inputs", "goreleaser-binaries", "goreleaser-windows",
    "goreleaser-docker", "publish-release-manifest", "record-registry-api",
    "verify-release", "notify-release-failure",
}


def load_yaml(path):
    return json.loads(subprocess.check_output(["yq", ".", str(path)], text=True))


def check_steps(steps, location):
    errors = []
    for step in steps:
        action = step.get("uses")
        if action is None:
            continue
        if action in LOCAL_ACTIONS:
            continue
        name, separator, ref = action.partition("@")
        components = name.split("/")
        first_party = (len(components) >= 2 and components[0] == "actions"
                       and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                               for part in components[1:]))
        if (not first_party and name not in ALLOWED_ACTIONS and action not in ALLOWED_ACTIONS) or not separator:
            errors.append(f"{location}: action not allowlisted: {action}")
        elif not re.fullmatch(r"[0-9a-f]{40}", ref):
            errors.append(f"{location}: action must use a full commit SHA: {action}")
    return errors


def check_local_bootstrap(steps, location):
    errors = []
    ready = False
    for step in steps:
        action = step.get("uses", "")
        settings = step.get("with") or {}
        checkout = action.startswith("actions/checkout@")
        if "with" in step and not isinstance(step["with"], dict):
            errors.append(f"{location}: action inputs must be a mapping")
            if checkout:
                ready = False
            continue
        if checkout:
            path = settings.get("path", "")
            if not isinstance(path, str):
                errors.append(f"{location}: checkout path must be a string")
                ready = False
                continue
            rel = os.path.normpath(str(path or "."))
            if (rel in (".", "_workflows") or rel.startswith("_workflows/")
                    or rel.startswith("..") or os.path.isabs(rel) or "${{" in str(path)):
                ready = (re.fullmatch(r"actions/checkout@[0-9a-f]{40}", action) is not None
                         and settings.get("repository") == "ConductorOne/github-workflows"
                         and settings.get("path") == "_workflows"
                         and settings.get("ref") == "${{ job.workflow_sha }}"
                         and settings.get("persist-credentials") is False
                         and "token" not in settings and "if" not in step)
        if action in LOCAL_ACTIONS and not ready:
            errors.append(f"{location}: local action requires the preceding pinned workflow checkout")
    return errors


def references_secrets(value):
    if isinstance(value, str):
        # Match the context itself too (for example, toJSON(secrets)).
        return re.search(r"\bsecrets\b", value) is not None
    if isinstance(value, dict):
        return any(references_secrets(item) for item in value.values())
    if isinstance(value, list):
        return any(references_secrets(item) for item in value)
    return False


def check_workflow(workflow):
    errors = []
    for name, job in workflow["jobs"].items():
        permissions = job.get("permissions", workflow.get("permissions", {}))
        oidc = permissions == "write-all" or (
            isinstance(permissions, dict) and permissions.get("id-token") == "write")
        inherited_secret = references_secrets(workflow.get("env", {}))
        if name in PRIVILEGED_JOBS or oidc or job.get("secrets") or inherited_secret or references_secrets(job):
            errors.extend(check_steps([job] if "uses" in job else job.get("steps", []), name))
            errors.extend(check_local_bootstrap(job.get("steps", []), name))
    return errors


def check_repository(root):
    errors = check_workflow(load_yaml(root / ".github/workflows/release.yaml"))
    for action in sorted(LOCAL_ACTIONS):
        path = root / action.removeprefix("./_workflows/") / "action.yml"
        errors.extend(check_steps(load_yaml(path)["runs"]["steps"], action))
    return errors


if __name__ == "__main__":
    failures = check_repository(ROOT)
    for failure in failures:
        print(failure, file=sys.stderr)
    sys.exit(bool(failures))
