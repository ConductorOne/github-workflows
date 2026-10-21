#!/usr/bin/env python3
"""Offline SH10 contract tests; OS mutations are confined to temporary fixtures.

Run with a Python interpreter with PyYAML installed. No sudo, host trust-store
writes, GitHub calls, or network access are used by these tests.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/sh10-trust-probe/action.yml"
FIXTURES = ACTION.parent / "fixtures"
METADATA = yaml.safe_load(ACTION.read_text())
SCRIPT = METADATA["runs"]["steps"][0]["run"]
DENY = SCRIPT.split("<<'SH10_OS_DENY'\n", 1)[1].split("\nSH10_OS_DENY\n", 1)[0]
CERTS = re.findall(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n",
    (FIXTURES / "vulnerable-roots.pem").read_text(),
    re.S,
)
IDENTITIES = dict(re.findall(r'"([^"\n]+):([A-F0-9]{40})"', DENY))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.work), GITHUB_ENV=str(self.work / "env"))
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.env["GIT_CONFIG_GLOBAL"] = str(self.work / "gitconfig")
        self.env["SH10_DIR"] = str(FIXTURES)
        self.env["SH10_MODE"] = "enforce"
        self.env["SH10_APPLY_DISTRUST"] = "true"
        self.env["SH10_OS_DENY"] = "false"

    def executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_script(self, script, **env):
        return subprocess.run(
            ["bash", "-euo", "pipefail"], input=script, text=True,
            env=dict(self.env, **env), cwd=self.work, capture_output=True,
        )

    def rows(self, result):
        return [json.loads(line[len("sh10-probe "):]) for line in result.stdout.splitlines()
                if line.startswith("sh10-probe ")]

    def controlled_probe(self):
        # Replace only physical-store/provider boundaries. Positive controls,
        # fixture hashing, clean-bundle env/verification, ordering and gate are real.
        script = SCRIPT[:SCRIPT.index("run_checks() {")] + """run_checks() {
  verdict=$TEST_BEFORE
  if [ "$PHASE" = after-distrust ]; then verdict=$TEST_AFTER; fi
  emit fixture-system 1 "$verdict" 'test boundary'
  emit fixture-system 2 "$verdict" 'test boundary'
}
""" + SCRIPT[SCRIPT.index("apply_distrust() {"):]
        start = script.index("apply_os_deny() {")
        end = script.index("# Preserve the v3.11 BEFORE baseline", start)
        return script[:start] + """apply_os_deny() {
  echo os-deny-boundary
  return "${TEST_DENY_RC:-0}"
}
""" + script[end:]

    def test_composite_and_fixture_integrity(self):
        self.assertEqual(METADATA["runs"]["using"], "composite")
        self.assertEqual(METADATA["inputs"]["mode"]["default"], "enforce")
        for switch in ("apply-distrust", "os-deny"):
            self.assertEqual(METADATA["inputs"][switch]["default"], "true")
        for step in METADATA["runs"]["steps"]:
            self.assertEqual(step["shell"], "bash")
            self.assertEqual(step["env"]["SH10_DIR"], "${{ github.action_path }}/fixtures")
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
        subprocess.run(["bash", "-n"], input=DENY, text=True, check=True)
        for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
            digest, filename = line.split()
            self.assertEqual(hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest(), digest)
        self.assertEqual(len(IDENTITIES), 4)
        for pem in CERTS:
            result = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha1"],
                                    input=pem, text=True, capture_output=True, check=True)
            thumbprint = result.stdout.strip().split("=")[-1].replace(":", "")
            self.assertIn(thumbprint, IDENTITIES.values())

    def test_modes_and_before_baseline_gate(self):
        for mode in ("report", "enforce"):
            for apply in ("true", "false"):
                for before in ("TRUSTS", "VERIFIED-DISTRUST", "UNKNOWN"):
                    with self.subTest(mode=mode, apply=apply, before=before):
                        result = self.run_script(self.controlled_probe(), SH10_MODE=mode,
                                                 SH10_APPLY_DISTRUST=apply,
                                                 TEST_BEFORE=before, TEST_AFTER="VERIFIED-DISTRUST")
                        expected = int(mode == "enforce" and apply == "false" and before == "TRUSTS")
                        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                        rows = self.rows(result)
                        self.assertTrue(any(r.get("phase") == "after-distrust" for r in rows))
                        if before == "TRUSTS" and apply == "true":
                            self.assertEqual(rows[-1]["summary"], "PHYSICAL_STORE_TRUSTS_BUT_MITIGATED_BY_CLEAN_BUNDLE")

    def test_errors_always_fail_closed_and_after_is_not_new_gate(self):
        for mode in ("report", "enforce"):
            for before, after in (("ERROR", "VERIFIED-DISTRUST"), ("VERIFIED-DISTRUST", "ERROR")):
                result = self.run_script(self.controlled_probe(), SH10_MODE=mode,
                                         TEST_BEFORE=before, TEST_AFTER=after)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self.rows(result)[-1]["summary"], "ERROR")
        result = self.run_script(self.controlled_probe(), SH10_APPLY_DISTRUST="false",
                                 TEST_BEFORE="VERIFIED-DISTRUST", TEST_AFTER="TRUSTS")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_order_and_deny_failure_record(self):
        result = self.run_script(self.controlled_probe(), SH10_OS_DENY="true",
                                 TEST_BEFORE="TRUSTS", TEST_AFTER="VERIFIED-DISTRUST")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(result.stdout.index('"phase":"before"'), result.stdout.index("os-deny-boundary"))
        self.assertLess(result.stdout.index("os-deny-boundary"), result.stdout.index('"distrust":"clean-ca-bundle-env"'))
        self.assertLess(result.stdout.index('"distrust":"clean-ca-bundle-env"'), result.stdout.index('"phase":"after-distrust"'))
        result = self.run_script(self.controlled_probe(), SH10_OS_DENY="true", TEST_DENY_RC="1",
                                 TEST_BEFORE="TRUSTS", TEST_AFTER="VERIFIED-DISTRUST")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(any(r.get("store") == "os-deny" and r.get("verdict") == "ERROR"
                            for r in self.rows(result)))
        self.assertTrue(any(r.get("phase") == "after-distrust" for r in self.rows(result)))

    def test_clean_bundle_ineffective(self):
        openssl = shutil.which("openssl")
        self.executable("openssl", f"""#!/bin/bash
if [ "$1" = verify ] && [[ " $* " == *'clean-ca-bundle.pem'* ]]; then exit 0; fi
exec {openssl} "$@"
""")
        for mode in ("report", "enforce"):
            result = self.run_script(self.controlled_probe(), SH10_MODE=mode,
                                     TEST_BEFORE="TRUSTS", TEST_AFTER="TRUSTS")
            self.assertEqual(result.returncode, int(mode == "enforce"), result.stdout + result.stderr)

    def test_invalid_input_and_tampered_fixture_refused(self):
        result = self.run_script(SCRIPT, SH10_MODE="typo")
        self.assertNotEqual(result.returncode, 0)
        fixtures = self.work / "fixtures"
        shutil.copytree(FIXTURES, fixtures)
        with (fixtures / "leaf-1.pem").open("a") as handle:
            handle.write("tampered\n")
        result = self.run_script(SCRIPT, SH10_DIR=str(fixtures))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"phase":"before"', result.stdout)

    def prepare_deny(self, platform):
        self.executable("uname", f"#!/bin/sh\necho {platform}\n")
        self.executable("id", "#!/bin/sh\necho 0\n")
        source = self.work / "ca"
        source.mkdir()
        for index, pem in enumerate(CERTS):
            (source / f"root-{index}.crt").write_text(pem)
        conf = self.work / "ca-certificates.conf"
        conf.write_text("".join(f"root-{i}.crt\n" for i in range(len(CERTS))))
        bundle = self.work / "ca-bundle.pem"
        bundle.write_bytes((FIXTURES / "vulnerable-roots.pem").read_bytes())
        script = DENY.replace("/usr/share/ca-certificates", str(source))
        script = script.replace("/etc/ca-certificates.conf", str(conf))
        script = script.replace("/etc/ssl/certs/ca-certificates.crt", str(bundle))
        self.executable("update-ca-certificates", f"""#!/bin/sh
echo update >> '{self.work / 'mutations'}'
if [ "${{TEST_KEEP_BUNDLE:-0}}" != 1 ]; then cp '{FIXTURES / 'clean-ca-bundle.pem'}' '{bundle}'; fi
""")
        # A fake security CLI serves real, pinned certificates by CN.
        self.executable("security", f"""#!/usr/bin/python3
import os, pathlib, subprocess, sys
args = sys.argv[1:]
if args[0] == 'find-certificate':
    if os.environ.get('TEST_NOT_PRESENT') == '1': sys.exit(1)
    if 'SystemRootCertificates' in args[-1] and os.environ.get('TEST_FALLBACK') == '1': sys.exit(1)
    name = args[args.index('-c') + 1]
    for cert in pathlib.Path({str(source)!r}).glob('*.crt'):
        subject = subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-noout', '-subject', '-nameopt', 'RFC2253'], text=True).strip()
        if 'CN=' + name + ',' in subject or subject.endswith('CN=' + name):
            sys.stdout.write(cert.read_text()); sys.exit(0)
    sys.exit(1)
if args[0] == 'add-trusted-cert':
    with open({str(self.work / 'mutations')!r}, 'a') as handle: handle.write(' '.join(args) + '\\n')
    sys.exit(int(os.environ.get('TEST_MUTATION_RC', '0')))
sys.exit(2)
""")
        return script, conf

    def mismatch(self):
        openssl = shutil.which("openssl")
        self.executable("openssl", f"""#!/bin/bash
if [[ " $* " == *' -fingerprint '* ]] && [[ " $* " == *' -sha1 '* ]]; then
  echo 'sha1 Fingerprint=0000000000000000000000000000000000000000'
else
  exec {openssl} "$@"
fi
""")

    def test_linux_blacklist_and_compiled_bundle_verification(self):
        script, conf = self.prepare_deny("Linux")
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(line.startswith("!") for line in conf.read_text().splitlines()))
        self.assertEqual((self.work / "mutations").read_text(), "update\n")
        self.assertEqual(self.rows(result)[-1]["verdict"], "VERIFIED-DISTRUST")
        # Idempotent: already-denied config must not need another rebuild.
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "mutations").read_text(), "update\n")

    def test_linux_verification_refuses_root_left_in_bundle(self):
        script, _ = self.prepare_deny("Linux")
        result = self.run_script(script, TEST_KEEP_BUNDLE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still trusted after blacklist", result.stdout)

    def test_fingerprint_mismatch_before_any_mutation(self):
        for platform in ("Linux", "Darwin"):
            with self.subTest(platform=platform):
                # Each platform needs its own isolated sandbox.
                if (self.work / "ca").exists(): shutil.rmtree(self.work / "ca")
                script, conf = self.prepare_deny(platform)
                original = conf.read_bytes()
                self.mismatch()
                result = self.run_script(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("fingerprint mismatch", result.stdout)
                self.assertFalse((self.work / "mutations").exists())
                self.assertEqual(conf.read_bytes(), original)

    def test_macos_fallback_deny_and_not_present(self):
        script, _ = self.prepare_deny("Darwin")
        result = self.run_script(script, TEST_FALLBACK="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mutations = (self.work / "mutations").read_text().splitlines()
        self.assertEqual(len(mutations), 4)
        self.assertTrue(all('add-trusted-cert -d -r deny -k /Library/Keychains/System.keychain' in line
                            for line in mutations))
        result = self.run_script(script, TEST_NOT_PRESENT="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.rows(result)), 4)
        self.assertTrue(all(r["verdict"] == "NOT-PRESENT" for r in self.rows(result)))
        self.assertEqual((self.work / "mutations").read_text().splitlines(), mutations)

    def test_macos_mutation_failure_is_not_hidden(self):
        script, _ = self.prepare_deny("Darwin")
        result = self.run_script(script, TEST_MUTATION_RC="7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_macos_nonroot_uses_noninteractive_sudo(self):
        script, _ = self.prepare_deny("Darwin")
        self.executable("id", "#!/bin/sh\necho 1000\n")
        self.executable("sudo", f"""#!/bin/sh
printf '%s\\n' "$1" >> '{self.work / 'sudo-args'}'
[ "$1" = -n ] || exit 2
shift
exec "$@"
""")
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "sudo-args").read_text().splitlines(), ["-n"] * 4)

    def test_linux_missing_config_skips_without_mutation(self):
        script, conf = self.prepare_deny("Linux")
        conf.unlink()
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.rows(result)[-1]["verdict"], "SKIPPED-NO-CONFIG")
        self.assertFalse((self.work / "mutations").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
