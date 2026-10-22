package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// Go's incident SPKI blacklist applies even to explicit roots in x509.Verify.
// Keep OpenSSL as the independent measurement instrument: otherwise the pinned
// positive control fails and vulnerable file stores cannot be distinguished.
type opensslVerifier struct {
	path    string
	version string
	flags   []string
}

func opensslCandidates(override string) []string {
	var candidates []string
	if override != "" {
		candidates = append(candidates, override)
	}
	return append(candidates,
		"/opt/homebrew/opt/openssl@3/bin/openssl",
		"/usr/local/opt/openssl@3/bin/openssl",
		"/opt/homebrew/bin/openssl",
		"/usr/local/bin/openssl",
		"openssl", // Resolve the PATH candidate only after the explicit locations.
		"/usr/bin/openssl",
	)
}

var helpOption = regexp.MustCompile(`-[A-Za-z][A-Za-z0-9-]*`)

func verificationFlags(help, emptyCAPath string) ([]string, error) {
	advertised := make(map[string]bool)
	for _, option := range helpOption.FindAllString(help, -1) {
		advertised[option] = true
	}
	for _, required := range []string{"-CAfile", "-untrusted", "-attime"} {
		if !advertised[required] {
			return nil, fmt.Errorf("verify help does not advertise required %s", required)
		}
	}
	var flags []string
	if advertised["-no-CApath"] {
		flags = append(flags, "-no-CApath")
	} else {
		flags = append(flags, "-CApath", emptyCAPath)
	}
	if advertised["-no-CAfile"] {
		flags = append(flags, "-no-CAfile")
	}
	if advertised["-no-CAstore"] {
		flags = append(flags, "-no-CAstore")
	}
	return flags, nil
}

func inspectVerifier(candidate, emptyCAPath string) (opensslVerifier, error) {
	verifier := opensslVerifier{path: candidate}
	path, err := exec.LookPath(nativePath(candidate))
	if err != nil {
		return verifier, fmt.Errorf("resolve binary: %w", err)
	}
	verifier.path, err = filepath.Abs(path)
	if err != nil {
		return verifier, err
	}
	output, err := exec.Command(verifier.path, "version").Output()
	verifier.version = strings.TrimSpace(string(output))
	if err != nil {
		return verifier, fmt.Errorf("read version: %w", err)
	}
	if verifier.version == "" {
		return verifier, errors.New("empty openssl version")
	}
	help, err := exec.Command(verifier.path, "verify", "-help").CombinedOutput()
	// Some variants print complete usage and exit nonzero for -help. The
	// advertised options and the subsequent isolation check are authoritative.
	var exitError *exec.ExitError
	if err != nil && !errors.As(err, &exitError) {
		return verifier, fmt.Errorf("read verify help: %w", err)
	}
	verifier.flags, err = verificationFlags(string(help), emptyCAPath)
	return verifier, err
}

func newVerifier(c chain, fixtures, work string, diagnostics io.Writer) (opensslVerifier, error) {
	return selectVerifier(opensslCandidates(os.Getenv("CA_TRUST_GUARD_OPENSSL")), c, fixtures, work, diagnostics)
}

func selectVerifier(candidates []string, c chain, fixtures, work string, diagnostics io.Writer) (opensslVerifier, error) {
	emptyCAPath := filepath.Join(work, "empty-ca-path")
	if err := os.Mkdir(emptyCAPath, 0700); err != nil {
		return opensslVerifier{}, err
	}
	unrelated, err := os.ReadFile(filepath.Join(fixtures, "isolation-ca.pem"))
	if err != nil {
		return opensslVerifier{}, err
	}
	isolationCA := filepath.Join(work, "isolation-ca.pem")
	if err := os.WriteFile(isolationCA, unrelated, 0600); err != nil {
		return opensslVerifier{}, err
	}
	var rejected []string
	for _, candidate := range candidates {
		verifier, candidateError := inspectVerifier(candidate, emptyCAPath)
		if candidateError == nil {
			verdict, detail := verifier.classifyStore(c, isolationCA)
			if verdict != distrust {
				candidateError = fmt.Errorf("isolation self-check ERROR: got %s (%s); implicit stores may be leaking", verdict, detail)
			}
		}
		if candidateError == nil {
			fmt.Fprintf(diagnostics, "ca-trust-guard selected OpenSSL path=%q version=%q flags=%q; isolation self-check VERIFIED-DISTRUST\n",
				verifier.path, verifier.version, verifier.flags)
			return verifier, nil
		}
		// Rejection is selection evidence, not a measurement ERROR row: a
		// healthy fallback must not poison the unchanged summarize contract.
		message := fmt.Sprintf("path=%q version=%q rejected: %v", verifier.path, verifier.version, candidateError)
		fmt.Fprintln(diagnostics, "ca-trust-guard OpenSSL selection: "+message)
		rejected = append(rejected, message)
	}
	return opensslVerifier{}, fmt.Errorf("no isolated OpenSSL candidate available: %s", strings.Join(rejected, "; "))
}

var missingIssuer = regexp.MustCompile(`error (20|2) at [0-9]+ depth lookup: unable to get (local )?issuer certificate`)

func classifyOpenSSL(exitCode int, stderr string) (string, string) {
	if exitCode == 0 {
		return trusts, ""
	}
	if detail := missingIssuer.FindString(stderr); detail != "" {
		return distrust, detail
	}
	detail := strings.SplitN(stderr, "\n", 2)[0]
	if len(detail) > 120 {
		detail = detail[:120]
	}
	return probeError, detail
}

func (v opensslVerifier) classifyStore(c chain, path string) (string, string) {
	args := append([]string{"verify"}, v.flags...)
	args = append(args, "-CAfile", path, "-untrusted", c.intermediatesPath,
		"-attime", strconv.FormatInt(c.spec.CaptureEpoch, 10), c.leafPath)
	command := exec.Command(v.path, args...)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	err := command.Run()
	if err == nil {
		return classifyOpenSSL(0, stderr.String())
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return classifyOpenSSL(exitError.ExitCode(), stderr.String())
	}
	return probeError, fmt.Sprintf("measurement openssl failed: %v", err)
}

// Later action phases run as separate processes. Reuse the selected instrument
// identity from their sibling probe evidence, without selecting another binary.
func evidenceInstrument(dir string) opensslVerifier {
	for _, phase := range []string{"after", "before"} {
		rows, err := readRows(filepath.Join(dir, phase+".jsonl"))
		if err != nil {
			continue
		}
		for _, r := range rows {
			if r.OpenSSLPath != "" && r.OpenSSLVersion != "" {
				return opensslVerifier{path: r.OpenSSLPath, version: r.OpenSSLVersion}
			}
		}
	}
	return opensslVerifier{}
}
