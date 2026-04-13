package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

// RecordReleaseRequest is the JSON body sent to the registry API.
type RecordReleaseRequest struct {
	Org            string                   `json:"org"`
	Name           string                   `json:"name"`
	Version        string                   `json:"version"`
	RepositoryURL  string                   `json:"repositoryUrl"`
	CommitSha      string                   `json:"commitSha"`
	WorkflowRunID  string                   `json:"workflowRunId"`
	Documentation  string                   `json:"documentation,omitempty"`
	Changelog      string                   `json:"changelog,omitempty"`
	ConfigSchema   string                   `json:"configSchema,omitempty"`
	Capabilities   string                   `json:"capabilities,omitempty"`
	SignatureURL   string                   `json:"signatureUrl,omitempty"`
	CertificateURL string                   `json:"certificateUrl,omitempty"`
	Assets         map[string]*ReleaseAsset `json:"assets,omitempty"`
	Images         map[string]*ReleaseImage `json:"images,omitempty"`
	ReleasedAt     string                   `json:"releasedAt,omitempty"`
}

// ReleaseAsset is the transformed asset for the registry API.
type ReleaseAsset struct {
	Platform       string `json:"platform"`
	Filename       string `json:"filename"`
	MediaType      string `json:"mediaType"`
	SizeBytes      int64  `json:"sizeBytes"`
	Sha256         string `json:"sha256"`
	DownloadURL    string `json:"downloadUrl"`
	SignatureURL   string `json:"signatureUrl,omitempty"`
	CertificateURL string `json:"certificateUrl,omitempty"`
	SbomURL        string `json:"sbomUrl,omitempty"`
}

// ReleaseImage is the transformed image for the registry API.
type ReleaseImage struct {
	Ref      string `json:"ref"`
	Digest   string `json:"digest"`
	Platform string `json:"platform"`
}

// authTransport adds a Bearer token to every outgoing request.
type authTransport struct {
	token string
	base  http.RoundTripper
}

func (t *authTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("Authorization", "Bearer "+t.token)
	req.Header.Set("Content-Type", "application/json")
	return t.base.RoundTrip(req)
}

func main() {
	var (
		manifestPath  string
		docsPath      string
		org           string
		name          string
		version       string
		repositoryURL string
		commitSha     string
		workflowRunID string
		registryURL      string
		changelogPath    string
		configSchemaPath string
		capabilitiesPath string
		token            string
	)

	flag.StringVar(&manifestPath, "manifest", "", "Path to merged manifest.json file (required)")
	flag.StringVar(&docsPath, "docs", "", "Path to docs/connector.mdx file (optional)")
	flag.StringVar(&org, "org", "", "GitHub organization (required)")
	flag.StringVar(&name, "name", "", "Repository/connector name (required)")
	flag.StringVar(&version, "version", "", "Release version tag (required)")
	flag.StringVar(&repositoryURL, "repository-url", "", "Full repository URL (required)")
	flag.StringVar(&commitSha, "commit-sha", "", "Git commit SHA (required)")
	flag.StringVar(&workflowRunID, "workflow-run-id", "", "GitHub Actions workflow run ID (required)")
	flag.StringVar(&registryURL, "registry-url", "", "Registry API base URL (required)")
	flag.StringVar(&changelogPath, "changelog", "", "Path to a file containing release notes (optional)")
	flag.StringVar(&configSchemaPath, "config-schema", "", "Path to config_schema.json file (optional)")
	flag.StringVar(&capabilitiesPath, "capabilities", "", "Path to baton_capabilities.json file (optional)")
	var releasedAt string
	flag.StringVar(&releasedAt, "released-at", "", "Release publish timestamp in RFC 3339 format (optional, defaults to server time)")
	flag.StringVar(&token, "token", "", "Bearer token (or set REGISTRY_API_TOKEN env var)")
	flag.Parse()

	// Validate required flags
	var missing []string
	if manifestPath == "" {
		missing = append(missing, "-manifest")
	}
	if org == "" {
		missing = append(missing, "-org")
	}
	if name == "" {
		missing = append(missing, "-name")
	}
	if version == "" {
		missing = append(missing, "-version")
	}
	if repositoryURL == "" {
		missing = append(missing, "-repository-url")
	}
	if commitSha == "" {
		missing = append(missing, "-commit-sha")
	}
	if workflowRunID == "" {
		missing = append(missing, "-workflow-run-id")
	}
	if registryURL == "" {
		missing = append(missing, "-registry-url")
	}
	if len(missing) > 0 {
		fmt.Fprintf(os.Stderr, "record-release: error: missing required flags: %s\n", strings.Join(missing, ", "))
		flag.Usage()
		os.Exit(1)
	}

	// Resolve token: flag > env var
	if token == "" {
		token = os.Getenv("REGISTRY_API_TOKEN")
	}
	if token == "" {
		fmt.Fprintf(os.Stderr, "record-release: error: bearer token required (use -token flag or REGISTRY_API_TOKEN env var)\n")
		os.Exit(1)
	}

	// Read and parse manifest using protojson (same pattern as merge-manifests)
	manifestBytes, err := os.ReadFile(manifestPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: reading manifest: %v\n", err)
		os.Exit(1)
	}

	manifest := &pb.Manifest{}
	unmarshalOpts := protojson.UnmarshalOptions{
		DiscardUnknown: true,
	}
	if err := unmarshalOpts.Unmarshal(manifestBytes, manifest); err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: parsing manifest: %v\n", err)
		os.Exit(1)
	}

	// Read optional documentation
	var documentation string
	if docsPath != "" {
		docsBytes, err := os.ReadFile(docsPath)
		if err != nil {
			// Not fatal -- docs are optional
			fmt.Fprintf(os.Stderr, "record-release: warning: could not read docs file: %v\n", err)
		} else {
			documentation = string(docsBytes)
		}
	}

	// Read optional changelog / release notes
	var changelog string
	if changelogPath != "" {
		changelogBytes, err := os.ReadFile(changelogPath)
		if err != nil {
			// Not fatal -- changelog is optional
			fmt.Fprintf(os.Stderr, "record-release: warning: could not read changelog file: %v\n", err)
		} else {
			changelog = string(changelogBytes)
		}
	}

	// Read optional config_schema.json (committed to connector repo by CI)
	var configSchema string
	if configSchemaPath != "" {
		data, err := os.ReadFile(configSchemaPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "record-release: warning: could not read config-schema file: %v\n", err)
		} else {
			configSchema = string(data)
		}
	}

	// Read optional baton_capabilities.json (committed to connector repo by CI)
	var capabilities string
	if capabilitiesPath != "" {
		data, err := os.ReadFile(capabilitiesPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "record-release: warning: could not read capabilities file: %v\n", err)
		} else {
			capabilities = string(data)
		}
	}

	// Transform manifest assets: href->downloadUrl, signatureHref->signatureUrl, etc.
	assets := make(map[string]*ReleaseAsset)
	for platform, asset := range manifest.GetAssets() {
		assets[platform] = &ReleaseAsset{
			Platform:       platform,
			Filename:       asset.GetFilename(),
			MediaType:      asset.GetMediaType(),
			SizeBytes:      asset.GetSizeBytes(),
			Sha256:         asset.GetSha256(),
			DownloadURL:    asset.GetHref(),
			SignatureURL:   asset.GetSignatureHref(),
			CertificateURL: asset.GetCertificateHref(),
			SbomURL:        asset.GetSbomHref(),
		}
	}

	// Transform manifest images: extract ref, digest, add platform from map key
	images := make(map[string]*ReleaseImage)
	for platform, image := range manifest.GetImages() {
		images[platform] = &ReleaseImage{
			Ref:      image.GetRef(),
			Digest:   image.GetDigest(),
			Platform: platform,
		}
	}

	// Build request body
	req := &RecordReleaseRequest{
		Org:            org,
		Name:           name,
		Version:        version,
		RepositoryURL:  repositoryURL,
		CommitSha:      commitSha,
		WorkflowRunID:  workflowRunID,
		Documentation:  documentation,
		Changelog:      changelog,
		ConfigSchema:   configSchema,
		Capabilities:   capabilities,
		SignatureURL:   manifest.GetSignatureHref(),
		CertificateURL: manifest.GetCertificateHref(),
		Assets:         assets,
		Images:         images,
		ReleasedAt:     releasedAt,
	}

	bodyBytes, err := json.Marshal(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: marshaling request body: %v\n", err)
		os.Exit(1)
	}

	// POST to registry API
	baseURL, err := url.Parse(registryURL)
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: parsing registry URL: %v\n", err)
		os.Exit(1)
	}
	endpoint := baseURL.JoinPath("/api/v1/ingest/release")
	httpReq, err := http.NewRequest(http.MethodPost, endpoint.String(), bytes.NewReader(bodyBytes))
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: creating HTTP request: %v\n", err)
		os.Exit(1)
	}

	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &authTransport{
			token: token,
			base:  http.DefaultTransport,
		},
	}
	resp, err := client.Do(httpReq)
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: HTTP request failed: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "record-release: error: reading response body: %v\n", err)
		os.Exit(1)
	}

	// Handle response codes
	switch resp.StatusCode {
	case http.StatusOK:
		result, _ := json.Marshal(map[string]interface{}{
			"status":  "success",
			"code":    http.StatusOK,
			"version": version,
		})
		fmt.Println(string(result))
	case http.StatusConflict:
		// 409 = already exists, not an error (expected during dual-write migration)
		result, _ := json.Marshal(map[string]interface{}{
			"status":  "already_exists",
			"code":    http.StatusConflict,
			"version": version,
		})
		fmt.Println(string(result))
	default:
		fmt.Fprintf(os.Stderr, "::error::Registry API record failed: HTTP %d\n", resp.StatusCode)
		fmt.Fprintf(os.Stderr, "%s\n", string(respBody))
		os.Exit(1)
	}
}
