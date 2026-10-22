// Command rekor-entry authenticates v1 entry proofs before emitting digest metadata.
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/sigstore/rekor/pkg/client"
	"github.com/sigstore/rekor/pkg/generated/client/entries"
	"github.com/sigstore/rekor/pkg/generated/models"
	"github.com/sigstore/rekor/pkg/util"
	"github.com/sigstore/rekor/pkg/verify"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/tuf"
	"github.com/sigstore/sigstore/pkg/signature"
)

type observation struct {
	UUID        string
	Index       int64
	CertSubject string
	Issuer      string
}

type result struct {
	UUID           string               `json:"uuid"`
	Index          int64                `json:"index"`
	Kind           string               `json:"kind,omitempty"`
	Digest         string               `json:"digest,omitempty"`
	IntegratedTime int64                `json:"integrated_time,omitempty"`
	Identity       *certificate.Summary `json:"identity,omitempty"`
	Error          string               `json:"error,omitempty"`
}

// checkProof binds the body, position, tree size and root to signed log data.
// The consistency callback additionally joins this checkpoint to the monitor's view.
func checkProof(ctx context.Context, observed observation, entry *models.LogEntryAnon,
	verifier signature.Verifier, saved *util.SignedCheckpoint,
	consistent func(context.Context, *util.SignedCheckpoint, *util.SignedCheckpoint, string) error) error {
	if entry.Verification == nil || entry.Verification.InclusionProof == nil || entry.LogIndex == nil || entry.IntegratedTime == nil {
		return errors.New("entry lacks inclusion or timestamp evidence")
	}
	proof := entry.Verification.InclusionProof
	if proof.LogIndex == nil || proof.TreeSize == nil || proof.RootHash == nil || proof.Checkpoint == nil || *entry.LogIndex != observed.Index || *proof.LogIndex < 0 || *proof.TreeSize <= *proof.LogIndex {
		return errors.New("entry position does not match observation")
	}
	body, ok := entry.Body.(string)
	if !ok {
		return errors.New("entry body is not base64")
	}
	raw, err := base64.StdEncoding.DecodeString(body)
	if err != nil {
		return err
	}
	leaf := sha256.Sum256(append([]byte{0}, raw...))
	if len(observed.UUID) != 80 || !strings.HasSuffix(observed.UUID, hex.EncodeToString(leaf[:])) {
		return errors.New("entry UUID does not bind its body")
	}
	if err := verify.VerifyLogEntry(ctx, entry, verifier); err != nil {
		return fmt.Errorf("verify log entry: %w", err)
	}
	checkpoint := &util.SignedCheckpoint{}
	if err := checkpoint.UnmarshalText([]byte(*proof.Checkpoint)); err != nil {
		return err
	}
	if checkpoint.Size != uint64(*proof.TreeSize) || saved.Origin != checkpoint.Origin || !saved.Verify(verifier) {
		return errors.New("proof checkpoint is not the monitored tree")
	}
	treeID, err := strconv.ParseUint(observed.UUID[:16], 16, 64)
	if err != nil {
		return err
	}
	older, newer := saved, checkpoint
	if older.Size > newer.Size {
		older, newer = newer, older
	}
	return consistent(ctx, older, newer, strconv.FormatUint(treeID, 10))
}

func metadata(observed observation, entry *models.LogEntryAnon, roots, intermediates *x509.CertPool) result {
	out := result{UUID: observed.UUID, Index: observed.Index, IntegratedTime: *entry.IntegratedTime}
	var body struct {
		Kind string `json:"kind"`
		Spec struct {
			Data struct {
				Hash struct{ Algorithm, Value string }
			} `json:"data"`
			Signature struct{ PublicKey struct{ Content string } } `json:"signature"`
		} `json:"spec"`
	}
	raw, err := base64.StdEncoding.DecodeString(entry.Body.(string))
	if err == nil {
		err = json.Unmarshal(raw, &body)
	}
	if err != nil {
		out.Error = err.Error()
		return out
	}
	out.Kind = body.Kind
	if body.Kind != "hashedrekord" {
		return out
	} // Opaque attestation payloads are deliberately UNKNOWN.
	certPEM, err := base64.StdEncoding.DecodeString(body.Spec.Signature.PublicKey.Content)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	block, _ := pem.Decode(certPEM)
	if block == nil || block.Type != "CERTIFICATE" {
		out.Error = "entry has no certificate"
		return out
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	_, err = cert.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates,
		CurrentTime: time.Unix(*entry.IntegratedTime, 0), KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageCodeSigning}})
	if err != nil {
		out.Error = "UNAUTHENTICATED-SAN"
		return out
	}
	summary, err := certificate.SummarizeCertificate(cert)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	if summary.SubjectAlternativeName != observed.CertSubject || summary.Issuer != observed.Issuer {
		out.Error = "certificate identity differs from observation"
		return out
	}
	out.Identity = &summary
	digest, err := hex.DecodeString(body.Spec.Data.Hash.Value)
	if body.Spec.Data.Hash.Algorithm != "sha256" || err != nil || len(digest) != 32 {
		out.Error = "unsupported signed digest"
		return out
	}
	out.Digest = hex.EncodeToString(digest)
	return out
}

func pool(path string) (*x509.CertPool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(data) {
		return nil, fmt.Errorf("no certificates in %s", path)
	}
	return pool, nil
}

func run(ctx context.Context) error {
	checkpointPath := flag.String("checkpoint", "monitor-state/checkpoint.txt", "verified monitor checkpoint")
	rootsPath := flag.String("roots", "tools/rekor-fulcio-roots.pem", "committed Fulcio roots")
	intermediatesPath := flag.String("intermediates", "tools/rekor-fulcio-intermediates.pem", "committed Fulcio intermediates")
	flag.Parse()
	roots, err := pool(*rootsPath)
	if err != nil {
		return err
	}
	intermediates, err := pool(*intermediatesPath)
	if err != nil {
		return err
	}
	savedText, err := os.ReadFile(*checkpointPath)
	if err != nil {
		return err
	}
	saved := &util.SignedCheckpoint{}
	if err := saved.UnmarshalText([]byte(strings.ReplaceAll(strings.TrimSpace(string(savedText)), `\n`, "\n"))); err != nil {
		return err
	}
	tufOptions := tuf.DefaultOptions()
	tufOptions.Context = ctx
	trusted, err := root.FetchTrustedRootWithOptions(tufOptions)
	if err != nil {
		return err
	}
	rekorClient, err := client.GetRekorClient("https://rekor.sigstore.dev")
	if err != nil {
		return err
	}
	consistent := func(ctx context.Context, older, newer *util.SignedCheckpoint, treeID string) error {
		return verify.ProveConsistency(ctx, rekorClient, older, newer, treeID)
	}
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	encoder := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		var observed observation
		if err := json.Unmarshal(scanner.Bytes(), &observed); err != nil {
			return err
		}
		if len(observed.UUID) != 80 {
			return errors.New("unsupported v1 entry identifier")
		}
		params := entries.NewGetLogEntryByUUIDParamsWithContext(ctx).WithEntryUUID(observed.UUID)
		response, err := rekorClient.Entries.GetLogEntryByUUID(params)
		out := result{UUID: observed.UUID, Index: observed.Index}
		if err == nil {
			entry, ok := response.Payload[observed.UUID]
			if !ok {
				err = errors.New("response omitted requested entry")
			} else {
				if entry.LogID == nil {
					err = errors.New("entry lacks log ID")
				} else if log := trusted.RekorLogs()[*entry.LogID]; log == nil {
					err = errors.New("log ID is not in the TUF trusted root")
				} else {
					verifier, loadErr := signature.LoadVerifier(log.PublicKey, log.HashFunc)
					err = loadErr
					if err == nil {
						err = checkProof(ctx, observed, &entry, verifier, saved, consistent)
					}
					if err == nil {
						out = metadata(observed, &entry, roots, intermediates)
					}
				}
			}
		}
		if err != nil {
			out.Error = err.Error()
		}
		if err := encoder.Encode(out); err != nil {
			return err
		}
	}
	return scanner.Err()
}

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	if err := run(ctx); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
