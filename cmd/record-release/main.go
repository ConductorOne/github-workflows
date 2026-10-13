package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

// RecordReleaseRequest is the JSON body sent to the registry API.
type RecordReleaseRequest struct {
	Org                string                   `json:"org"`
	Name               string                   `json:"name"`
	Version            string                   `json:"version"`
	RepositoryURL      string                   `json:"repositoryUrl"`
	CommitSha          string                   `json:"commitSha"`
	WorkflowRunID      string                   `json:"workflowRunId"`
	Documentation      string                   `json:"documentation,omitempty"`
	Changelog          string                   `json:"changelog,omitempty"`
	ConfigSchema       string                   `json:"configSchema,omitempty"`
	Capabilities       string                   `json:"capabilities,omitempty"`
	SignatureURL       string                   `json:"signatureUrl,omitempty"`
	CertificateURL     string                   `json:"certificateUrl,omitempty"`
	ManifestURL        string                   `json:"manifestUrl,omitempty"`
	SignatureBundleURL string                   `json:"signatureBundleUrl,omitempty"`
	Assets             map[string]*ReleaseAsset `json:"assets,omitempty"`
	Images             map[string]*ReleaseImage `json:"images,omitempty"`
	ReleasedAt         string                   `json:"releasedAt,omitempty"`
}

// ReleaseAsset is the transformed asset for the registry API.
type ReleaseAsset struct {
	Platform       string                `json:"platform"`
	Filename       string                `json:"filename"`
	MediaType      string                `json:"mediaType"`
	SizeBytes      int64                 `json:"sizeBytes"`
	Sha256         string                `json:"sha256"`
	DownloadURL    string                `json:"downloadUrl"`
	SignatureURL   string                `json:"signatureUrl,omitempty"`
	CertificateURL string                `json:"certificateUrl,omitempty"`
	SbomURL        string                `json:"sbomUrl,omitempty"`
	Attestations   []*ReleaseAttestation `json:"attestations,omitempty"`
	// Metadata carries free-form, per-asset key/value pairs that the registry
	// stores on registry.v1.Asset.metadata. Currently used to forward the Tauri
	// updater bundle's minisign signature under updaterSignatureMetadataKey.
	// Left nil (and omitted from JSON) for assets without any metadata.
	Metadata map[string]string `json:"metadata,omitempty"`
}

const (
	// updaterSignatureMetadataKey is the well-known registry metadata key under
	// which a Tauri auto-update bundle's base64 minisign signature is stored
	// (registry.v1.Asset.metadata["updater.signature"]).
	updaterSignatureMetadataKey = "updater.signature"

	// changelogMaxChars mirrors the registry API's validation of the
	// changelog field (registry.v1 RecordReleaseRequest.changelog,
	// `string.max_len = 10000`), counted in Unicode characters as
	// protovalidate does. A release that ships a long backlog carries a
	// commit list well past it; the registry field is a summary, and the
	// full notes stay on the GitHub release.
	changelogMaxChars = 10000
)

// truncateChangelog fits changelog under changelogMaxChars, counted in Unicode
// characters, ending with a note that it was cut. The cut prefers the last
// line break before the limit so a commit entry is not split mid-line; if the
// notes have no line break in range, it cuts at the last character that fits.
// Input at or under the limit is returned unchanged.
func truncateChangelog(changelog string) string {
	if utf8.RuneCountInString(changelog) <= changelogMaxChars {
		return changelog
	}
	suffix := "\n\n… truncated to the registry's " + fmt.Sprint(changelogMaxChars) + "-character limit."
	budget := changelogMaxChars - utf8.RuneCountInString(suffix)
	// Advance rune by rune so the cut never lands inside a multibyte sequence.
	cut := 0
	for i := 0; i < budget && cut < len(changelog); i++ {
		_, size := utf8.DecodeRuneInString(changelog[cut:])
		cut += size
	}
	head := changelog[:cut]
	if nl := strings.LastIndex(head, "\n"); nl > 0 {
		head = head[:nl]
	}
	return strings.TrimRight(head, "\r\n") + suffix
}

// ReleaseImage is the transformed image for the registry API.
type ReleaseImage struct {
	Ref          string                `json:"ref"`
	Digest       string                `json:"digest"`
	Platform     string                `json:"platform"`
	Attestations []*ReleaseAttestation `json:"attestations,omitempty"`
}

// ReleaseAttestation is the registry API attestation shape.
type ReleaseAttestation struct {
	Type   string `json:"type"`
	URL    string `json:"url,omitempty"`
	Digest string `json:"digest,omitempty"`
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

// retryPolicy bounds how recordRelease retries a POST to the registry API:
// up to maxAttempts tries total, waiting baseDelay after the first failure
// and doubling the wait after each subsequent one.
type retryPolicy struct {
	maxAttempts int
	baseDelay   time.Duration
}

var defaultRetryPolicy = retryPolicy{maxAttempts: 5, baseDelay: 10 * time.Second}

// sleep is time.Sleep by default; tests stub it out so retries run instantly.
var sleep = time.Sleep

// upstreamFiveXXRe matches a registry 400 response whose message reports
// that fetching an asset's signature or certificate from the CDN returned a
// 5xx -- a transient upstream failure the API surfaces as a validation error.
var upstreamFiveXXRe = regexp.MustCompile(`returned HTTP 5[0-9][0-9]`)

// registryError carries the registry API's final HTTP response once
// recordRelease stops retrying, so main can format the ::error:: lines.
type registryError struct {
	status   int
	body     []byte
	attempts int
}

func (e *registryError) Error() string {
	return fmt.Sprintf("HTTP %d after %d attempt%s", e.status, e.attempts, plural(e.attempts))
}

// transportError carries a request-level failure (no HTTP response at all)
// once recordRelease stops retrying.
type transportError struct {
	err      error
	attempts int
}

func (e *transportError) Error() string {
	return fmt.Sprintf("request failed after %d attempt%s: %v", e.attempts, plural(e.attempts), e.err)
}

func (e *transportError) Unwrap() error { return e.err }

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// shouldRetryStatus reports whether an HTTP response is worth retrying: a
// 5xx, a 429, or a 400 the API attributes to the CDN itself returning a 5xx.
// Any other 4xx is a real validation failure and is not retried.
func shouldRetryStatus(status int, body []byte) bool {
	if status == http.StatusTooManyRequests || status >= http.StatusInternalServerError {
		return true
	}
	return status == http.StatusBadRequest && upstreamFiveXXRe.Match(body)
}

// retryWait doubles base on each successive attempt (1-indexed): base, then
// 2x, 4x, and so on.
func retryWait(base time.Duration, attempt int) time.Duration {
	return base * time.Duration(1<<uint(attempt-1))
}

// logBody returns up to the first 200 bytes of body with embedded newlines
// flattened to spaces, for a compact single-line stderr log.
func logBody(body []byte) string {
	if len(body) > 200 {
		body = body[:200]
	}
	s := strings.ReplaceAll(string(body), "\r\n", " ")
	s = strings.ReplaceAll(s, "\n", " ")
	return strings.ReplaceAll(s, "\r", " ")
}

// writeStatusJSON prints the status JSON main has always printed on 200/409.
func writeStatusJSON(w io.Writer, status string, code int, version string) {
	result, _ := json.Marshal(map[string]interface{}{
		"status":  status,
		"code":    code,
		"version": version,
	})
	fmt.Fprintln(w, string(result))
}

// recordRelease POSTs body to endpoint, retrying per policy when the failure
// looks transient (see shouldRetryStatus). HTTP 200 and 409 both write the
// matching status JSON to stdout and return nil; any other outcome returns a
// *registryError or *transportError describing the final attempt. The
// request body is re-sent unchanged on every attempt.
func recordRelease(client *http.Client, endpoint string, body []byte, version string, policy retryPolicy, stdout, stderr io.Writer) error {
	var (
		lastStatus int
		lastBody   []byte
		lastErr    error
		attempt    int
	)

	for attempt = 1; attempt <= policy.maxAttempts; attempt++ {
		httpReq, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return fmt.Errorf("creating HTTP request: %w", err)
		}

		resp, err := client.Do(httpReq)
		if err != nil {
			lastErr = err
			if attempt == policy.maxAttempts {
				break
			}
			wait := retryWait(policy.baseDelay, attempt)
			fmt.Fprintf(stderr, "record-release: attempt %d/%d failed (transport error): %v; retrying in %s\n",
				attempt, policy.maxAttempts, err, wait)
			sleep(wait)
			continue
		}

		respBody, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			lastErr = fmt.Errorf("reading response body: %w", readErr)
			if attempt == policy.maxAttempts {
				break
			}
			wait := retryWait(policy.baseDelay, attempt)
			fmt.Fprintf(stderr, "record-release: attempt %d/%d failed (transport error): %v; retrying in %s\n",
				attempt, policy.maxAttempts, lastErr, wait)
			sleep(wait)
			continue
		}
		lastErr = nil

		switch resp.StatusCode {
		case http.StatusOK:
			writeStatusJSON(stdout, "success", http.StatusOK, version)
			return nil
		case http.StatusConflict:
			writeStatusJSON(stdout, "already_exists", http.StatusConflict, version)
			return nil
		}

		lastStatus, lastBody = resp.StatusCode, respBody

		if !shouldRetryStatus(resp.StatusCode, respBody) || attempt == policy.maxAttempts {
			break
		}

		wait := retryWait(policy.baseDelay, attempt)
		fmt.Fprintf(stderr, "record-release: attempt %d/%d failed (HTTP %d): %s; retrying in %s\n",
			attempt, policy.maxAttempts, resp.StatusCode, logBody(respBody), wait)
		sleep(wait)
	}

	if lastErr != nil {
		return &transportError{err: lastErr, attempts: attempt}
	}
	return &registryError{status: lastStatus, body: lastBody, attempts: attempt}
}

func main() {
	var (
		manifestPath     string
		docsPath         string
		org              string
		name             string
		version          string
		repositoryURL    string
		commitSha        string
		workflowRunID    string
		registryURL      string
		changelogPath    string
		configSchemaPath string
		capabilitiesPath string
		manifestURL      string
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
	flag.StringVar(&manifestURL, "manifest-url", "", "Published manifest.json URL (required)")
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
	if manifestURL == "" {
		missing = append(missing, "-manifest-url")
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
	if manifest.GetSignatureBundleHref() == "" {
		fmt.Fprintf(os.Stderr, "record-release: error: manifest missing signatureBundleHref\n")
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
	if full := utf8.RuneCountInString(changelog); full > changelogMaxChars {
		changelog = truncateChangelog(changelog)
		fmt.Fprintf(os.Stderr, "record-release: changelog truncated from %d to %d characters (registry limit %d)\n",
			full, utf8.RuneCountInString(changelog), changelogMaxChars)
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

	assets := transformAssets(manifest)
	images := transformImages(manifest)

	// Build request body
	req := &RecordReleaseRequest{
		Org:                org,
		Name:               name,
		Version:            version,
		RepositoryURL:      repositoryURL,
		CommitSha:          commitSha,
		WorkflowRunID:      workflowRunID,
		Documentation:      documentation,
		Changelog:          changelog,
		ConfigSchema:       configSchema,
		Capabilities:       capabilities,
		SignatureURL:       manifest.GetSignatureHref(),
		CertificateURL:     manifest.GetCertificateHref(),
		ManifestURL:        manifestURL,
		SignatureBundleURL: manifest.GetSignatureBundleHref(),
		Assets:             assets,
		Images:             images,
		ReleasedAt:         releasedAt,
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

	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &authTransport{
			token: token,
			base:  http.DefaultTransport,
		},
	}

	if err := recordRelease(client, endpoint.String(), bodyBytes, version, defaultRetryPolicy, os.Stdout, os.Stderr); err != nil {
		var regErr *registryError
		var transErr *transportError
		switch {
		case errors.As(err, &regErr):
			fmt.Fprintf(os.Stderr, "::error::Registry API record failed after %d attempt%s: HTTP %d\n",
				regErr.attempts, plural(regErr.attempts), regErr.status)
			fmt.Fprintf(os.Stderr, "%s\n", string(regErr.body))
		case errors.As(err, &transErr):
			fmt.Fprintf(os.Stderr, "::error::Registry API request failed after %d attempt%s: %v\n",
				transErr.attempts, plural(transErr.attempts), transErr.err)
		default:
			fmt.Fprintf(os.Stderr, "record-release: error: %v\n", err)
		}
		os.Exit(1)
	}
}

func transformAssets(manifest *pb.Manifest) map[string]*ReleaseAsset {
	assets := make(map[string]*ReleaseAsset)
	for platform, asset := range manifest.GetAssets() {
		ra := &ReleaseAsset{
			Platform:       platform,
			Filename:       asset.GetFilename(),
			MediaType:      asset.GetMediaType(),
			SizeBytes:      asset.GetSizeBytes(),
			Sha256:         asset.GetSha256(),
			DownloadURL:    asset.GetHref(),
			SignatureURL:   asset.GetSignatureHref(),
			CertificateURL: asset.GetCertificateHref(),
			SbomURL:        asset.GetSbomHref(),
			Attestations:   transformAttestations(asset.GetAttestations()),
		}
		// The Tauri updater bundle carries a base64 minisign signature that must
		// reach the registry as per-asset metadata. Only the updater asset sets
		// updaterSignature; every other asset leaves Metadata nil/omitted.
		if sig := asset.GetUpdaterSignature(); sig != "" {
			ra.Metadata = map[string]string{updaterSignatureMetadataKey: sig}
		}
		assets[platform] = ra
	}

	return assets
}

func transformImages(manifest *pb.Manifest) map[string]*ReleaseImage {
	images := make(map[string]*ReleaseImage)
	for platform, image := range manifest.GetImages() {
		images[platform] = &ReleaseImage{
			Ref:          image.GetRef(),
			Digest:       image.GetDigest(),
			Platform:     platform,
			Attestations: transformImageAttestations(image, manifest.GetImageAttestation()),
		}
	}

	return images
}

// Asset-level attestations are the source of truth in the registry. The dist
// exporter derives the manifest-level assetAttestation summary from them.
func transformAttestations(in []*pb.AttestationDescriptor) []*ReleaseAttestation {
	var out []*ReleaseAttestation
	for _, att := range in {
		if att.GetPredicateType() == "" || att.GetBundleHref() == "" {
			continue
		}
		out = append(out, &ReleaseAttestation{
			Type: att.GetPredicateType(),
			URL:  att.GetBundleHref(),
		})
	}
	return out
}

func transformImageAttestations(image *pb.Image, att *pb.AttestationDescriptor) []*ReleaseAttestation {
	if image == nil || !image.GetIsIndex() {
		return nil
	}
	if att == nil || att.GetPredicateType() == "" {
		return nil
	}
	// The registry stores image attestations per image. Dist manifests keep a
	// single imageAttestation summary, and the exporter recreates that summary
	// from the per-image entries.
	//
	// Image attestations are discovered through OCI referrers, so bundleHref is
	// intentionally empty in current release manifests.
	return []*ReleaseAttestation{{
		Type: att.GetPredicateType(),
		URL:  att.GetBundleHref(),
	}}
}
