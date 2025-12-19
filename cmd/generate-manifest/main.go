package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func main() {
	var (
		assetDir string
		repoName string
		orgName  string
		tag      string
		baseURL  string
	)
	flag.StringVar(&assetDir, "asset-dir", ".", "Directory containing distribution artifacts")
	flag.StringVar(&repoName, "repo-name", "", "Repository name")
	flag.StringVar(&orgName, "org-name", "", "Organization name")
	flag.StringVar(&tag, "tag", "", "Release tag (e.g., v0.0.8)")
	flag.StringVar(&baseURL, "base-url", "", "Base URL for artifact downloads")
	flag.Parse()

	if repoName == "" || orgName == "" || tag == "" || baseURL == "" {
		fmt.Fprintf(os.Stderr, "generate-manifest: error: repo-name, org-name, tag, and base-url are required\n")
		os.Exit(1)
	}

	now := time.Now().UTC()
	assets := make(map[string]*pb.Asset)

	// Asset patterns: platform -> (pattern, mediaType)
	assetPatterns := map[string]struct {
		pattern   string
		mediaType string
	}{
		"darwin-arm64":  {"*darwin-arm64.zip", "application/zip"},
		"darwin-amd64":  {"*darwin-amd64.zip", "application/zip"},
		"linux-arm64":   {"*linux-arm64.tar.gz", "application/gzip"},
		"linux-amd64":   {"*linux-amd64.tar.gz", "application/gzip"},
		"windows-amd64": {"*windows-amd64.zip", "application/zip"},
		"checksums":     {"*checksums.txt", "text/plain"},
	}

	// Parse checksums file first to get SHA256 hashes from goreleaser
	checksumsMap, err := parseChecksumsFile(assetDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate-manifest: error: %v\n", err)
		os.Exit(1)
	}

	// Find and add assets
	for key, pattern := range assetPatterns {
		matches, err := filepath.Glob(filepath.Join(assetDir, pattern.pattern))
		if err != nil || len(matches) == 0 {
			continue
		}
		filename := filepath.Base(matches[0])

		// Get file size
		info, err := os.Stat(filepath.Join(assetDir, filename))
		if err != nil {
			fmt.Fprintf(os.Stderr, "generate-manifest: warning: failed to stat %s: %v\n", filename, err)
			continue
		}
		size := info.Size()

		// Get SHA256 from checksums file (required - must match goreleaser output)
		sha256Hash, ok := checksumsMap[filename]
		if !ok {
			// Skip checksums file itself (it won't be in its own checksums file)
			if key == "checksums" {
				// Calculate hash for the checksums file itself
				calculatedHash, err := calculateFileSHA256(filepath.Join(assetDir, filename))
				if err != nil {
					fmt.Fprintf(os.Stderr, "generate-manifest: error: failed to calculate hash for checksums file %s: %v\n", filename, err)
					os.Exit(1)
				}
				sha256Hash = calculatedHash
			} else {
				fmt.Fprintf(os.Stderr, "generate-manifest: error: SHA256 hash not found in checksums file for %s\n", filename)
				fmt.Fprintf(os.Stderr, "generate-manifest: error: all hashes must come from goreleaser checksums file\n")
				os.Exit(1)
			}
		}

		href := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), filename)
		builder := pb.Asset_builder{
			Filename:        &filename,
			MediaType:       &pattern.mediaType,
			SizeBytes:       &size,
			Sha256:          &sha256Hash,
			Href:            &href,
			SignatureHref:   stringPtr(href + ".sig"),
			CertificateHref: stringPtr(href + ".cert"),
		}

		// Check for provenance attestation bundle
		var attestations []*pb.AttestationDescriptor
		provenancePath := filepath.Join(assetDir, filename+".provenance.sigstore.json")
		if _, err := os.Stat(provenancePath); err == nil {
			attestationType := "https://in-toto.io/Statement/v1"
			predicateType := "https://slsa.dev/provenance/v1"
			bundleHref := fmt.Sprintf("%s/%s.provenance.sigstore.json", strings.TrimSuffix(baseURL, "/"), filename)
			attestations = append(attestations, pb.AttestationDescriptor_builder{
				AttestationType: &attestationType,
				PredicateType:   &predicateType,
				BundleHref:      &bundleHref,
			}.Build())
		}

		// Check for SBOM attestation bundle
		sbomBundlePath := filepath.Join(assetDir, filename+".sbom.sigstore.json")
		if _, err := os.Stat(sbomBundlePath); err == nil {
			attestationType := "https://in-toto.io/Statement/v1"
			predicateType := "https://spdx.dev/Document"
			bundleHref := fmt.Sprintf("%s/%s.sbom.sigstore.json", strings.TrimSuffix(baseURL, "/"), filename)
			attestations = append(attestations, pb.AttestationDescriptor_builder{
				AttestationType: &attestationType,
				PredicateType:   &predicateType,
				BundleHref:      &bundleHref,
			}.Build())
		}

		if len(attestations) > 0 {
			builder.Attestations = attestations
		}

		asset := builder.Build()
		assets[key] = asset
	}

	// Build manifest with builder pattern
	version := "2"
	baseURLTrimmed := strings.TrimSuffix(baseURL, "/")
	signatureHref := fmt.Sprintf("%s/manifest.json.sig", baseURLTrimmed)
	certificateHref := fmt.Sprintf("%s/manifest.json.cert", baseURLTrimmed)

	manifest := pb.Manifest_builder{
		Version:         &version,
		Name:            &repoName,
		Org:             &orgName,
		Semver:          &tag,
		ReleasedAt:      timestamppb.New(now),
		Assets:          assets,
		SignatureHref:   &signatureHref,
		CertificateHref: &certificateHref,
	}.Build()

	// Marshal to JSON
	// Marshal options with frontend consumption in mind. Ensures all fields are present for predictable structure.
	opts := protojson.MarshalOptions{
		Multiline:       true,
		Indent:          "  ",
		EmitUnpopulated: true,
	}
	jsonBytes, err := opts.Marshal(manifest)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate-manifest: error: marshaling manifest: %v\n", err)
		os.Exit(1)
	}

	// Write JSON to stdout (progress messages go to stderr)
	fmt.Println(string(jsonBytes))
	fmt.Fprintln(os.Stderr, "✅ Generated manifest")
}

// parseChecksumsFile parses the goreleaser checksums file and returns a map of filename -> SHA256 hash.
// The checksums file format is: <sha256>  <filename>
// Returns an error if the checksums file cannot be found or read.
func parseChecksumsFile(assetDir string) (map[string]string, error) {
	// Find checksums file
	matches, err := filepath.Glob(filepath.Join(assetDir, "*checksums.txt"))
	if err != nil {
		return nil, fmt.Errorf("failed to search for checksums file: %w", err)
	}
	if len(matches) == 0 {
		return nil, fmt.Errorf("checksums file not found in %s (expected pattern: *checksums.txt)", assetDir)
	}

	checksumsFile := matches[0]
	file, err := os.Open(checksumsFile)
	if err != nil {
		return nil, fmt.Errorf("failed to open checksums file %s: %w", checksumsFile, err)
	}
	defer file.Close()

	checksumsMap := make(map[string]string)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		// Format: <sha256>  <filename> or <sha256>  *<filename>
		parts := strings.Fields(line)
		if len(parts) >= 2 {
			hash := parts[0]
			filename := strings.TrimPrefix(parts[1], "*")
			checksumsMap[filename] = hash
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("failed to read checksums file %s: %w", checksumsFile, err)
	}

	return checksumsMap, nil
}

// calculateFileSHA256 calculates the SHA256 hash of a file.
func calculateFileSHA256(filepath string) (string, error) {
	file, err := os.Open(filepath)
	if err != nil {
		return "", err
	}
	defer file.Close()

	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}

	return hex.EncodeToString(hash.Sum(nil)), nil
}

// stringPtr returns a pointer to the given string value.
func stringPtr(s string) *string {
	return &s
}
