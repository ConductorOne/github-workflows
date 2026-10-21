#!/usr/bin/env python3
"""Local fixture proof of hash-before-extract, Go gating, and event-SHA checks."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class IntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        (self.root / "tools").mkdir()
        for name in ("install-tool.sh", "verify-locked-go.sh", "gon-source.sh", "verify-release-checkout.sh", "setup-release-buildx.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.scripts / name)
        self.lock = self.root / "tools/tool-locks.txt"
        self.lock.write_text((ROOT / "tools/tool-locks.txt").read_text())
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.path_file = self.root / "github-path"
        self.env_file = self.root / "github-env"
        self.output_file = self.root / "github-output"
        self.calls = self.root / "calls"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        GITHUB_PATH=str(self.path_file), GITHUB_ENV=str(self.env_file),
                        GITHUB_OUTPUT=str(self.output_file), GOTOOLCHAIN="local",
                        TEST_OS="Linux", TEST_ARCH="x86_64", CALLS=str(self.calls))

    def executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + content)
        path.chmod(0o755)

    def run_script(self, name: str, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(["bash", str(self.scripts / name), *map(str, args)],
                                env=self.env, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def archive_member(self, tool: str, platform: str) -> str:
        if tool == "go":
            return "go/bin/go"
        if tool == "node":
            version = next(line.split()[1] for line in self.lock.read_text().splitlines()
                           if line.startswith("node ") and line.split()[2] == platform)
            os_name, arch = platform.split("/")
            node_arch = "x64" if arch == "amd64" else arch
            return f"node-{version}-{os_name}-{node_arch}/bin/node"
        return tool

    def fixture_download(self, tool: str, platform: str, member: str | None = None, mode: int = 0o755) -> Path:
        archive = self.root / "fixture.tar.gz"
        member = member or self.archive_member(tool, platform)
        with tarfile.open(archive, "w:gz") as tar:
            data = b"#!/bin/sh\necho 'binary must not be invoked' >&2\nexit 77\n"
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        rows = []
        for line in self.lock.read_text().splitlines():
            fields = line.split()
            if len(fields) == 5 and fields[0] == tool and fields[2] == platform:
                fields[3] = digest
                line = " ".join(fields)
            rows.append(line)
        self.lock.write_text("\n".join(rows) + "\n")
        self.env["ARCHIVE"] = str(archive)
        self.executable("uname", 'case "$1" in -s) echo "$TEST_OS";; -m) echo "$TEST_ARCH";; esac\n')
        self.executable("curl", 'printf "curl %s\\n" "$*" >> "$CALLS"\nwhile [ "$1" != -o ]; do shift; done\ncp "$ARCHIVE" "$2"\n')
        real_tar = shutil.which("tar")
        self.executable("tar", f'echo tar >> "$CALLS"\nexec "{real_tar}" "$@"\n')
        return archive

    def test_verified_install_never_invokes_binary(self) -> None:
        for tool in ("syft", "goreleaser", "go", "node"):
            for os_name, platform in (("Linux", "linux/amd64"), ("Darwin", "darwin/arm64")):
                with self.subTest(tool=tool, platform=platform):
                    self.fixture_download(tool, platform)
                    self.env.update(TEST_OS=os_name, TEST_ARCH="arm64" if os_name == "Darwin" else "x86_64")
                    dest = self.root / f"{tool}-{os_name}"
                    self.run_script("install-tool.sh", tool, dest)
                    installed = dest / self.archive_member(tool, platform)
                    self.assertTrue(installed.is_file())
                    self.assertIn(str(installed.parent), self.path_file.read_text().splitlines())
                    if tool == "go":
                        self.assertIn(f"GOROOT={dest}/go", self.env_file.read_text())

    def test_mismatch_fails_before_extraction_or_path_update(self) -> None:
        for tool in ("syft", "goreleaser", "go", "node"):
            with self.subTest(tool=tool):
                archive = self.fixture_download(tool, "linux/amd64")
                archive.write_bytes(archive.read_bytes() + b"tampered")
                self.calls.unlink(missing_ok=True)
                result = self.run_script("install-tool.sh", tool, self.root / "dest", ok=False)
                self.assertIn(f"::error title=GF13::{tool} sha256 mismatch", result.stderr)
                self.assertNotIn("tar\n", self.calls.read_text())
                self.assertFalse((self.root / "dest").exists())
                self.assertFalse(self.path_file.exists())

    def test_node_layout_mismatch_never_publishes_path(self) -> None:
        for os_name, platform in (("Linux", "linux/amd64"), ("Darwin", "darwin/arm64")):
            with self.subTest(platform=platform):
                self.fixture_download("node", platform, member="unexpected-node/bin/node")
                self.env.update(TEST_OS=os_name, TEST_ARCH="arm64" if os_name == "Darwin" else "x86_64")
                dest = self.root / f"wrong-layout-{os_name}"
                result = self.run_script("install-tool.sh", "node", dest, ok=False)
                self.assertIn("::error title=GF13::node package layout mismatch", result.stderr)
                self.assertTrue((dest / "unexpected-node/bin/node").is_file(), "fixture must reach extraction")
                self.assertFalse(self.path_file.exists())

    def test_nonexecutable_binary_never_publishes_environment(self) -> None:
        for tool in ("go", "node"):
            with self.subTest(tool=tool):
                self.fixture_download(tool, "linux/amd64", mode=0o644)
                result = self.run_script("install-tool.sh", tool, self.root / tool, ok=False)
                self.assertIn(f"::error title=GF13::{tool} package layout mismatch", result.stderr)
                self.assertFalse(self.path_file.exists())
                self.assertFalse(self.env_file.exists())

    def test_stale_version_url_and_duplicate_locks_fail_before_network(self) -> None:
        self.fixture_download("syft", "linux/amd64")
        original = self.lock.read_text()
        self.lock.write_text(original.replace("syft v1.42.3 linux/amd64", "syft v1.42.4 linux/amd64"))
        self.assertIn("does not match its URL", self.run_script("install-tool.sh", "syft", self.root / "dest", ok=False).stderr)
        self.assertFalse(self.calls.exists())
        row = next(line for line in original.splitlines() if line.startswith("syft ") and "linux/amd64" in line)
        self.lock.write_text(original + row + "\n")
        self.run_script("install-tool.sh", "syft", self.root / "dest", ok=False)
        self.assertFalse(self.calls.exists())
        self.env["TEST_ARCH"] = "unsupported"
        self.run_script("install-tool.sh", "syft", self.root / "dest", ok=False)
        self.assertFalse(self.calls.exists())

    def test_node_stale_url_and_duplicate_locks_fail_before_network(self) -> None:
        self.fixture_download("node", "linux/amd64")
        original = self.lock.read_text()
        row = next(line for line in original.splitlines() if line.startswith("node ") and "linux/amd64" in line)
        fields = row.split()
        fields[1] = "v0.0.0"
        self.lock.write_text(original.replace(row, " ".join(fields)))
        result = self.run_script("install-tool.sh", "node", self.root / "dest", ok=False)
        self.assertIn("does not match its URL", result.stderr)
        self.assertFalse(self.calls.exists())
        self.lock.write_text(original + row + "\n")
        self.run_script("install-tool.sh", "node", self.root / "dest", ok=False)
        self.assertFalse(self.calls.exists())

    def test_go_version_and_module_gates(self) -> None:
        self.executable("go", 'echo "go $*" >> "$CALLS"\necho "go version ${TEST_GO_VERSION:-go1.27.3} linux/arm64"\n')
        self.run_script("verify-locked-go.sh", "version")
        self.env["TEST_GO_VERSION"] = "go1.27.4"
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.calls.unlink()
        module = self.root / "go.mod"
        for version in ("1.13", "1.27", "1.27.3"):
            module.write_text(f"module example.com/fixture\n\ngo {version}\n")
            self.run_script("verify-locked-go.sh", "module", module)
        module.write_bytes(b"module example.com/fixture\r\n\r\ngo 1.27.3\r\n")
        self.run_script("verify-locked-go.sh", "module", module)
        module.write_bytes(b"module example.com/fixture\r\n\r\ngo 1.27.4\r\n")
        result = self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.assertIn(f"::error title=GF13::{module} requires Go 1.27.4, newer than locked go1.27.3", result.stderr)
        for directive in ("go 1.27.4", "go 1.28", "go 2.0", "go 1.28rc1", "", "go 1.27\ngo 1.26"):
            module.write_text(f"module example.com/fixture\n\n{directive}\n")
            self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.assertFalse(self.calls.exists(), "module gating must not invoke Go or download a toolchain")
        self.env["GOTOOLCHAIN"] = "auto"
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.assertFalse(self.calls.exists())

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def init_repo(self) -> str:
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        self.git("commit", "--allow-empty", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_event_sha_and_peeled_tags(self) -> None:
        commit = self.init_repo()
        self.env["EXPECTED_COMMIT"] = commit
        for name, annotated in (("v1.0.0", False), ("v1.0.1", True)):
            self.git("tag", *( ["-a", name, "-m", "annotated"] if annotated else [name]))
            self.env["RELEASE_TAG"] = name
            self.run_script("verify-release-checkout.sh", self.repo)
        self.assertIn(f"commit_sha={commit}", self.output_file.read_text())
        self.env["EXPECTED_COMMIT"] = self.git("rev-parse", "refs/tags/v1.0.1")
        self.assertIn("event SHA", self.run_script("verify-release-checkout.sh", self.repo, ok=False).stderr)
        self.env["EXPECTED_COMMIT"] = commit
        self.git("commit", "--allow-empty", "-qm", "different commit")
        self.run_script("verify-release-checkout.sh", self.repo, ok=False)
        self.env["EXPECTED_COMMIT"] = self.git("rev-parse", "HEAD")
        self.run_script("verify-release-checkout.sh", self.repo, ok=False)
        self.env["RELEASE_TAG"] = "missing"
        self.assertIn("Cannot peel", self.run_script("verify-release-checkout.sh", self.repo, ok=False).stderr)

    def test_gon_source_gates_and_build(self) -> None:
        commit = self.init_repo()
        text = self.lock.read_text()
        old_commit = next(line.split()[1] for line in text.splitlines() if line.startswith("gon "))
        self.lock.write_text(text.replace(old_commit, commit))
        self.run_script("gon-source.sh", "ref")
        self.assertEqual(self.output_file.read_text(), f"ref={commit}\n")
        self.env.update(GOFLAGS="-mod=readonly", GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org")
        self.executable("go", 'echo "go $*" >> "$CALLS"\nif [ "$1" = version ]; then echo "go version go1.27.3 linux/arm64"; else [[ "$PWD" == */cmd/gon ]]; [ "$1 $2 $3" = "build -trimpath -o" ]; printf fixture > "$4"; fi\n')
        module = self.repo / "go.mod"
        module.write_text("module fixture\ngo 1.13\n")
        (self.repo / "cmd/gon").mkdir(parents=True)
        self.run_script("gon-source.sh", "build", self.repo, self.root / "gon-bin")
        self.assertTrue((self.root / "gon-bin/gon").exists())
        self.calls.unlink()
        module.write_text("module fixture\ngo 1.28\n")
        self.run_script("gon-source.sh", "build", self.repo, self.root / "new-gon", ok=False)
        self.assertNotIn("build", self.calls.read_text())
        self.env["GOSUMDB"] = "off"
        self.calls.unlink()
        self.run_script("gon-source.sh", "build", self.repo, self.root / "new-gon", ok=False)
        self.assertFalse(self.calls.exists())
        self.env["GOSUMDB"] = "sum.golang.org"
        self.git("commit", "--allow-empty", "-qm", "changed source")
        self.run_script("gon-source.sh", "build", self.repo, self.root / "new-gon", ok=False)
        self.assertFalse(self.calls.exists(), "mismatched gon commit must fail before invoking Go")

    def test_bundled_buildx_fails_without_downloading(self) -> None:
        self.executable("docker", 'echo "$*" >> "$CALLS"\nif [ "${FAIL_BUILDX:-}" = 1 ] && [ "$*" = "buildx version" ]; then exit 1; fi\n')
        self.run_script("setup-release-buildx.sh")
        commands = self.calls.read_text().splitlines()
        self.assertEqual(commands[0], "buildx version")
        self.assertIn("--driver-opt image=moby/buildkit@sha256:", commands[1])
        self.assertEqual(commands[2], "buildx inspect --bootstrap")
        self.calls.unlink()
        self.env["FAIL_BUILDX"] = "1"
        self.run_script("setup-release-buildx.sh", ok=False)
        self.assertEqual(self.calls.read_text(), "buildx version\n")

    def test_workflow_security_wiring(self) -> None:
        workflow = json.loads(subprocess.check_output(["yq", ".", str(ROOT / ".github/workflows/release.yaml")], text=True))
        jobs = workflow["jobs"]
        self.assertEqual(jobs["determine-workflows-ref"]["outputs"]["ref"], "${{ job.workflow_sha }}")
        for job_name, job in jobs.items():
            steps = job["steps"]
            installs = [i for i, step in enumerate(steps) if any(token in step.get("run", "") for token in ("install-tool.", "gon-source.sh build", "setup-release-buildx.sh")) or "cosign-installer@" in step.get("uses", "") or "setup-qemu-action@" in step.get("uses", "")]
            credentials = [i for i, step in enumerate(steps) if "secrets." in str(step) or "configure-aws-credentials@" in step.get("uses", "")]
            if installs and credentials:
                self.assertLess(max(installs), min(credentials), job_name)
            for step in steps:
                if "install-tool." in step.get("run", ""):
                    self.assertNotIn("\n", step["run"], job_name)
                    self.assertEqual(job["env"]["GOTOOLCHAIN"], "local")
                if step.get("with", {}).get("path") in ("_caller", "_connector"):
                    self.assertEqual(step["with"]["ref"], "${{ github.sha }}")
                if "verify-release-checkout.sh" in step.get("run", ""):
                    self.assertEqual(step["env"]["EXPECTED_COMMIT"], "${{ github.sha }}")
                if "cosign-installer@" in step.get("uses", ""):
                    self.assertEqual(step["with"]["cosign-release"], "v2.5.2")
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
