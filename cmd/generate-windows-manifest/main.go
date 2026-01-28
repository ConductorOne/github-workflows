package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

const (
	// AttestationTypeInTotoV1 is the in-toto Statement v1 envelope type
	AttestationTypeInTotoV1 = "https://in-toto.io/Statement/v1"
	// PredicateTypeSLSAProvenanceV1 is the SLSA v1 provenance predicate type
	PredicateTypeSLSAProvenanceV1 = "https://slsa.dev/provenance/v1"
	// PredicateTypeSPDX is the SPDX SBOM predicate type
	PredicateTypeSPDX = "https://spdx.dev/Document"
)

func main() {
	var (
		distDir    string
		cdnBaseURL string
		s3Dir      string
	)
	flag.StringVar(&distDir, "dist-dir", "", "Path to the dist directory containing Windows artifacts")
	flag.StringVar(&cdnBaseURL, "cdn-base-url", "", "CDN base URL for artifact links")
	flag.StringVar(&s3Dir, "s3-directory", "", "S3 directory path for artifacts")
	flag.Parse()

	if distDir == "" || cdnBaseURL == "" || s3Dir == "" {
		fmt.Fprintf(os.Stderr, "generate-windows-manifest: error: all flags are required\n")
		fmt.Fprintf(os.Stderr, "Usage: generate-windows-manifest -dist-dir <path> -cdn-base-url <url> -s3-directory <dir>\n")
		os.Exit(1)
	}

	baseURL := fmt.Sprintf("%s/%s", cdnBaseURL, s3Dir)
	assets := make(map[string]*pb.Asset)

	// Find and process zip files
	zipFiles, err := filepath.Glob(filepath.Join(distDir, "*.zip"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate-windows-manifest: error finding zip files: %v\n", err)
		os.Exit(1)
	}

	for _, zipPath := range zipFiles {
		filename := filepath.Base(zipPath)
		if strings.Contains(filename, "checksums") {
			continue
		}

		asset, err := buildAsset(zipPath, filename, "application/zip", baseURL, distDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "generate-windows-manifest: error processing %s: %v\n", filename, err)
			os.Exit(1)
		}

		// Windows zip uses key "windows-amd64"
		assets["windows-amd64"] = asset
		fmt.Fprintf(os.Stderr, "✅ Added zip asset: windows-amd64 -> %s\n", filename)
	}

	// Find and process MSI files (flattened to dist root by workflow)
	msiFiles, err := filepath.Glob(filepath.Join(distDir, "*.msi"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate-windows-manifest: error finding MSI files: %v\n", err)
		os.Exit(1)
	}

	for _, msiPath := range msiFiles {
		filename := filepath.Base(msiPath)

		asset, err := buildAsset(msiPath, filename, "application/x-msi", baseURL, distDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "generate-windows-manifest: error processing %s: %v\n", filename, err)
			os.Exit(1)
		}

		// MSI uses key "windows-amd64-msi"
		// MSI has cosign signatures and attestations; Azure Trusted Signing (Windows code signing) planned for Stage 2
		assets["windows-amd64-msi"] = asset
		fmt.Fprintf(os.Stderr, "✅ Added MSI asset: windows-amd64-msi -> %s\n", filename)
	}

	// Marshal assets map to JSON
	// We need to output a map[string]Asset JSON, not a full manifest
	output := make(map[string]json.RawMessage)
	marshalOpts := protojson.MarshalOptions{
		EmitUnpopulated: true,
	}

	for key, asset := range assets {
		jsonBytes, err := marshalOpts.Marshal(asset)
		if err != nil {
			fmt.Fprintf(os.Stderr, "generate-windows-manifest: error marshaling asset %s: %v\n", key, err)
			os.Exit(1)
		}
		output[key] = jsonBytes
	}

	// Output JSON to stdout
	outputBytes, err := json.Marshal(output)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate-windows-manifest: error marshaling output: %v\n", err)
		os.Exit(1)
	}

	fmt.Println(string(outputBytes))
	fmt.Fprintf(os.Stderr, "✅ Generated Windows manifest with %d assets\n", len(assets))
}

func buildAsset(filePath, filename, mediaType, baseURL, distDir string) (*pb.Asset, error) {
	// Calculate SHA256
	hash, err := sha256File(filePath)
	if err != nil {
		return nil, fmt.Errorf("calculating hash: %w", err)
	}

	// Get file size
	info, err := os.Stat(filePath)
	if err != nil {
		return nil, fmt.Errorf("getting file info: %w", err)
	}

	sizeBytes := info.Size()
	href := fmt.Sprintf("%s/%s", baseURL, filename)

	// Check for signature and certificate files (all in dist root after flatten step)
	var signatureHref, certificateHref *string
	sigPath := filepath.Join(distDir, filename+".sig")
	if _, err := os.Stat(sigPath); err == nil {
		s := fmt.Sprintf("%s/%s.sig", baseURL, filename)
		signatureHref = &s
	}
	certPath := filepath.Join(distDir, filename+".cert")
	if _, err := os.Stat(certPath); err == nil {
		c := fmt.Sprintf("%s/%s.cert", baseURL, filename)
		certificateHref = &c
	}

	// Build attestations array
	var attestations []*pb.AttestationDescriptor

	// Check for provenance attestation
	provenancePath := filepath.Join(distDir, filename+".provenance.sigstore.json")
	if _, err := os.Stat(provenancePath); err == nil {
		attestationType := AttestationTypeInTotoV1
		predicateType := PredicateTypeSLSAProvenanceV1
		bundleHref := fmt.Sprintf("%s/%s.provenance.sigstore.json", baseURL, filename)
		attestations = append(attestations, pb.AttestationDescriptor_builder{
			AttestationType: &attestationType,
			PredicateType:   &predicateType,
			BundleHref:      &bundleHref,
		}.Build())
	}

	// Check for SBOM attestation
	sbomPath := filepath.Join(distDir, filename+".sbom.sigstore.json")
	if _, err := os.Stat(sbomPath); err == nil {
		attestationType := AttestationTypeInTotoV1
		predicateType := PredicateTypeSPDX
		bundleHref := fmt.Sprintf("%s/%s.sbom.sigstore.json", baseURL, filename)
		attestations = append(attestations, pb.AttestationDescriptor_builder{
			AttestationType: &attestationType,
			PredicateType:   &predicateType,
			BundleHref:      &bundleHref,
		}.Build())
	}

	return pb.Asset_builder{
		Filename:        &filename,
		MediaType:       &mediaType,
		SizeBytes:       &sizeBytes,
		Sha256:          &hash,
		Href:            &href,
		SignatureHref:   signatureHref,
		CertificateHref: certificateHref,
		Attestations:    attestations,
	}.Build(), nil
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}

	return hex.EncodeToString(h.Sum(nil)), nil
}
