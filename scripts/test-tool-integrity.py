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
import zipfile


ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github/actions/locked-tools"
GUARD_ACTION = "./_workflows/.github/actions/ca-trust-guard"
TOOLS_ACTION = "./_workflows/.github/actions/locked-tools"
GO_GATE_ENV = {
    "EXPECTED_GO_VERSION": "${{ fromJSON(steps.tools.outputs.versions || '{}').go }}",
    "EXPECTED_LOCK_SHA256": "${{ steps.tools.outputs.lock_sha256 }}",
    "LOCK_PATH": "${{ steps.tools.outputs.lock_path }}",
}


class IntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.action_dir = self.root / ".github/actions/locked-tools"
        self.action_dir.mkdir(parents=True)
        for name in ("install-tool.sh", "verify-locked-go.sh", "gon-source.sh"):
            shutil.copyfile(ACTION / name, self.action_dir / name)
        shutil.copyfile(ACTION / "locked-tools.sh", self.action_dir / "locked-tools.sh")
        shutil.copyfile(ACTION / "install-tool.ps1", self.action_dir / "install-tool.ps1")
        for name in ("verify-release-checkout.sh", "setup-release-buildx.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.scripts / name)
        self.lock = self.action_dir / "tool-locks.txt"
        self.lock.write_text((ACTION / "tool-locks.txt").read_text())
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.path_file = self.root / "github-path"
        self.env_file = self.root / "github-env"
        self.output_file = self.root / "github-output"
        self.calls = self.root / "calls"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        GITHUB_PATH=str(self.path_file), GITHUB_ENV=str(self.env_file),
                        GITHUB_OUTPUT=str(self.output_file), GOTOOLCHAIN="local",
                        TEST_OS="Linux", TEST_ARCH="x86_64", CALLS=str(self.calls),
                        GITHUB_WORKSPACE=str(self.workspace), LOCKED_TOOLS_DEST=str(self.root / "dest"))
        self.env.pop("TOOL_LOCK_FILE", None)
        for name in ("EXPECTED_GO_VERSION", "EXPECTED_LOCK_SHA256", "LOCK_PATH"):
            self.env.pop(name, None)

    def executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + content)
        path.chmod(0o755)

    def run_script(self, name: str, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        script = self.action_dir / name
        if not script.exists():
            script = self.scripts / name
        result = subprocess.run(["bash", str(script), *map(str, args)],
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
            if os_name == "windows":
                return f"node-{version}-win-{node_arch}/node.exe"
            return f"node-{version}-{os_name}-{node_arch}/bin/node"
        return tool

    def fixture_download(self, tool: str, platform: str, member: str | None = None,
                         mode: int = 0o755, data: bytes | None = None) -> Path:
        is_zip = tool == "minisign" or (tool == "node" and platform == "windows/amd64")
        archive = self.root / ("fixture.zip" if is_zip else "fixture.tar.gz")
        member = member or self.archive_member(tool, platform)
        if data is None:
            data = b"#!/bin/sh\necho 'binary must not be invoked' >&2\nexit 77\n"
        if is_zip:
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(member, data)
        else:
            with tarfile.open(archive, "w:gz") as tar:
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

    def test_go_linux_arm64_verified_install(self) -> None:
        for machine in ("aarch64", "arm64"):
            with self.subTest(machine=machine):
                self.fixture_download("go", "linux/arm64")
                self.env.update(TEST_OS="Linux", TEST_ARCH=machine)
                dest = self.root / f"go-linux-{machine}"
                self.run_script("install-tool.sh", "go", dest)
                binary = dest / "go/bin/go"
                self.assertTrue(binary.is_file())
                self.assertTrue(os.access(binary, os.X_OK))
                self.assertIn(str(binary.parent), self.path_file.read_text().splitlines())
                self.assertIn(f"GOROOT={dest}/go", self.env_file.read_text())
                row = next(line.split() for line in self.lock.read_text().splitlines()
                           if line.startswith("go ") and line.split()[2] == "linux/arm64")
                self.assertEqual(row[4], f"https://go.dev/dl/{row[1]}.linux-arm64.tar.gz")
                self.assertIn(row[4], self.calls.read_text())

    def test_go_linux_arm64_hash_mismatch_precedes_extraction(self) -> None:
        archive = self.fixture_download("go", "linux/arm64")
        archive.write_bytes(archive.read_bytes() + b"tampered")
        self.env.update(TEST_OS="Linux", TEST_ARCH="aarch64")
        dest = self.root / "bad-go-arm64"
        result = self.run_script("install-tool.sh", "go", dest, ok=False)
        self.assertIn("::error title=locked-tools::go sha256 mismatch", result.stderr)
        self.assertNotIn("tar\n", self.calls.read_text())
        self.assertFalse(dest.exists())
        self.assertFalse(self.path_file.exists())
        self.assertFalse(self.env_file.exists())

    def test_mismatch_fails_before_extraction_or_path_update(self) -> None:
        for tool in ("syft", "goreleaser", "go", "node"):
            with self.subTest(tool=tool):
                archive = self.fixture_download(tool, "linux/amd64")
                archive.write_bytes(archive.read_bytes() + b"tampered")
                self.calls.unlink(missing_ok=True)
                result = self.run_script("install-tool.sh", tool, self.root / "dest", ok=False)
                self.assertIn(f"::error title=locked-tools::{tool} sha256 mismatch", result.stderr)
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
                self.assertIn("::error title=locked-tools::node package layout mismatch", result.stderr)
                self.assertTrue((dest / "unexpected-node/bin/node").is_file(), "fixture must reach extraction")
                self.assertFalse(self.path_file.exists())

    def test_nonexecutable_binary_never_publishes_environment(self) -> None:
        for tool in ("go", "node"):
            with self.subTest(tool=tool):
                self.fixture_download(tool, "linux/amd64", mode=0o644)
                result = self.run_script("install-tool.sh", tool, self.root / tool, ok=False)
                self.assertIn(f"::error title=locked-tools::{tool} package layout mismatch", result.stderr)
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

    def go_contract(self, version: str = "go1.27.3", lock: Path | None = None) -> None:
        lock = lock or self.lock
        self.env.update(EXPECTED_GO_VERSION=version, LOCK_PATH=str(lock.resolve()),
                        EXPECTED_LOCK_SHA256=hashlib.sha256(lock.read_bytes()).hexdigest())

    def mock_go_gate(self) -> None:
        self.executable("go", r'''
printf 'go %s\n' "$*" >> "$CALLS"
case "$1" in
  version) echo "go version ${TEST_GO_VERSION:-go1.27.3} linux/arm64" ;;
  list)
    [ "$2" = -m ] && [ "$GOTOOLCHAIN" = local ] && [ "$GOWORK" = off ]
    [ "$GOPROXY" = off ] && [ "$GOSUMDB" = off ]
    [ "${TEST_GO_LIST_FAIL:-false}" != true ] || exit 43
    module=
    for argument in "$@"; do
      case "$argument" in -modfile=*) module="${argument#*=}";; esac
    done
    if [ -n "${TEST_GO_LIST_VERSION:-}" ]; then echo "$TEST_GO_LIST_VERSION"
    else awk '{ sub(/\r$/, "") } $1 == "go" { print $2 }' "$module"; fi ;;
  *) exit 99 ;;
esac
''')

    def test_go_version_and_module_gates(self) -> None:
        self.go_contract()
        self.mock_go_gate()
        self.run_script("verify-locked-go.sh", "version")
        self.env["TEST_GO_VERSION"] = "go1.27.4"
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.env.pop("TEST_GO_VERSION")
        self.calls.unlink()
        module = self.root / "go.mod"
        for version in ("1.13", "1.27", "1.27.3"):
            module.write_text(f"module example.com/fixture\n\ngo {version}\n")
            self.run_script("verify-locked-go.sh", "module", module)
        module.write_bytes(b"module example.com/fixture\r\n\r\ngo 1.27.3\r\n")
        self.run_script("verify-locked-go.sh", "module", module)
        self.assertEqual(self.calls.read_text().count("go list -m"), 4)
        self.calls.unlink()
        module.write_bytes(b"module example.com/fixture\r\n\r\ngo 1.27.4\r\n")
        result = self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.assertIn(f"::error title=locked-tools::{module} requires Go 1.27.4, newer than locked go1.27.3", result.stderr)
        for directive in ("go 1.27.4", "go 1.28", "go 2.0", "go 1.28rc1", "", "go 1.27\ngo 1.26"):
            module.write_text(f"module example.com/fixture\n\n{directive}\n")
            self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.assertFalse(self.calls.exists(), "invalid directives must fail before invoking Go")
        module.write_text("module fixture\ngo 1.27.3\n")
        self.env["TEST_GO_VERSION"] = "go1.27.4"
        self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.assertNotIn("go list", self.calls.read_text())
        self.env.pop("TEST_GO_VERSION")
        self.calls.unlink()
        self.env["GOTOOLCHAIN"] = "auto"
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.assertFalse(self.calls.exists())

    def test_go_gate_requires_complete_env_and_matching_lock_identity(self) -> None:
        self.go_contract()
        self.mock_go_gate()
        complete = dict(self.env)
        module = self.root / "go.mod"
        module.write_text("module fixture\ngo 1.27.3\n")
        for name in ("EXPECTED_GO_VERSION", "EXPECTED_LOCK_SHA256", "LOCK_PATH"):
            with self.subTest(missing=name):
                self.env = dict(complete)
                self.env.pop(name)
                self.run_script("verify-locked-go.sh", "version", ok=False)
                self.run_script("verify-locked-go.sh", "module", module, ok=False)
                self.assertFalse(self.calls.exists())
        self.env = dict(complete)
        for name in ("EXPECTED_GO_VERSION", "EXPECTED_LOCK_SHA256", "LOCK_PATH"):
            self.env.pop(name)
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.run_script("verify-locked-go.sh", "version", self.lock, ok=False)
        self.run_script("verify-locked-go.sh", "module", self.lock, module, ok=False)
        self.assertFalse(self.calls.exists())
        for name, value in (("EXPECTED_GO_VERSION", "1.27.3"),
                            ("EXPECTED_LOCK_SHA256", "0" * 64),
                            ("LOCK_PATH", "relative/tool-locks.txt"),
                            ("LOCK_PATH", str(self.root / "missing-lock"))):
            with self.subTest(name=name, value=value):
                self.env = dict(complete)
                self.env[name] = value
                self.run_script("verify-locked-go.sh", "version", ok=False)
                self.assertFalse(self.calls.exists())
        self.env = dict(complete)
        self.lock.write_text(self.lock.read_text() + "# drift\n")
        self.run_script("verify-locked-go.sh", "version", ok=False)
        self.assertFalse(self.calls.exists())

    def test_go_gate_env_does_not_parse_versions_from_lock(self) -> None:
        self.lock.write_text("opaque identity bytes: not a version manifest\n")
        self.go_contract()
        self.mock_go_gate()
        self.run_script("verify-locked-go.sh", "version")
        self.assertIn("go version", self.calls.read_text())

    def test_go_list_failure_or_disagreement_fails_closed(self) -> None:
        self.go_contract()
        self.mock_go_gate()
        module = self.root / "go.mod"
        module.write_text("module fixture\ngo 1.27.3\n")
        self.env["TEST_GO_LIST_FAIL"] = "true"
        self.run_script("verify-locked-go.sh", "module", module, ok=False)
        self.env.pop("TEST_GO_LIST_FAIL")
        self.env["TEST_GO_LIST_VERSION"] = "1.27.2"
        self.run_script("verify-locked-go.sh", "module", module, ok=False)

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
        self.go_contract()
        self.run_script("gon-source.sh", "ref")
        self.assertEqual(self.output_file.read_text(), f"ref={commit}\n")
        self.env.update(GOFLAGS="-mod=readonly", GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org")
        self.executable("go", 'echo "go $*" >> "$CALLS"\ncase "$1" in version) echo "go version go1.27.3 linux/arm64";; list) awk \'$1 == "go" { print $2 }\' go.mod;; build) [[ "$PWD" == */cmd/gon ]]; [ "$1 $2 $3" = "build -trimpath -o" ]; printf fixture > "$4";; *) exit 99;; esac\n')
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

    def test_minisign_zip_verified_before_extraction(self) -> None:
        archive = self.fixture_download("minisign", "darwin/arm64")
        self.env.update(TEST_OS="Darwin", TEST_ARCH="arm64")
        self.run_script("install-tool.sh", "minisign", self.root / "minisign")
        self.assertTrue(os.access(self.root / "minisign/minisign", os.X_OK))
        self.path_file.unlink()
        archive.write_bytes(archive.read_bytes() + b"tampered")
        result = self.run_script("install-tool.sh", "minisign", self.root / "bad-minisign", ok=False)
        self.assertIn("::error title=locked-tools::minisign sha256 mismatch", result.stderr)
        self.assertFalse(self.path_file.exists())
        self.assertFalse((self.root / "bad-minisign").exists())

    def test_custom_lock_overrides_action_defaults(self) -> None:
        self.fixture_download("node", "linux/amd64")
        expected_member = self.archive_member("node", "linux/amd64")
        custom = self.root / "custom locks.txt"
        shutil.copyfile(self.lock, custom)
        self.lock.write_text("# The canonical fixture lock is deliberately unusable.\n")
        self.env["TOOL_LOCK_FILE"] = str(custom)
        self.run_script("install-tool.sh", "node", self.root / "custom-install")
        self.assertTrue((self.root / "custom-install" / expected_member).is_file())
        self.run_script("gon-source.sh", "ref")
        self.assertIn("ref=", self.output_file.read_text())
        module = self.root / "go.mod"
        module.write_bytes(b"module fixture\r\ngo 1.27.3\r\n")
        self.go_contract(lock=custom)
        self.mock_go_gate()
        self.run_script("verify-locked-go.sh", "module", module)

    def phase_outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.output_file.read_text().splitlines())

    def test_action_go_outputs_feed_environment_gate(self) -> None:
        version = next(line.split()[1] for line in self.lock.read_text().splitlines()
                       if line.startswith("go "))
        binary = f'#!/bin/sh\n[ "$1" = version ] || exit 77\nprintf "go version {version} linux/amd64\\n"\n'.encode()
        self.fixture_download("go", "linux/amd64", data=binary)
        self.env.update(LOCKED_TOOLS="go", LOCKS_INPUT="", ACTION_DIR=str(self.action_dir))
        self.run_script("locked-tools.sh", "prepare")
        plan = self.phase_outputs()
        self.assertEqual(plan["go_version"], version)
        self.assertTrue(Path(plan["lock_path"]).is_absolute())
        self.assertTrue(Path(plan["lock_path"]).is_file())
        self.env.update(LOCKED_TOOLS=plan["tools"], LOCK_PATH=plan["lock_path"],
                        EXPECTED_GO_VERSION=plan["go_version"], EXPECTED_LOCK_SHA256=plan["lock_sha256"])
        self.run_script("locked-tools.sh", "install")
        self.run_script("locked-tools.sh", "outputs")
        outputs = self.phase_outputs()
        self.assertEqual(json.loads(outputs["versions"]), {"go": version})
        self.assertEqual(outputs["go"], version)
        self.assertEqual(outputs["lock_path"], plan["lock_path"])
        self.assertEqual(outputs["lock_sha256"], plan["lock_sha256"])
        self.env["PATH"] = f"{self.root / 'dest/go/bin'}:{self.env['PATH']}"
        self.env["EXPECTED_GO_VERSION"] = json.loads(outputs["versions"])["go"]
        self.run_script("verify-locked-go.sh", "version")

    def test_action_prepare_owns_single_go_version_sanity(self) -> None:
        self.fixture_download("go", "linux/amd64")
        self.env.update(LOCKED_TOOLS="go", LOCKS_INPUT="", ACTION_DIR=str(self.action_dir))
        self.lock.write_text(self.lock.read_text().replace("go go1.27.3 darwin/arm64", "go go1.27.4 darwin/arm64"))
        self.run_script("locked-tools.sh", "prepare", ok=False)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.path_file.exists())

    def test_action_node_phases_and_provenance_outputs(self) -> None:
        self.fixture_download("node", "linux/amd64")
        custom = self.root / "custom locks.txt"
        shutil.copyfile(self.lock, custom)
        self.env.update(LOCKED_TOOLS=" node ", LOCKS_INPUT=str(custom), ACTION_DIR=str(self.action_dir))
        self.run_script("locked-tools.sh", "prepare")
        plan = self.phase_outputs()
        expected_hash = hashlib.sha256(custom.read_bytes()).hexdigest()
        self.assertEqual(plan["lock_sha256"], expected_hash)
        self.assertEqual(plan["lock_path"], str(custom.resolve()))
        self.assertTrue(Path(plan["lock_path"]).is_file())
        self.env.update(LOCKED_TOOLS=plan["tools"], TOOL_LOCK_FILE=plan["lock_file"],
                        LOCKED_TOOLS_DEST=plan["dest"], EXPECTED_LOCK_SHA256=plan["lock_sha256"])
        self.run_script("locked-tools.sh", "install")
        self.run_script("locked-tools.sh", "outputs")
        outputs = self.phase_outputs()
        expected_version = next(line.split()[1] for line in custom.read_text().splitlines() if line.startswith("node "))
        self.assertEqual(outputs["node"], expected_version)
        self.assertEqual(json.loads(outputs["versions"]), {"node": expected_version})
        self.assertEqual(outputs["lock_sha256"], expected_hash)
        self.assertEqual(outputs["lock_path"], str(custom.resolve()))
        self.assertTrue(self.path_file.exists())
        custom.write_text(custom.read_text() + "# changed after preparation\n")
        self.calls.unlink()
        result = self.run_script("locked-tools.sh", "install", ok=False)
        self.assertIn("Lock file changed", result.stderr)
        self.assertFalse(self.calls.exists())
        self.run_script("locked-tools.sh", "outputs", ok=False)

    def test_windows_node_zip_and_action_dispatch(self) -> None:
        row = next(line.split() for line in self.lock.read_text().splitlines()
                   if line.startswith("node ") and line.split()[2] == "windows/amd64")
        version = row[1]
        self.assertEqual(row[4], f"https://nodejs.org/dist/{version}/node-{version}-win-x64.zip")
        archive = self.fixture_download("node", "windows/amd64")
        member = f"node-{version}-win-x64/node.exe"
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), [member])
            self.assertFalse(zipped.getinfo(member).is_dir())
        self.env.update(TEST_OS="MINGW64_NT", TEST_ARCH="x86_64", LOCKED_TOOLS="node",
                        LOCKS_INPUT="", ACTION_DIR=str(self.action_dir))
        self.executable("cygpath", '[[ "$1" = -u || "$1" = -w ]]; printf "%s\\n" "$2"\n')
        self.executable("pwsh", 'printf "pwsh %s\\n" "$*" >> "$CALLS"\n')
        self.run_script("locked-tools.sh", "prepare")
        plan = self.phase_outputs()
        self.assertEqual(plan["tools"], "node")
        self.env.update(TOOL_LOCK_FILE=plan["lock_file"], EXPECTED_LOCK_SHA256=plan["lock_sha256"])
        self.run_script("locked-tools.sh", "install")
        self.assertEqual(self.calls.read_text().splitlines(), [
            f"pwsh -NoProfile -File {self.action_dir}/install-tool.ps1 -Tool node -DestDir {self.root / 'dest/node'}"
        ])
        self.run_script("locked-tools.sh", "outputs")
        self.assertEqual(self.phase_outputs()["node"], version)
        self.assertEqual(json.loads(self.phase_outputs()["versions"]), {"node": version})

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "requires native Windows PowerShell")
    def test_windows_node_installer_fixture(self) -> None:
        original = (ACTION / "tool-locks.txt").read_text()
        version = next(line.split()[1] for line in original.splitlines()
                       if line.startswith("node ") and line.split()[2] == "windows/amd64")
        harness = """
$ErrorActionPreference = 'Stop'
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [int]$MaximumRetryCount)
    Add-Content -LiteralPath $env:CALLS -Value "download $Uri"
    Copy-Item -LiteralPath $env:ARCHIVE -Destination $OutFile
}
& $env:INSTALLER -Tool node -DestDir $env:INSTALL_DEST
"""
        for case in ("valid", "bad-hash", "bad-layout", "bad-url"):
            with self.subTest(case=case):
                self.lock.write_text(original)
                member = "unexpected-node/node.exe" if case == "bad-layout" else None
                archive = self.fixture_download("node", "windows/amd64", member=member)
                if case == "bad-hash":
                    archive.write_bytes(archive.read_bytes() + b"tampered")
                if case == "bad-url":
                    self.lock.write_text(self.lock.read_text().replace(
                        f"node-{version}-win-x64.zip", "unexpected-node.zip"))
                for path in (self.path_file, self.env_file, self.calls):
                    path.unlink(missing_ok=True)
                dest = self.root / case
                env = dict(self.env, TOOL_LOCK_FILE=str(self.lock),
                           INSTALLER=str(self.action_dir / "install-tool.ps1"), INSTALL_DEST=str(dest))
                result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", harness],
                                        env=env, text=True, capture_output=True)
                if case == "valid":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    package = dest / f"node-{version}-win-x64"
                    self.assertTrue((package / "node.exe").is_file())
                    # PowerShell may expand the Windows 8.3 temporary-directory alias.
                    published = self.path_file.read_text(encoding="utf-8-sig").splitlines()
                    self.assertTrue(any(Path(path).samefile(package) for path in published))
                    self.assertFalse(self.env_file.exists(), "Node must not publish GOROOT")
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(self.path_file.exists())
                    if case in ("bad-hash", "bad-url"):
                        self.assertFalse(dest.exists(), "must reject before extraction")
                    if case == "bad-url":
                        self.assertFalse(self.calls.exists(), "must reject before downloading")

    def test_action_empty_lock_input_resolves_in_action_context(self) -> None:
        self.fixture_download("node", "linux/amd64")
        self.env.update(LOCKED_TOOLS="node", LOCKS_INPUT="", ACTION_DIR=str(self.action_dir),
                        TOOL_LOCK_FILE="/caller-context/tool-locks.txt")
        self.run_script("locked-tools.sh", "prepare")
        plan = self.phase_outputs()
        self.assertEqual(plan["lock_file"], str(self.lock))
        self.assertEqual(plan["lock_path"], str(self.lock.resolve()))
        self.assertTrue(Path(plan["lock_path"]).is_absolute())
        self.assertTrue(Path(plan["lock_path"]).is_file())
        self.env.update(TOOL_LOCK_FILE=plan["lock_file"], EXPECTED_LOCK_SHA256=plan["lock_sha256"])
        self.run_script("locked-tools.sh", "install")
        self.run_script("locked-tools.sh", "outputs")
        self.assertEqual(self.phase_outputs()["lock_sha256"], hashlib.sha256(self.lock.read_bytes()).hexdigest())

    def test_action_explicit_missing_lock_fails_without_fallback(self) -> None:
        self.fixture_download("node", "linux/amd64")
        self.env.update(LOCKED_TOOLS="node", LOCKS_INPUT=str(self.root / "missing-lock.txt"),
                        ACTION_DIR=str(self.action_dir))
        result = self.run_script("locked-tools.sh", "prepare", ok=False)
        self.assertIn("::error title=locked-tools::Lock file missing or not readable:", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.path_file.exists())

    def test_action_gon_automatically_adds_go_and_rejects_bad_requests(self) -> None:
        self.fixture_download("node", "darwin/arm64")
        self.env.update(TEST_OS="Darwin", TEST_ARCH="arm64", LOCKED_TOOLS="gon,node")
        self.run_script("locked-tools.sh", "prepare")
        plan = self.phase_outputs()
        self.assertEqual(plan["tools"], "go,gon,node")
        self.assertRegex(plan["gon_ref"], r"^[0-9a-f]{40}$")
        self.assertTrue((self.workspace / plan["gon_path"]).is_dir())
        for request in ("unknown", "node,node", "node,", "node,,go", " ", "node\nsyft"):
            self.env["LOCKED_TOOLS"] = request
            self.run_script("locked-tools.sh", "prepare", ok=False)
        self.assertFalse(self.calls.exists(), "preparation must not download or run tools")

    def test_composite_metadata_and_single_canonical_lock(self) -> None:
        action = json.loads(subprocess.check_output(["yq", ".", str(ACTION / "action.yml")], text=True))
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertEqual(action["inputs"]["locks"]["default"], "")
        prepare = action["runs"]["steps"][0]
        self.assertEqual(prepare["env"]["ACTION_DIR"], "${{ github.action_path }}")
        self.assertEqual(prepare["env"]["LOCKS_INPUT"], "${{ inputs.locks }}")
        self.assertEqual(action["inputs"]["dest"]["default"], "${{ runner.temp }}/locked-tools")
        self.assertFalse((ROOT / "tools/tool-locks.txt").exists())
        for step in action["runs"]["steps"]:
            if "run" in step:
                self.assertNotIn("\n", step["run"])
                self.assertIn("$GITHUB_ACTION_PATH/locked-tools.sh", step["run"])
            else:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                self.assertEqual(step["with"]["ref"], "${{ steps.prepare.outputs.gon_ref }}")
                self.assertFalse(step["with"]["persist-credentials"])
        for tool in ("go", "node", "syft", "goreleaser", "goreleaser-pro", "gon", "minisign", "versions", "lock_sha256", "lock_path"):
            self.assertIn(tool, action["outputs"])
        self.assertEqual(action["outputs"]["lock_path"]["value"], "${{ steps.result.outputs.lock_path }}")
        for step in action["runs"]["steps"]:
            if step.get("name") in ("Install verified artifacts", "Build locked gon source",
                                    "Report installed tool versions and lock hash"):
                self.assertEqual(step["env"]["LOCK_PATH"], "${{ steps.prepare.outputs.lock_path }}")
                self.assertEqual(step["env"]["EXPECTED_GO_VERSION"], "${{ steps.prepare.outputs.go_version }}")

    def test_release_workflow_has_no_remote_self_actions_or_duplicate_bootstrap(self) -> None:
        text = (ROOT / ".github/workflows/release.yaml").read_text()
        self.assertNotIn("ConductorOne/github-workflows/.github/actions/", text)
        self.assertNotIn("verify-locked-go.sh version", text)
        self.assertNotIn("gon-source.sh", text)
        for legacy in ("_workflows/scripts/install-tool", "_workflows/scripts/gon-source", "_workflows/scripts/verify-locked-go"):
            self.assertNotIn(legacy, text)

    def test_workflow_security_wiring(self) -> None:
        workflow = json.loads(subprocess.check_output(["yq", ".", str(ROOT / ".github/workflows/release.yaml")], text=True))
        jobs = workflow["jobs"]
        self.assertEqual(jobs["determine-workflows-ref"]["outputs"]["ref"], "${{ job.workflow_sha }}")
        self.assertEqual(jobs["validate-inputs"]["permissions"], {"contents": "read"})
        expected_tools = {
            "validate-inputs": "",
            "determine-workflows-ref": "",
            "release-preflight": "go",
            "goreleaser-binaries": "go,gon,syft,goreleaser",
            "goreleaser-windows": "go,syft,goreleaser-pro",
            "goreleaser-docker": "go,goreleaser",
            "publish-release-manifest": "go",
            "record-registry-api": "go",
            "verify-release": "",
            "notify-release-failure": "",
        }
        self.assertEqual(set(jobs), set(expected_tools))
        checkout_count = guard_count = tool_count = gate_count = 0
        for job_name, job in jobs.items():
            with self.subTest(job=job_name):
                steps = job["steps"]
                guards = [index for index, step in enumerate(steps) if step.get("uses") == GUARD_ACTION]
                tools = [index for index, step in enumerate(steps) if step.get("uses") == TOOLS_ACTION]
                workflow_checkouts = [index for index, step in enumerate(steps)
                                      if step.get("with", {}).get("path") == "_workflows"]
                if job_name == "notify-release-failure":
                    self.assertEqual(workflow_checkouts, [])
                    self.assertEqual(guards, [], "guard failures must not suppress failure reporting")
                    self.assertEqual(tools, [])
                    self.assertEqual(job["if"], "failure()")
                    continue
                self.assertEqual(workflow_checkouts, [0])
                bootstrap = steps[0]
                self.assertRegex(bootstrap["uses"], r"^actions/checkout@[0-9a-f]{40}$")
                self.assertEqual(bootstrap["with"], {
                    "path": "_workflows",
                    "repository": "ConductorOne/github-workflows",
                    "ref": "${{ job.workflow_sha }}",
                    "persist-credentials": False,
                })
                self.assertNotIn("if", bootstrap)
                self.assertNotIn("continue-on-error", bootstrap)
                self.assertEqual(job["permissions"]["contents"], "read")
                checkout_count += 1
                self.assertEqual(guards, [1])
                self.assertEqual(steps[1].get("with", {}), {}, "use enforce/true/true/fail defaults")
                self.assertNotIn("if", steps[1])
                self.assertNotIn("continue-on-error", steps[1])
                guard_count += 1
                prefix_length = 2
                if expected_tools[job_name]:
                    self.assertEqual(tools, [2])
                    self.assertEqual(steps[2]["id"], "tools")
                    self.assertEqual(steps[2]["with"], {"tools": expected_tools[job_name]})
                    self.assertNotIn("continue-on-error", steps[2])
                    self.assertEqual(job["env"]["GOTOOLCHAIN"], "local")
                    if job_name == "release-preflight":
                        self.assertEqual(steps[2]["if"], "inputs.go_source_hygiene == true || inputs.go_vulnerability_scan == true")
                    else:
                        self.assertNotIn("if", steps[2])
                    prefix_length = 3
                    tool_count += 1
                else:
                    self.assertEqual(tools, [])
                caller_checkouts = [index for index, step in enumerate(steps)
                                    if step.get("with", {}).get("path") in ("_caller", "_connector")]
                checkouts = workflow_checkouts + caller_checkouts
                verifications = [index for index, step in enumerate(steps)
                                 if "verify-release-checkout.sh" in step.get("run", "")]
                credentials = [index for index, step in enumerate(steps)
                               if "secrets." in str(step) or "github.token" in str(step) or "core.getIDToken" in str(step)
                               or "configure-aws-credentials@" in step.get("uses", "")]
                self.assertTrue(all(index >= prefix_length for index in caller_checkouts))
                if verifications:
                    self.assertLess(max(checkouts), min(verifications))
                if credentials:
                    self.assertLess(prefix_length - 1, min(credentials))
                    if verifications:
                        self.assertLess(max(verifications), min(credentials))
                installs = [index for index, step in enumerate(steps)
                            if "cosign-installer@" in step.get("uses", "")
                            or "setup-qemu-action@" in step.get("uses", "")
                            or "setup-release-buildx.sh" in step.get("run", "")]
                if installs and credentials:
                    self.assertLess(max(installs), min(credentials))
                module_gates = [index for index, step in enumerate(steps)
                                if "/locked-tools/verify-locked-go.sh module " in step.get("run", "")]
                if expected_tools[job_name]:
                    self.assertEqual(len(module_gates), 1)
                    gate = steps[module_gates[0]]
                    self.assertEqual(gate["env"], GO_GATE_ENV)
                    self.assertLess(max(checkouts), module_gates[0])
                    if verifications:
                        self.assertLess(max(verifications), module_gates[0])
                    if credentials:
                        self.assertLess(module_gates[0], min(credentials))
                    modules = "_workflows/go.mod" if job_name in ("publish-release-manifest", "record-registry-api") else "_caller/go.mod _workflows/go.mod"
                    self.assertEqual(gate["run"],
                                     "bash _workflows/.github/actions/locked-tools/verify-locked-go.sh module " + modules)
                    if job_name == "release-preflight":
                        self.assertEqual(gate["if"], steps[2]["if"])
                    gate_count += 1
                for step in steps:
                    self.assertNotEqual(step.get("with", {}).get("repository"), "ConductorOne/gon")
                    if step.get("with", {}).get("path") in ("_caller", "_connector"):
                        self.assertEqual(step["with"]["ref"], "${{ github.sha }}")
                    if "verify-release-checkout.sh" in step.get("run", ""):
                        self.assertEqual(step["env"]["EXPECTED_COMMIT"], "${{ github.sha }}")
                    if "cosign-installer@" in step.get("uses", ""):
                        self.assertEqual(step["with"]["cosign-release"], "v2.5.2")
                        self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                ids = [step["id"] for step in steps if "id" in step]
                self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual((checkout_count, guard_count, tool_count, gate_count), (9, 9, 6, 6))
        for wrapper in ("install-tool.sh", "install-tool.ps1", "gon-source.sh", "verify-locked-go.sh"):
            self.assertFalse((ROOT / "scripts" / wrapper).exists())


if __name__ == "__main__":
    unittest.main()
