package main

import (
	"bytes"
	"errors"
	"fmt"
	"os/exec"
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
}

func newVerifier() (opensslVerifier, error) {
	path, err := exec.LookPath("openssl")
	if err != nil {
		return opensslVerifier{}, fmt.Errorf("resolve measurement openssl: %w", err)
	}
	output, err := exec.Command(path, "version").Output()
	if err != nil {
		return opensslVerifier{}, fmt.Errorf("measurement openssl version: %w", err)
	}
	version := strings.TrimSpace(string(output))
	if version == "" {
		return opensslVerifier{}, errors.New("measurement openssl returned empty version")
	}
	return opensslVerifier{path: path, version: version}, nil
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
	command := exec.Command(v.path, "verify", "-no-CApath", "-no-CAfile", "-no-CAstore",
		"-CAfile", path, "-untrusted", c.intermediatesPath, "-attime", strconv.FormatInt(c.spec.CaptureEpoch, 10), c.leafPath)
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
