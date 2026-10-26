package main

import (
	"bytes"
	"crypto/x509"
	"encoding/json"
	"errors"
	"go/version"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

func fixtureDir(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs("../../.github/actions/ca-trust-guard/fixtures")
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func fixtureChains(t *testing.T) []chain {
	t.Helper()
	chains, err := loadChains(filepath.Join(fixtureDir(t), "chains.json"))
	if err != nil {
		t.Fatal(err)
	}
	return chains
}

func TestDiscoverStoresDarwin(t *testing.T) {
	prefix := filepath.Join(t.TempDir(), "opt", "homebrew")
	bundle := filepath.Join(prefix, "etc", "ca-certificates", "cert.pem")
	if err := os.MkdirAll(filepath.Dir(bundle), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(bundle, []byte("fixture bundle"), 0600); err != nil {
		t.Fatal(err)
	}
	system := store{"macos-etc-ssl-cert-pem", "/etc/ssl/cert.pem"}
	for _, test := range []struct {
		name   string
		output string
		err    error
		want   []store
	}{
		{"brew unavailable", "", errors.New("brew not found"), []store{system}},
		{"brew failure with output", prefix + "\n", errors.New("brew failed"), []store{system}},
		{"empty prefix", " \n", nil, []store{system}},
		{"bundle present", prefix + "\n", nil, []store{system, {"macos-brew", bundle}}},
		{"bundle absent", filepath.Join(t.TempDir(), "missing-formula") + "\n", nil, []store{system}},
	} {
		t.Run(test.name, func(t *testing.T) {
			command := func(name string, args ...string) ([]byte, error) {
				if name != "brew" || !reflect.DeepEqual(args, []string{"--prefix"}) {
					t.Fatalf("unexpected discovery command: %s %v", name, args)
				}
				return []byte(test.output), test.err
			}
			if got := discoverStores("darwin", command); !reflect.DeepEqual(got, test.want) {
				t.Fatalf("stores=%v, want %v", got, test.want)
			}
		})
	}
}

func TestClassifyStores(t *testing.T) {
	dir := fixtureDir(t)
	chains := fixtureChains(t)
	verifier, err := newVerifier(chains[0], dir, t.TempDir(), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	temp := t.TempDir()
	empty, malformed := filepath.Join(temp, "empty.pem"), filepath.Join(temp, "malformed.pem")
	if err := os.WriteFile(empty, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(malformed, []byte("-----BEGIN CERTIFICATE-----\nnot base64\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, c := range chains {
		for _, test := range []struct {
			name, path, want string
			epoch            int64
		}{
			{"vulnerable", filepath.Join(dir, "vulnerable-roots.pem"), trusts, c.spec.CaptureEpoch},
			{"patched Mozilla", filepath.Join(dir, "clean-ca-bundle.pem"), distrust, c.spec.CaptureEpoch},
			{"empty", empty, probeError, c.spec.CaptureEpoch},
			{"malformed", malformed, probeError, c.spec.CaptureEpoch},
			{"unreadable", filepath.Join(temp, "missing.pem"), probeError, c.spec.CaptureEpoch},
			{"wrong epoch", filepath.Join(dir, "vulnerable-roots.pem"), probeError, 1},
		} {
			t.Run(c.spec.ID+"/"+test.name, func(t *testing.T) {
				c.spec.CaptureEpoch = test.epoch
				verdict, detail := verifier.classifyStore(c, test.path)
				if verdict != test.want {
					t.Fatalf("got %s (%s), want %s", verdict, detail, test.want)
				}
			})
		}
	}
}

func TestOpenSSLClassifier(t *testing.T) {
	for _, test := range []struct {
		name         string
		code         int
		stderr, want string
	}{
		{"success", 0, "", trusts},
		{"missing issuer 20", 2, "error 20 at 2 depth lookup: unable to get local issuer certificate", distrust},
		{"missing issuer 2", 2, "error 2 at 1 depth lookup: unable to get issuer certificate", distrust},
		{"expired", 2, "error 10 at 0 depth lookup: certificate has expired", probeError},
		{"load failure", 1, "Error loading file bad.pem", probeError},
		{"unknown", 1, "unrecognized verifier failure", probeError},
	} {
		t.Run(test.name, func(t *testing.T) {
			verdict, _ := classifyOpenSSL(test.code, test.stderr)
			if verdict != test.want {
				t.Fatalf("got %s, want %s", verdict, test.want)
			}
		})
	}
}

// Living documentation: replacing OpenSSL with x509.Verify erases the
// vulnerable-store measurement even with explicit roots and capture time.
func TestGoVerifierRejectsIncidentPositiveControl(t *testing.T) {
	if !version.IsValid(runtime.Version()) || version.Compare(runtime.Version(), "go1.27.3") < 0 {
		t.Skip("incident SPKI blacklist regression applies to Go >=1.27.3")
	}
	roots, err := readCertificates(filepath.Join(fixtureDir(t), "vulnerable-roots.pem"))
	if err != nil {
		t.Fatal(err)
	}
	rootPool := x509.NewCertPool()
	for _, cert := range roots {
		rootPool.AddCert(cert)
	}
	for _, c := range fixtureChains(t) {
		intermediates := x509.NewCertPool()
		for _, cert := range c.intermediates {
			intermediates.AddCert(cert)
		}
		_, err := c.leaf.Verify(x509.VerifyOptions{
			Roots: rootPool, Intermediates: intermediates, CurrentTime: time.Unix(c.spec.CaptureEpoch, 0),
		})
		if err == nil {
			t.Fatalf("chain %s: Go unexpectedly accepted blacklisted incident SPKI", c.spec.ID)
		}
		t.Logf("chain %s: %v (OpenSSL positive control remains TRUSTS)", c.spec.ID, err)
	}
}

func TestLoadChains(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtureDir(t), "chains.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, old, new string
		invalid        bool
	}{
		{"valid", "", "", false},
		{"bad epoch", "1792540800", "0", true},
		{"bad identity", `"id": "1"`, `"id": "2"`, true},
		{"wrong fingerprint", "9DCB21B1", "00000000", true},
		{"traversal", "leaf-1.pem", "../leaf-1.pem", true},
		{"malformed", "{", "!", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			dir := t.TempDir()
			for _, name := range []string{"leaf-1.pem", "leaf-2.pem", "intermediates-1.pem", "intermediates-2.pem"} {
				contents, err := os.ReadFile(filepath.Join(fixtureDir(t), name))
				if err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(dir, name), contents, 0600); err != nil {
					t.Fatal(err)
				}
			}
			contents := string(data)
			if test.old != "" {
				contents = strings.Replace(contents, test.old, test.new, 1)
			}
			path := filepath.Join(dir, "chains.json")
			if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
				t.Fatal(err)
			}
			chains, err := loadChains(path)
			if (err != nil) != test.invalid {
				t.Fatalf("error=%v, want invalid=%v", err, test.invalid)
			}
			if err == nil && len(chains) != 2 {
				t.Fatalf("got %d chains", len(chains))
			}
		})
	}
	if err := verifyFixtures(fixtureDir(t)); err != nil {
		t.Fatal(err)
	}
}

func TestSecTrustOutcomes(t *testing.T) {
	for _, test := range []struct {
		output, want string
		success      bool
	}{
		{"", trusts, true},
		{"SecTrustEvaluate result: kSecTrustResultDeny", distrust, false},
		{"kSecTrustResultDeny", distrust, false},
		{"SecTrust result: explicit Deny (trust setting)", distrust, false},
		{"Cert Verify Result: CSSMERR_TP_NOT_TRUSTED", distrust, false},
		{"Cert Verify Result: CSSMERR_TP_VERIFY_ACTION_FAILED", rejectedUnknown, false},
		{"unrecognized failure", probeError, false},
	} {
		t.Run(test.output, func(t *testing.T) {
			var err error
			if !test.success {
				err = errors.New("security exit 1")
			}
			verdict, _ := classifySecTrust(test.output, err)
			if verdict != test.want {
				t.Fatalf("got %s, want %s", verdict, test.want)
			}
		})
	}
}

func writeTestRows(t *testing.T, dir, name string, rows []row) {
	t.Helper()
	file, err := os.Create(filepath.Join(dir, name+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	for _, r := range rows {
		if err := json.NewEncoder(file).Encode(r); err != nil {
			t.Fatal(err)
		}
	}
}

func TestJSONRowsEscapeDetails(t *testing.T) {
	dir := t.TempDir()
	var stdout bytes.Buffer
	writer, err := openRows(filepath.Join(dir, "rows.jsonl"), false, &stdout)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.file.Close()
	detail := "quoted \"certificate\" in C:\\path\nwith newline"
	if err := writer.emit(row{Phase: "os-deny", Store: "os-deny", Verdict: probeError, Detail: detail}); err != nil {
		t.Fatal(err)
	}
	rows, err := readRows(filepath.Join(dir, "rows.jsonl"))
	if err != nil || len(rows) != 1 || rows[0].Detail != detail {
		t.Fatalf("%+v %v", rows, err)
	}
}

func TestProbeIsolationThenPositiveControls(t *testing.T) {
	var stdout bytes.Buffer
	err := probe([]string{"--phase", "before", "--stores", "fixture=" + filepath.Join(fixtureDir(t), "vulnerable-roots.pem"),
		"--chains", filepath.Join(fixtureDir(t), "chains.json"), "--out", filepath.Join(t.TempDir(), "before.jsonl")}, &stdout)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	for i := range 3 {
		var r row
		if err := json.Unmarshal([]byte(strings.TrimPrefix(lines[i], "ca-trust-guard ")), &r); err != nil {
			t.Fatal(err)
		}
		wantPhase, wantVerdict := "positive-control", trusts
		if i == 0 {
			wantPhase, wantVerdict = "isolation-self-check", distrust
		}
		if r.Phase != wantPhase || r.Verdict != wantVerdict {
			t.Fatalf("isolation must precede both positive controls: %+v", r)
		}
		if r.OpenSSLPath == "" || r.OpenSSLVersion == "" {
			t.Fatalf("instrument identity missing: %+v", r)
		}
	}
}
