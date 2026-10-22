package main

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/url"
	"testing"
	"time"

	"github.com/sigstore/rekor/pkg/generated/models"
	"github.com/sigstore/rekor/pkg/util"
	"github.com/sigstore/sigstore/pkg/signature"
	"github.com/sigstore/sigstore/pkg/signature/options"
)

func fixtureProof(t *testing.T) (observation, *models.LogEntryAnon, *util.SignedCheckpoint, signature.SignerVerifier) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := signature.LoadECDSASignerVerifier(key, crypto.SHA256)
	if err != nil {
		t.Fatal(err)
	}
	body := base64.StdEncoding.EncodeToString([]byte(`{"kind":"fixture"}`))
	leaf := sha256.Sum256(append([]byte{0}, []byte(`{"kind":"fixture"}`)...))
	checkpoint, err := util.CreateSignedCheckpoint(util.Checkpoint{Origin: "fixture", Size: 1, Hash: leaf[:]})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := checkpoint.Sign("fixture", signer, options.WithContext(context.Background())); err != nil {
		t.Fatal(err)
	}
	note, err := checkpoint.MarshalText()
	if err != nil {
		t.Fatal(err)
	}
	// Rekor's signed global index differs from the shard-local proof position.
	index, localIndex, size, integrated := int64(100), int64(0), int64(1), int64(1700000000)
	rootHash, logID, noteText := hex.EncodeToString(leaf[:]), "fixture-log", string(note)
	setPayload, err := json.Marshal(map[string]any{"body": body, "integratedTime": integrated, "logID": logID, "logIndex": index})
	if err != nil {
		t.Fatal(err)
	}
	set, err := signer.SignMessage(bytes.NewReader(setPayload))
	if err != nil {
		t.Fatal(err)
	}
	entry := &models.LogEntryAnon{Body: body, IntegratedTime: &integrated, LogIndex: &index, LogID: &logID,
		Verification: &models.LogEntryAnonVerification{SignedEntryTimestamp: set,
			InclusionProof: &models.InclusionProof{Checkpoint: &noteText, Hashes: []string{}, LogIndex: &localIndex, TreeSize: &size, RootHash: &rootHash}}}
	return observation{UUID: "0000000000000001" + rootHash, Index: index}, entry, checkpoint, signer
}

func TestProofVerification(t *testing.T) {
	for _, mutation := range []string{"valid", "body", "index", "root", "checkpoint", "timestamp", "consistency", "uuid"} {
		t.Run(mutation, func(t *testing.T) {
			observed, entry, saved, verifier := fixtureProof(t)
			switch mutation {
			case "body":
				entry.Body = base64.StdEncoding.EncodeToString([]byte("tampered"))
			case "index":
				observed.Index = 1
			case "root":
				*entry.Verification.InclusionProof.RootHash = "00"
			case "checkpoint":
				*entry.Verification.InclusionProof.Checkpoint += "invalid"
			case "timestamp":
				*entry.IntegratedTime += 1
			case "uuid":
				observed.UUID = "0000000000000001" + string(bytes.Repeat([]byte("a"), 64))
			}
			called := false
			consistent := func(context.Context, *util.SignedCheckpoint, *util.SignedCheckpoint, string) error {
				called = true
				if mutation == "consistency" {
					return errors.New("fork")
				}
				return nil
			}
			err := checkProof(context.Background(), observed, entry, verifier, saved, consistent)
			if mutation == "valid" {
				if err != nil || !called {
					t.Fatalf("valid proof: called=%v err=%v", called, err)
				}
			} else if err == nil {
				t.Fatal("accepted altered proof")
			}
		})
	}
}

func TestCertificateChainAndDigest(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1700000000, 0)
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "fixture CA"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, err = x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	san, _ := url.Parse("https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@refs/tags/v4")
	issuer := "https://token.actions.githubusercontent.com"
	issuerDER, _ := asn1.Marshal(issuer)
	leaf := &x509.Certificate{SerialNumber: big.NewInt(2), NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Minute),
		URIs: []*url.URL{san}, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageCodeSigning},
		ExtraExtensions: []pkix.Extension{{Id: asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 57264, 1, 8}, Value: issuerDER}}}
	der, err := x509.CreateCertificate(rand.Reader, leaf, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert := base64.StdEncoding.EncodeToString(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
	digest := hex.EncodeToString(bytes.Repeat([]byte{1}, 32))
	body, _ := json.Marshal(map[string]any{"kind": "hashedrekord", "spec": map[string]any{
		"data":      map[string]any{"hash": map[string]string{"algorithm": "sha256", "value": digest}},
		"signature": map[string]any{"publicKey": map[string]string{"content": cert}}}})
	integrated := now.Unix()
	entry := &models.LogEntryAnon{Body: base64.StdEncoding.EncodeToString(body), IntegratedTime: &integrated}
	observed := observation{CertSubject: san.String(), Issuer: issuer}
	roots := x509.NewCertPool()
	roots.AddCert(ca)
	got := metadata(observed, entry, roots, x509.NewCertPool())
	if got.Error != "" || got.Digest != digest || got.Identity == nil {
		t.Fatalf("authenticated cert: %+v", got)
	}
	got = metadata(observed, entry, x509.NewCertPool(), x509.NewCertPool())
	if got.Error != "UNAUTHENTICATED-SAN" || got.Identity != nil {
		t.Fatalf("untrusted cert: %+v", got)
	}
	for _, kind := range []string{"dsse", "intoto"} {
		entry.Body = base64.StdEncoding.EncodeToString([]byte(`{"kind":"` + kind + `","spec":{"payloadHash":{"value":"` + digest + `"}}}`))
		got = metadata(observed, entry, roots, x509.NewCertPool())
		if got.Digest != "" || got.Identity != nil {
			t.Fatal("opaque payload hash became artifact digest")
		}
	}
}
