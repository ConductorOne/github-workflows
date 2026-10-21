#!/usr/bin/env python3
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECKER = Path(__file__).resolve().with_name("check-image-locks.sh")
PIN = "example/base:stable@sha256:" + "a" * 64


class ImageLockCheckTest(unittest.TestCase):
    def check_files(self, files, expected_status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = subprocess.run(
                ["bash", str(CHECKER)], cwd=root, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, expected_status, result.stderr)
            return result.stderr

    def test_pinned_dot_template_and_platform(self):
        self.check_files({"templates/.Dockerfile.tmpl": f"FROM --platform=$TARGETPLATFORM {PIN} AS base\n"}, 0)

    def test_unpinned_template(self):
        diagnostic = self.check_files({"templates/.Dockerfile.tmpl": "FROM example/base:latest\n"}, 1)
        self.assertIn("templates/.Dockerfile.tmpl:1:", diagnostic)

    def test_lowercase_dockerfile_instruction(self):
        self.check_files({"Dockerfile": "from example/base:latest\n"}, 1)

    def test_malformed_digest(self):
        self.check_files({"Dockerfile": "FROM example/base@sha256:abc\n"}, 1)

    def test_pinned_helper_and_yaml_image(self):
        self.check_files({"workflow.yaml": f"driver-opts: image={PIN}\nimage: '{PIN}'\n"}, 0)

    def test_unpinned_helper(self):
        self.check_files({"workflow.yaml": "driver-opts: image=moby/buildkit:latest\n"}, 1)

    def test_unpinned_yaml_image(self):
        self.check_files({"workflow.yaml": "  image: tonistiigi/binfmt:latest\n"}, 1)

    def test_second_helper_on_same_line_cannot_hide(self):
        self.check_files({"script.sh": f"docker buildx create --driver-opt image={PIN} --driver-opt image=unlocked\n"}, 1)

    def test_quoted_helper_cannot_hide(self):
        self.check_files({"script.sh": "docker buildx create --driver-opt 'image=unlocked'\n"}, 1)

    def test_reports_failures_across_files(self):
        diagnostic = self.check_files({"Dockerfile": "FROM unlocked\n", "nested/Dockerfile": "FROM other\n"}, 1)
        self.assertIn("Dockerfile:1:", diagnostic)
        self.assertIn("nested/Dockerfile:1:", diagnostic)

    def test_checks_documented_examples(self):
        self.check_files({"README.md": "```dockerfile\nFROM unlocked\n```\n"}, 1)

    def test_ignores_python_imports_and_non_declarations(self):
        self.check_files({"script.py": "from pathlib import Path\n", "notes.txt": "Attesting image: output\n"}, 0)

    def test_empty_repository(self):
        self.check_files({}, 0)


class UpdateConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = CHECKER.parent.parent
        cls.config = json.loads((cls.root / "renovate.json").read_text())

    def extract(self, manager, text):
        # Only translate named-group syntax; these patterns use no JS-only
        # operators or lookarounds/backreferences unsupported by Renovate's RE2.
        pattern = re.sub(r"\(\?<(\w+)>", r"(?P<\1>", manager["matchStrings"][0])
        return [match.groupdict() for match in re.finditer(pattern, text)]

    def test_every_image_lock_is_discovered(self):
        manager = next(m for m in self.config["customManagers"] if "image-locks" in str(m["managerFilePatterns"]))
        content = (self.root / "tools/image-locks.txt").read_text()
        locks = [line.split() for line in content.splitlines() if line and not line.startswith("#")]
        matches = self.extract(manager, content)
        self.assertEqual(len(matches), len(locks))
        for match, lock in zip(matches, locks):
            self.assertEqual(match["depName"] + ":" + match["currentValue"], lock[0])
            self.assertEqual(match["currentDigest"], lock[1])

    def test_helper_input_and_driver_assignment_are_discovered(self):
        manager = next(m for m in self.config["customManagers"] if "release" in str(m["managerFilePatterns"]))
        matches = self.extract(manager, f"image: '{PIN}'\n--driver-opt image={PIN}\n")
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(m["currentDigest"] == "sha256:" + "a" * 64 for m in matches))

    def test_binary_tool_proposals_preserve_go_prefix(self):
        for manager in self.config["customManagers"]:
            dep = manager.get("depNameTemplate")
            if not dep or dep == "ConductorOne/gon":
                continue
            tool = {"anchore/syft": "syft", "go": "go"}.get(dep, dep.split("/")[-1])
            version = "go9.8.7" if tool == "go" else "v9.8.7"
            content = f"{tool} {version} linux/amd64 {'a' * 64} https://example.invalid/artifact\n"
            matches = self.extract(manager, content)
            self.assertEqual(len(matches), 1, dep)
            expected = "9.8.7" if tool == "go" else version
            self.assertEqual(matches[0]["currentValue"], expected, dep)
            if tool == "go":
                self.assertEqual(manager["versioningTemplate"], "semver")

    def test_gon_tracks_tag_and_source_commit_together(self):
        manager = next(m for m in self.config["customManagers"] if m.get("depNameTemplate") == "ConductorOne/gon")
        matches = self.extract(manager, f"# gon-release v9.8.7\ngon {'b' * 40} darwin/* source\n")
        self.assertEqual(matches, [{"currentValue": "v9.8.7", "currentDigest": "b" * 40}])


if __name__ == "__main__":
    unittest.main()
