package main

import (
	"bytes"
	"crypto/sha1" // Thumbprint identity, not fixture integrity (which is SHA-256).
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type chainSpec struct {
	ID                  string   `json:"id"`
	Leaf                string   `json:"leaf"`
	Intermediates       string   `json:"intermediates"`
	LeafSHA256          string   `json:"leaf_sha256"`
	IntermediatesSHA256 []string `json:"intermediates_sha256"`
	CaptureEpoch        int64    `json:"capture_epoch"`
}

type chain struct {
	spec              chainSpec
	leaf              *x509.Certificate
	intermediates     []*x509.Certificate
	leafPath          string
	intermediatesPath string
}

func fixturePath(dir, name string) (string, error) {
	if name == "" || filepath.Base(name) != name || name == "." || name == ".." {
		return "", fmt.Errorf("invalid fixture filename %q", name)
	}
	return filepath.Join(dir, name), nil
}

func verifyFixtures(dir string) error {
	data, err := os.ReadFile(filepath.Join(dir, "SHA256SUMS"))
	if err != nil {
		return fmt.Errorf("read fixture checksums: %w", err)
	}
	seen := make(map[string]bool)
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			return fmt.Errorf("invalid checksum line %q", line)
		}
		path, err := fixturePath(dir, fields[1])
		if err != nil {
			return err
		}
		if seen[fields[1]] {
			return fmt.Errorf("duplicate checksum for %s", fields[1])
		}
		seen[fields[1]] = true
		contents, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read fixture %s: %w", fields[1], err)
		}
		digest := sha256.Sum256(contents)
		if !strings.EqualFold(hex.EncodeToString(digest[:]), fields[0]) {
			return fmt.Errorf("fixture checksum mismatch: %s", fields[1])
		}
	}
	for _, name := range []string{
		"chains.json", "incident-certs.json", "vulnerable-roots.pem", "clean-ca-bundle.pem",
		"leaf-1.pem", "leaf-2.pem", "intermediates-1.pem", "intermediates-2.pem",
		"expected-1.json", "expected-2.json",
	} {
		if !seen[name] {
			return fmt.Errorf("missing checksum for %s", name)
		}
	}
	_, err = loadIncidentCerts(filepath.Join(dir, "incident-certs.json"))
	return err
}

func parseCertificates(data []byte) ([]*x509.Certificate, error) {
	var certificates []*x509.Certificate
	for {
		block, rest := pem.Decode(data)
		if block == nil {
			if bytes.Contains(data, []byte("-----BEGIN")) {
				return nil, errors.New("malformed PEM block")
			}
			break
		}
		if block.Type != "CERTIFICATE" {
			return nil, fmt.Errorf("unexpected PEM block %q", block.Type)
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse certificate: %w", err)
		}
		certificates = append(certificates, cert)
		data = rest
	}
	if len(certificates) == 0 {
		return nil, errors.New("empty or malformed certificate store")
	}
	return certificates, nil
}

func readCertificates(path string) ([]*x509.Certificate, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseCertificates(data)
}

func matchesFingerprint(cert *x509.Certificate, want string) bool {
	digest := sha256.Sum256(cert.Raw)
	return strings.EqualFold(hex.EncodeToString(digest[:]), want)
}

func loadChains(path string) ([]chain, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read chains: %w", err)
	}
	var document struct {
		Chains []chainSpec `json:"chains"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return nil, fmt.Errorf("decode chains: %w", err)
	}
	if len(document.Chains) != 2 {
		return nil, errors.New("exactly two incident chains required")
	}
	seen := make(map[string]bool)
	var chains []chain
	for _, spec := range document.Chains {
		if (spec.ID != "1" && spec.ID != "2") || seen[spec.ID] || spec.CaptureEpoch <= 0 {
			return nil, fmt.Errorf("invalid chain identity or capture epoch: %q", spec.ID)
		}
		seen[spec.ID] = true
		leafPath, err := fixturePath(filepath.Dir(path), spec.Leaf)
		if err != nil {
			return nil, err
		}
		intermediatePath, err := fixturePath(filepath.Dir(path), spec.Intermediates)
		if err != nil {
			return nil, err
		}
		leaves, err := readCertificates(leafPath)
		if err != nil {
			return nil, err
		}
		intermediates, err := readCertificates(intermediatePath)
		if err != nil {
			return nil, err
		}
		if len(leaves) != 1 || !matchesFingerprint(leaves[0], spec.LeafSHA256) {
			return nil, fmt.Errorf("chain %s leaf fingerprint mismatch", spec.ID)
		}
		if len(intermediates) != len(spec.IntermediatesSHA256) {
			return nil, fmt.Errorf("chain %s intermediate count mismatch", spec.ID)
		}
		for i, cert := range intermediates {
			if !matchesFingerprint(cert, spec.IntermediatesSHA256[i]) {
				return nil, fmt.Errorf("chain %s intermediate fingerprint mismatch", spec.ID)
			}
		}
		chains = append(chains, chain{spec, leaves[0], intermediates, leafPath, intermediatePath})
	}
	return chains, nil
}

type incidentCert struct {
	Kind   string `json:"kind"`
	Name   string `json:"name"`
	SHA1   string `json:"sha1"`
	SHA256 string `json:"sha256"`
	Source string `json:"source"`
	PEM    string `json:"pem"`
}

func loadIncidentCerts(path string) ([]incidentCert, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var document struct {
		Certs []incidentCert `json:"certs"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return nil, err
	}
	kinds := make(map[string]int)
	seen := make(map[string]bool)
	for _, identity := range document.Certs {
		if identity.Name == "" || identity.Source == "" || seen[identity.SHA1] {
			return nil, errors.New("invalid/duplicate incident certificate identity")
		}
		seen[identity.SHA1] = true
		kinds[identity.Kind]++
		fixture, err := fixturePath(filepath.Dir(path), identity.PEM)
		if err != nil {
			return nil, err
		}
		certificates, err := readCertificates(fixture)
		if err != nil {
			return nil, err
		}
		found := false
		for _, cert := range certificates {
			digest := sha1.Sum(cert.Raw)
			if !strings.EqualFold(hex.EncodeToString(digest[:]), identity.SHA1) {
				continue
			}
			if cert.Subject.CommonName != identity.Name || !matchesFingerprint(cert, identity.SHA256) {
				return nil, fmt.Errorf("incident certificate fingerprint/name mismatch: %s", identity.Name)
			}
			found = true
		}
		if !found {
			return nil, fmt.Errorf("incident certificate missing from pinned fixture: %s", identity.Name)
		}
	}
	if len(document.Certs) != 6 || kinds["root"] != 4 || kinds["intermediate"] != 2 {
		return nil, errors.New("expected exactly four roots and two compromised intermediates")
	}
	return document.Certs, nil
}
