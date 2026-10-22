#!/usr/bin/env python3
"""Offline policy-wiring controls through the actual release validation script."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/validate-release-artifacts.sh"
IDENTITY = "https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@refs/tags/v4"
ISSUER = "https://token.actions.githubusercontent.com"


class CertificateIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        manifest = {"semver": "v1.2.3", "assets": {"linux-amd64": {"href": "https://dist.conductorone.com/releases/ConductorOne/example/v1.2.3/example.tar.gz"}},
                    "images": {"ecrPublic": {"uri": "public.ecr.aws/example/image@sha256:" + "a" * 64}}}
        self.manifest = self.directory / "manifest.json"
        self.manifest.write_text(json.dumps(manifest))
        curl = self.bin / "curl"
        curl.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
url = next((arg for arg in args if arg.startswith("https://")), "")
if os.environ.get("FIXTURE_DETACHED_MANIFEST") == "true" and url.endswith("manifest.json.sigstore.json"):
    sys.exit(22)
if "-o" in args:
    output = pathlib.Path(args[args.index("-o") + 1])
    output.write_bytes(pathlib.Path(os.environ["FIXTURE_MANIFEST"]).read_bytes() if url.endswith("/manifest.json") else b"fixture")
''')
        curl.chmod(0o755)
        cosign = self.bin / "cosign"
        cosign.write_text('''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FIXTURE_CALLS"], "a") as output:
    output.write(json.dumps(args) + "\\n")
try:
    pattern = args[args.index("--certificate-identity-regexp") + 1]
    issuer = args[args.index("--certificate-oidc-issuer") + 1]
except (ValueError, IndexError):
    sys.exit("missing certificate policy")
# For this anchored literal/ref expression fullmatch models Go's end-of-text '$'.
if not re.fullmatch(pattern, os.environ["FIXTURE_IDENTITY"]) or issuer != os.environ["FIXTURE_ISSUER"]:
    sys.exit("certificate identity or issuer mismatch")
''')
        cosign.chmod(0o755)

    def validate(self, identity=IDENTITY, issuer=ISSUER, detached=False, script=SCRIPT):
        calls = self.directory / "calls.jsonl"
        calls.unlink(missing_ok=True)
        environment = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                           FIXTURE_MANIFEST=str(self.manifest), FIXTURE_CALLS=str(calls),
                           FIXTURE_IDENTITY=identity, FIXTURE_ISSUER=issuer,
                           FIXTURE_DETACHED_MANIFEST=str(detached).lower(),
                           COSIGN_VERIFY_ATTEMPTS="1", COSIGN_VERIFY_DELAY="0",
                           CERT_IDENTITY_REGEXP=".*") # Must not override configured policy.
        completed = subprocess.run(["bash", str(script), "ConductorOne/example", "v1.2.3"],
                                   env=environment, capture_output=True, text=True)
        return completed, [json.loads(line) for line in calls.read_text().splitlines()]

    def test_exact_protected_ref_on_every_verification_path(self):
        for detached in (False, True):
            with self.subTest(detached_manifest=detached):
                result, calls = self.validate(detached=detached)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(Counter(call[0] for call in calls),
                                 {"verify-blob": 2, "verify-blob-attestation": 2, "verify-attestation": 1})
                self.assertEqual(len({call[call.index("--certificate-identity-regexp") + 1] for call in calls}), 1)
                for call in calls:
                    self.assertEqual(call[call.index("--certificate-oidc-issuer") + 1], ISSUER)
                manifest_call = calls[-1]
                self.assertEqual("--bundle" in manifest_call, not detached)
                self.assertEqual("--signature" in manifest_call, detached)

    def test_other_refs_and_lookalike_identities_are_rejected(self):
        invalid = [
            IDENTITY.replace("refs/tags/v4", ref) for ref in
            ("refs/heads/main", "refs/heads/topic", "refs/tags/v3", "refs/tags/v40",
             "refs/tags/v4.0.0", "v4", "a" * 40)]
        invalid += ["prefix/" + IDENTITY, IDENTITY + "/suffix", IDENTITY + "\n",
                    IDENTITY.replace("github.com", "githubXcom"),
                    IDENTITY.replace("/.github/", "/Xgithub/"),
                    IDENTITY.replace("release.yaml", "releaseXyaml"),
                    IDENTITY.replace("github-workflows/", "github-axiomatic-workflows/")]
        for identity in invalid:
            with self.subTest(identity=identity):
                result, calls = self.validate(identity)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(calls), 5)
                self.assertIn("Failed: 5", result.stdout)

    def test_wrong_issuer_is_rejected(self):
        result, _ = self.validate(issuer=ISSUER + ".untrusted")
        self.assertNotEqual(result.returncode, 0)

    def test_widened_regexp_negative_control_would_accept_branch(self):
        source = SCRIPT.read_text()
        pattern = re.search(r"^CERT_IDENTITY_REGEXP='([^']+)'$", source, re.MULTILINE)[1]
        widened = pattern.replace("@refs/tags/v4$", "@.*$")
        self.assertNotEqual(pattern, widened)
        mutant = self.directory / "wide-validation.sh"
        mutant.write_text(source.replace(pattern, widened))
        branch = IDENTITY.replace("refs/tags/v4", "refs/heads/main")
        rejected, _ = self.validate(branch)
        accepted, _ = self.validate(branch, script=mutant)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)


if __name__ == "__main__":
    unittest.main()
