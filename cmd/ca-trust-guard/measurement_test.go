package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestOpenSSLCandidateOrder(t *testing.T) {
	want := []string{"/reviewed/openssl", "/opt/homebrew/opt/openssl@3/bin/openssl",
		"/usr/local/opt/openssl@3/bin/openssl", "/opt/homebrew/bin/openssl", "/usr/local/bin/openssl",
		"openssl", "/usr/bin/openssl"}
	if got := opensslCandidates(want[0]); !reflect.DeepEqual(got, want) {
		t.Fatalf("candidates=%v, want %v", got, want)
	}
	if got := opensslCandidates(""); !reflect.DeepEqual(got, want[1:]) {
		t.Fatalf("unexpected candidates without override: %v", got)
	}
}

func opensslShim(t *testing.T, version, help string, helpExit int, leaks bool, forbidden string) (string, string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("executable POSIX OpenSSL shims require a Unix test host")
	}
	dir := t.TempDir()
	path, calls := filepath.Join(dir, "openssl"), filepath.Join(dir, "calls")
	leakValue := "false"
	if leaks {
		leakValue = "true"
	}
	// Shell arguments below contain only controlled test paths and text.
	script := "#!/bin/sh\n" +
		"version=" + shellQuote(version) + "\nhelp=" + shellQuote(help) + "\ncalls=" + shellQuote(calls) + "\n" +
		"case \"$1\" in\nversion) printf '%s\\n' \"$version\"; exit 0;;\nverify) ;;\n*) exit 64;;\nesac\n" +
		"if [ \"${2:-}\" = -help ]; then printf '%s\\n' \"$help\" >&2; exit " + strconv.Itoa(helpExit) + "; fi\n" +
		"printf '%s\\n' \"$@\" >> \"$calls\"\n" +
		"cafile=\nwhile [ \"$#\" -gt 0 ]; do\n" +
		"  if [ -n " + shellQuote(forbidden) + " ] && [ \"$1\" = " + shellQuote(forbidden) + " ]; then echo 'unsupported flag' >&2; exit 64; fi\n" +
		"  case \"$1\" in -CAfile) cafile=$2; shift;; esac\n  shift\ndone\n" +
		"case \"$cafile\" in *isolation-ca.pem)\n" +
		"  if " + leakValue + "; then exit 0; fi\n" +
		"  echo 'error 20 at 2 depth lookup: unable to get local issuer certificate' >&2; exit 2;;\n" +
		"esac\nexit 0\n"
	if err := os.WriteFile(path, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	return path, calls
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func TestOpenSSLVariantsUseOnlyAdvertisedFlags(t *testing.T) {
	const required = "-CAfile file -untrusted file -attime epoch"
	for _, test := range []struct {
		name, version, help, forbidden string
		helpExit                       int
		want                           []string
	}{
		{"3.x", "OpenSSL 3.0.22", required + " -no-CAfile -no-CApath -no-CAstore", "", 0,
			[]string{"-no-CApath", "-no-CAfile", "-no-CAstore"}},
		{"1.1.1", "OpenSSL 1.1.1w", required + " -no-CAfile -no-CApath", "-no-CAstore", 0,
			[]string{"-no-CApath", "-no-CAfile"}},
		{"LibreSSL", "LibreSSL 3.3.6", "usage: verify [-CAfile file] [-CApath dir] [-untrusted file] [-attime epoch]",
			"-no-CAstore", 1, []string{"-CApath", "EMPTY"}},
		{"no-CAfile-unavailable", "OpenSSL test", required + " -no-CApath", "-no-CAfile", 0,
			[]string{"-no-CApath"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			path, calls := opensslShim(t, test.version, test.help, test.helpExit, false, test.forbidden)
			work := t.TempDir()
			verifier, err := selectVerifier([]string{path}, fixtureChains(t)[0], fixtureDir(t), work, io.Discard)
			if err != nil {
				t.Fatal(err)
			}
			want := append([]string(nil), test.want...)
			for i, value := range want {
				if value == "EMPTY" {
					want[i] = filepath.Join(work, "empty-ca-path")
				}
			}
			if !reflect.DeepEqual(verifier.flags, want) {
				t.Fatalf("flags=%v, want %v", verifier.flags, want)
			}
			if verifier.path != path || verifier.version != test.version {
				t.Fatalf("identity=%+v", verifier)
			}
			data, err := os.ReadFile(calls)
			if err != nil {
				t.Fatal(err)
			}
			args := strings.Split(strings.TrimSpace(string(data)), "\n")
			wantArgs := append([]string{"verify"}, want...)
			c := fixtureChains(t)[0]
			wantArgs = append(wantArgs, "-CAfile", filepath.Join(work, "isolation-ca.pem"), "-untrusted",
				c.intermediatesPath, "-attime", strconv.FormatInt(c.spec.CaptureEpoch, 10), c.leafPath)
			if !reflect.DeepEqual(args, wantArgs) {
				t.Fatalf("isolation argv=%v, want %v", args, wantArgs)
			}
			entries, err := os.ReadDir(filepath.Join(work, "empty-ca-path"))
			if err != nil || len(entries) != 0 {
				t.Fatalf("CApath is not empty: %v %v", entries, err)
			}
		})
	}
}

func TestOpenSSLCapabilityProbeRequiresMeasurementOptions(t *testing.T) {
	for _, missing := range []string{"-CAfile", "-untrusted", "-attime"} {
		t.Run(missing, func(t *testing.T) {
			var options []string
			for _, option := range []string{"-CAfile", "-untrusted", "-attime", "-no-CAfile", "-no-CApath"} {
				if option != missing {
					options = append(options, option)
				}
			}
			help := strings.Join(options, " ")
			path, calls := opensslShim(t, "OpenSSL incomplete", help, 0, false, "")
			_, err := selectVerifier([]string{path}, fixtureChains(t)[0], fixtureDir(t), t.TempDir(), io.Discard)
			if err == nil || !strings.Contains(err.Error(), "required "+missing) {
				t.Fatalf("expected capability rejection: %v", err)
			}
			if _, err := os.Stat(calls); !os.IsNotExist(err) {
				t.Fatal("measurement ran before capability validation")
			}
		})
	}
}

func TestLeakingOpenSSLIsRejectedAndHealthyFallbackWins(t *testing.T) {
	const help = "-CAfile -untrusted -attime -no-CAfile -no-CApath -no-CAstore"
	leaking, _ := opensslShim(t, "OpenSSL leaking ignores -no-CAstore", help, 0, true, "")
	healthy, _ := opensslShim(t, "OpenSSL isolated", help, 0, false, "")
	var diagnostics bytes.Buffer
	verifier, err := selectVerifier([]string{leaking, healthy}, fixtureChains(t)[0], fixtureDir(t), t.TempDir(), &diagnostics)
	if err != nil {
		t.Fatal(err)
	}
	if verifier.path != healthy {
		t.Fatalf("leaking candidate selected: %+v", verifier)
	}
	if !strings.Contains(diagnostics.String(), leaking) || !strings.Contains(diagnostics.String(), "isolation self-check ERROR: got TRUSTS") {
		t.Fatalf("missing rejection evidence: %s", &diagnostics)
	}
	_, err = selectVerifier([]string{leaking}, fixtureChains(t)[0], fixtureDir(t), t.TempDir(), io.Discard)
	if err == nil || !strings.Contains(err.Error(), "no isolated OpenSSL candidate") {
		t.Fatalf("leaking-only candidate did not fail closed: %v", err)
	}
}

func TestOverrideLeakDoesNotPoisonHealthyProbeEvidence(t *testing.T) {
	leaking, _ := opensslShim(t, "OpenSSL leaking", "-CAfile -untrusted -attime -no-CAstore", 0, true, "")
	t.Setenv("CA_TRUST_GUARD_OPENSSL", leaking)
	out := filepath.Join(t.TempDir(), "before.jsonl")
	var stdout bytes.Buffer
	if err := probe([]string{"--phase", "before", "--stores", "fixture=" + filepath.Join(fixtureDir(t), "vulnerable-roots.pem"),
		"--chains", filepath.Join(fixtureDir(t), "chains.json"), "--out", out}, &stdout); err != nil {
		t.Fatal(err)
	}
	rows, err := readRows(out)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		if r.Verdict == probeError || r.OpenSSLPath == leaking {
			t.Fatalf("rejected candidate poisoned probe evidence: %+v", r)
		}
	}
}

func TestIsolationCertificateIsSingleUnrelatedSelfSignedCA(t *testing.T) {
	certs, err := readCertificates(filepath.Join(fixtureDir(t), "isolation-ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	if len(certs) != 1 || !certs[0].IsCA || certs[0].CheckSignatureFrom(certs[0]) != nil {
		t.Fatal("isolation fixture must contain one self-signed CA")
	}
	incident, err := readCertificates(filepath.Join(fixtureDir(t), "vulnerable-roots.pem"))
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range fixtureChains(t) {
		incident = append(incident, c.intermediates...)
		incident = append(incident, c.leaf)
	}
	for _, cert := range incident {
		if bytes.Equal(cert.RawSubjectPublicKeyInfo, certs[0].RawSubjectPublicKeyInfo) || cert.CheckSignatureFrom(certs[0]) == nil {
			t.Fatal("isolation CA is not unrelated to incident certificates")
		}
	}
}

func TestRecordAndSummaryReuseInstrumentIdentity(t *testing.T) {
	dir := t.TempDir()
	before := append(evidencePair("positive-control", "vulnerable-roots", trusts), evidencePair("before", "system", distrust)...)
	for i := range before {
		before[i].OpenSSLPath, before[i].OpenSSLVersion = "/chosen/before", "OpenSSL before"
	}
	writeTestRows(t, dir, "before", before)
	if err := record([]string{"--out", filepath.Join(dir, "os-deny.jsonl"), "--verdict", "DENIED"}, io.Discard); err != nil {
		t.Fatal(err)
	}
	rows, err := readRows(filepath.Join(dir, "os-deny.jsonl"))
	if err != nil || rows[0].OpenSSLPath != "/chosen/before" || rows[0].OpenSSLVersion != "OpenSSL before" {
		t.Fatalf("record lost identity: %+v %v", rows, err)
	}
	after := append(evidencePair("positive-control", "vulnerable-roots", trusts), evidencePair("after", "system", distrust)...)
	for i := range after {
		after[i].OpenSSLPath, after[i].OpenSSLVersion = "/chosen/after", "OpenSSL after"
	}
	writeTestRows(t, dir, "after", after)
	if err := summarize([]string{"--in", dir, "--mode", "report", "--apply-distrust=false", "--os-deny=false"}, io.Discard); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "summary.jsonl"))
	if err != nil || !bytes.Contains(data, []byte(`"openssl_path":"/chosen/after"`)) || !bytes.Contains(data, []byte(`"openssl_version":"OpenSSL after"`)) {
		t.Fatalf("summary lost selected instrument identity: %s %v", data, err)
	}
}
