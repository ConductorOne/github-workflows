#!/usr/bin/env python3
"""Read authenticated v1 entries; reconcile public release digests, never payload hashes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DIST = "https://dist.conductorone.com"
GITHUB = "https://api.github.com"
ISSUER = "https://token.actions.githubusercontent.com"
PINNED = {
    "https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@refs/tags/v4",
    "https://github.com/ConductorOne/github-axiomatic-workflows/.github/workflows/release.yaml@refs/tags/v1",
    "https://github.com/ConductorOne/multipass-workflows/.github/workflows/release-artifacts.yaml@refs/tags/v1",
}
WINDOW = 6 * 3600
FRESHNESS = 2 * 3600


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url):
    if urlsplit(url).scheme != "https" or urlsplit(url).netloc not in {"dist.conductorone.com", "api.github.com"}:
        raise ValueError("unexpected lookup host")
    headers = {"User-Agent": "release-rekor-monitor"}
    if url.startswith(GITHUB + "/") and os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=30) as response:
        return response.read()


def checkpoint_progress(checkpoint, previous, now):
    # Signed checkpoints have no timestamp. Track the first observation of the
    # SAME origin/size/root; a new note signature alone is not log progress.
    fields = checkpoint.replace("\\n", "\n").splitlines()
    if len(fields) < 3 or not fields[1].isdigit():
        raise ValueError("malformed checkpoint")
    view = hashlib.sha256("\n".join(fields[:3]).encode()).hexdigest()
    first = previous.get("first_observed", now) if previous.get("view") == view else now
    if first > now:
        raise ValueError("checkpoint observation clock moved backwards")
    return {"view": view, "first_observed": first}, now - first > FRESHNESS


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def run_identity(entry, get=fetch):
    identity = entry.get("identity", {})
    san = identity.get("subjectAlternativeName", "")
    if san not in PINNED or identity.get("issuer") != ISSUER:
        return "ANOMALY", "unexpected workflow identity"
    repository_uri = identity.get("sourceRepositoryURI", "")
    repository = repository_uri.removeprefix("https://github.com/")
    run_uri = identity.get("runInvocationURI", "")
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([0-9]+)(?:/attempts/([0-9]+))?", run_uri)
    if not repository_uri.startswith("https://github.com/") or not match or match[1] != repository or not identity.get("sourceRepositoryDigest"):
        return "UNKNOWN", "missing or non-joinable certificate run extensions"
    try:
        endpoint = f"{GITHUB}/repos/{repository}/actions/runs/{match[2]}"
        if match[3]:
            endpoint += f"/attempts/{match[3]}"
        run = json.loads(get(endpoint))
    except HTTPError as error:
        missing_public_run = error.code == 404 and identity.get("sourceRepositoryVisibilityAtSigning") == "public"
        return ("ANOMALY", "missing run") if missing_public_run else ("UNKNOWN", "run lookup unavailable")
    if run.get("repository", {}).get("full_name", "").lower() != repository.lower() or run.get("head_sha") != identity["sourceRepositoryDigest"]:
        return "ANOMALY", "run repository or commit differs from certificate"
    if run.get("status") != "completed":
        return "PENDING", "run has not concluded"
    # GitHub returns a call spelling such as @v4 plus a separate canonical ref.
    references = {item.get("path", "").rsplit("@", 1)[0] + "@" + item.get("ref", "")
                  for item in run.get("referenced_workflows", [])}
    if san.removeprefix("https://github.com/") not in references:
        return "ANOMALY", "run does not reference the pinned workflow"
    signed_at = entry["integrated_time"]
    if not timestamp(run["run_started_at"]) - 600 <= signed_at <= timestamp(run["updated_at"]) + 600:
        return "ANOMALY", "signature time is not plausible for the run"
    return "MATCH", "real concluded run on the pinned workflow"


def release_data(identity, get=fetch):
    repository = identity.get("sourceRepositoryURI", "").removeprefix("https://github.com/")
    ref = identity.get("sourceRepositoryRef", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not ref.startswith("refs/tags/"):
        return None, "UNKNOWN", "missing repository/tag or unknown storage alias"
    tag = ref.removeprefix("refs/tags/")
    if not tag or "/" in tag or tag in {".", ".."}:
        return None, "UNKNOWN", "non-joinable release tag"
    catalog = json.loads(get(DIST + "/index.json"))["catalogs"].get(repository)
    if not catalog or catalog.get("hidden"):
        return None, "UNKNOWN", "catalog hidden or storage alias not published"
    base = f"{DIST}/releases/{repository}"
    index = json.loads(get(base + "/index.json"))
    release = index["semvers"].get(tag)
    if not release:
        return None, "PENDING", "release not published yet"
    url = f"{base}/{quote(tag, safe='')}/manifest.json"
    if release.get("manifest") != url:
        return None, "UNKNOWN", "unexpected manifest location"
    raw = get(url)
    manifest = json.loads(raw)
    org, name = repository.split("/")
    if (manifest.get("org"), manifest.get("name"), manifest.get("semver")) != (org, name, tag):
        return None, "UNKNOWN", "manifest release identity differs from index"
    digests = {hashlib.sha256(raw).hexdigest()}
    digests.update(asset["sha256"].lower() for asset in manifest.get("assets", {}).values() if re.fullmatch(r"[0-9a-fA-F]{64}", asset.get("sha256", "")))
    # There is no signed flag today. Only public-ECR image indexes belong to the
    # current signed-image set; private Lambda images do not grant ambiguity budget.
    budget = sum(image.get("isIndex") is True and image.get("uri", "").startswith("public.ecr.aws/")
                 for image in manifest.get("images", {}).values())
    return {"key": f"{repository}@{tag}", "digests": digests, "budget": budget}, "READY", "public release loaded"


def evaluate(entry, state, now, get=fetch):
    identity = entry.get("identity", {})
    row = {"uuid": entry["uuid"], "log_index": entry["index"],
           "san": identity.get("subjectAlternativeName"), "repository": identity.get("sourceRepositoryURI"),
           "run_uri": identity.get("runInvocationURI"), "subject_digest": entry.get("digest"),
           "run_identity_match": "UNKNOWN", "artifact_authorized": "UNKNOWN"}
    if entry.get("error"):
        row.update(status="INFORMATIONAL" if entry["error"] == "UNAUTHENTICATED-SAN" else "MONITOR-ERROR", reason=entry["error"])
        return row
    if entry.get("kind") != "hashedrekord" or not entry.get("digest"):
        row.update(status="UNKNOWN", reason="opaque or unsupported signed payload; never compare payloadHash to artifact digest")
        return row
    first = state.setdefault("first_seen", {}).setdefault(entry["uuid"], now)
    expired = now - first >= WINDOW
    run_status, reason = run_identity(entry, get)
    row["run_identity_match"] = run_status
    if run_status != "MATCH":
        row.update(status="ANOMALY" if run_status == "PENDING" and expired else run_status, reason=reason)
        return row
    try:
        release, release_status, reason = release_data(identity, get)
    except HTTPError as error:
        release, release_status, reason = None, "UNKNOWN", f"release lookup unavailable ({error.code})"
    if release_status != "READY":
        row.update(status="ANOMALY" if release_status == "PENDING" and expired else release_status, reason=reason)
        return row
    if entry["digest"] in release["digests"]:
        row.update(status="MATCHED", artifact_authorized="AUTHORIZED", reason="signed-byte digest recorded in this public release")
        return row
    if not expired:
        row.update(status="PENDING", reason="digest not recorded yet; bounded reconciliation window")
        return row
    hashes = state.setdefault("unmatched_hashes", {}).setdefault(release["key"], [])
    if entry["digest"] not in hashes:
        hashes.append(entry["digest"])
    if len(hashes) <= release["budget"]:
        row.update(status="UNKNOWN", reason="unmatched hash within this release's opaque image-payload budget")
    else:
        row.update(status="ANOMALY", artifact_authorized="UNRECORDED", reason="unexpected digest exceeds signed-image ambiguity budget")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="monitor-state")
    parser.add_argument("--identities", default="monitor-state/identities.jsonl")
    parser.add_argument("--entry-helper", required=True)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    directory = Path(args.state_dir)
    state_path = directory / "reconciliation.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    elif args.bootstrap:
        state = {}
    else:
        raise ValueError("missing reconciliation state; refusing to reset monitor history")
    now = int(time.time())
    progress, stale = checkpoint_progress((directory / "checkpoint.txt").read_text(), state.get("checkpoint", {}), now)
    observations = {item["UUID"]: item for item in state.get("pending", [])}
    for line in Path(args.identities).read_text().splitlines():
        item = json.loads(line)
        observations[item["UUID"]] = item
    rows = []
    if observations:
        checked = subprocess.run([args.entry_helper, "-checkpoint", str(directory / "checkpoint.txt")],
                                 input="".join(json.dumps(item) + "\n" for item in observations.values()),
                                 capture_output=True, text=True, check=True)
        entries = [json.loads(line) for line in checked.stdout.splitlines()]
        if {item["uuid"] for item in entries} != set(observations):
            raise ValueError("proof helper omitted observations")
        cache = {}
        def cached_get(url):
            if url not in cache:
                cache[url] = fetch(url)
            return cache[url]
        rows = [evaluate(entry, state, now, cached_get) for entry in entries]
    state["checkpoint"] = progress
    retry = {row["uuid"] for row in rows if row["status"] in {"PENDING", "MONITOR-ERROR"}
             or (row["status"] == "UNKNOWN" and "unavailable" in row["reason"]
                 and now - state.get("first_seen", {}).get(row["uuid"], now) < WINDOW)}
    state["pending"] = [item for uuid, item in observations.items() if uuid in retry]
    state["first_seen"] = {key: value for key, value in state.get("first_seen", {}).items() if key in retry}
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    report = {"alert_channel_placeholder": "#ops-testing-new-alerts", "checkpoint_stale": stale, "entries": rows}
    diagnostics = directory / "diagnostics.log"
    chain_failures = diagnostics.read_text().count("Certificate chain for log entry") if diagnostics.exists() else 0
    report["unauthenticated_san_diagnostics"] = {"status": "INFORMATIONAL", "label": "UNAUTHENTICATED-SAN",
                                               "count": chain_failures, "run_identity_match": "UNKNOWN"}
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    Path(args.identities).write_text("")
    print(json.dumps(report, indent=2))
    if stale or any(row["status"] in {"ANOMALY", "MONITOR-ERROR"} for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"monitor lookup or proof check failed: {error}", file=sys.stderr)
        sys.exit(1)
