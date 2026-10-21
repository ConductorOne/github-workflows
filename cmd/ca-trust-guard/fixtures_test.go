package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestIncidentCertificateManifest(t *testing.T) {
	path := filepath.Join(fixtureDir(t), "incident-certs.json")
	certs, err := loadIncidentCerts(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(certs) != 6 {
		t.Fatalf("got %d identities", len(certs))
	}
	kinds := map[string]int{}
	intermediateNames := map[string]int{}
	for _, cert := range certs {
		kinds[cert.Kind]++
		if cert.Kind == "intermediate" {
			intermediateNames[cert.Name]++
		}
	}
	if kinds["root"] != 4 || kinds["intermediate"] != 2 {
		t.Fatalf("wrong kind counts: %v", kinds)
	}
	if intermediateNames["EM DV TLS CA - G3B"] != 2 {
		t.Fatal("cross-signed variants must remain distinct identities")
	}
	for _, test := range []struct {
		name   string
		mutate func([]incidentCert)
	}{
		{"sha1", func(c []incidentCert) { c[0].SHA1 = "0000000000000000000000000000000000000000" }},
		{"sha256", func(c []incidentCert) { c[0].SHA256 = "bad" }},
		{"name", func(c []incidentCert) { c[0].Name = "unexpected name" }},
		{"kind", func(c []incidentCert) { c[0].Kind = "unknown" }},
		{"source", func(c []incidentCert) { c[0].Source = "" }},
		{"traversal", func(c []incidentCert) { c[0].PEM = "../vulnerable-roots.pem" }},
		{"duplicate", func(c []incidentCert) { c[1] = c[0] }},
	} {
		t.Run(test.name, func(t *testing.T) {
			dir := t.TempDir()
			for _, name := range []string{"vulnerable-roots.pem", "intermediates-1.pem", "intermediates-2.pem"} {
				data, err := os.ReadFile(filepath.Join(fixtureDir(t), name))
				if err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(dir, name), data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			modified := append([]incidentCert(nil), certs...)
			test.mutate(modified)
			data, err := json.Marshal(struct {
				Certs []incidentCert `json:"certs"`
			}{modified})
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(dir, "incident-certs.json")
			if err := os.WriteFile(path, data, 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := loadIncidentCerts(path); err == nil {
				t.Fatal("invalid identity was not refused")
			}
		})
	}
}

func TestFixtureChecksumMismatch(t *testing.T) {
	dir := t.TempDir()
	entries, err := os.ReadDir(fixtureDir(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		data, err := os.ReadFile(filepath.Join(fixtureDir(t), entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, entry.Name()), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifyFixtures(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "incident-certs.json"), []byte("tampered"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := verifyFixtures(dir); err == nil {
		t.Fatal("tampered denial manifest must fail before OS mutation")
	}
}
