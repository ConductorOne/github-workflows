package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

const (
	inTotoStatement = "https://in-toto.io/Statement/v1"
	slsaProvenance  = "https://slsa.dev/provenance/v1"
	spdxDocument    = "https://spdx.dev/Document"

	// upstream503Body mirrors what the registry API sends back when an
	// asset's signature/certificate fetch from the CDN hits a transient 503:
	// an ordinary-looking 400 that is actually worth retrying.
	upstream503Body = `{"code":3,"message":"asset \"windows-amd64-msi\" detached signature files are required: fetch certificate: GET https://example.invalid/releases/org/name/v1.2.3/name_v1.2.3_windows_amd64.msi.cert returned HTTP 503"}`

	// ordinary400Body is a real validation failure; it must never be retried.
	ordinary400Body = `{"code":3,"message":"version is required"}`
)

func TestTransformAssetsPreservesAssetAttestations(t *testing.T) {
	sizeBytes := int64(123)
	manifest := pb.Manifest_builder{
		Assets: map[string]*pb.Asset{
			"linux-amd64": pb.Asset_builder{
				Filename:  strPtr("baton-example-v1.2.3-linux-amd64.tar.gz"),
				MediaType: strPtr("application/gzip"),
				SizeBytes: &sizeBytes,
				Sha256:    strPtr("asset-sha"),
				Href:      strPtr("https://dist.example.com/asset.tar.gz"),
				Attestations: []*pb.AttestationDescriptor{
					attestation(slsaProvenance, "https://dist.example.com/provenance.sigstore.json"),
					attestation(spdxDocument, "https://dist.example.com/sbom.sigstore.json"),
				},
			}.Build(),
		},
	}.Build()

	assets := transformAssets(manifest)
	got := assets["linux-amd64"].Attestations
	want := []*ReleaseAttestation{
		{Type: slsaProvenance, URL: "https://dist.example.com/provenance.sigstore.json"},
		{Type: spdxDocument, URL: "https://dist.example.com/sbom.sigstore.json"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("attestations = %#v, want %#v", got, want)
	}
}

func TestTransformAssetsMapsUpdaterSignatureToMetadata(t *testing.T) {
	manifest := pb.Manifest_builder{
		Assets: map[string]*pb.Asset{
			// The macOS updater bundle carries the minisign signature.
			"darwin-universal-updater": pb.Asset_builder{
				Filename:         strPtr("baton-example-v1.2.3-darwin-universal.app.tar.gz"),
				MediaType:        strPtr("application/gzip"),
				Href:             strPtr("https://dist.example.com/updater.app.tar.gz"),
				UpdaterSignature: strPtr("dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduCg=="),
			}.Build(),
			// A regular asset must not gain any metadata.
			"linux-amd64": pb.Asset_builder{
				Filename:  strPtr("baton-example-v1.2.3-linux-amd64.tar.gz"),
				MediaType: strPtr("application/gzip"),
				Href:      strPtr("https://dist.example.com/asset.tar.gz"),
			}.Build(),
		},
	}.Build()

	assets := transformAssets(manifest)

	updater := assets["darwin-universal-updater"]
	wantMeta := map[string]string{"updater.signature": "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduCg=="}
	if !reflect.DeepEqual(updater.Metadata, wantMeta) {
		t.Fatalf("updater metadata = %#v, want %#v", updater.Metadata, wantMeta)
	}

	if plain := assets["linux-amd64"]; plain.Metadata != nil {
		t.Fatalf("non-updater asset metadata = %#v, want nil", plain.Metadata)
	}

	// Metadata is omitted from JSON when nil, present when set.
	plainBody, err := json.Marshal(assets["linux-amd64"])
	if err != nil {
		t.Fatalf("marshal plain asset: %v", err)
	}
	if strings.Contains(string(plainBody), "\"metadata\"") {
		t.Fatalf("plain asset JSON unexpectedly contains metadata: %s", plainBody)
	}
	updaterBody, err := json.Marshal(updater)
	if err != nil {
		t.Fatalf("marshal updater asset: %v", err)
	}
	if !strings.Contains(string(updaterBody), "\"updater.signature\"") {
		t.Fatalf("updater asset JSON missing metadata signature: %s", updaterBody)
	}
}

func TestTransformAttestationsSkipsIncompleteAssetEntries(t *testing.T) {
	got := transformAttestations([]*pb.AttestationDescriptor{
		attestation(slsaProvenance, "https://dist.example.com/provenance.sigstore.json"),
		attestation("", "https://dist.example.com/missing-predicate.sigstore.json"),
		attestation(spdxDocument, ""),
	})
	want := []*ReleaseAttestation{
		{Type: slsaProvenance, URL: "https://dist.example.com/provenance.sigstore.json"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("attestations = %#v, want %#v", got, want)
	}
}

func TestTransformImagesAppliesManifestImageAttestation(t *testing.T) {
	isIndex := true
	manifest := pb.Manifest_builder{
		ImageAttestation: attestation(slsaProvenance, ""),
		Images: map[string]*pb.Image{
			"ecrPublic": pb.Image_builder{
				Ref:     strPtr("public.ecr.aws/example/baton-example:v1.2.3"),
				Digest:  strPtr("sha256:ecr"),
				IsIndex: &isIndex,
			}.Build(),
		},
	}.Build()

	images := transformImages(manifest)
	for platform, image := range images {
		want := []*ReleaseAttestation{{Type: slsaProvenance}}
		if !reflect.DeepEqual(image.Attestations, want) {
			t.Fatalf("%s attestations = %#v, want %#v", platform, image.Attestations, want)
		}
	}
}

func TestTransformImagesSkipsAttestationForNonIndexImage(t *testing.T) {
	isIndex := false
	manifest := pb.Manifest_builder{
		ImageAttestation: attestation(slsaProvenance, ""),
		Images: map[string]*pb.Image{
			"lambda-arm64": pb.Image_builder{
				Ref:     strPtr("baton-example:1.2.3-arm64"),
				Digest:  strPtr("sha256:lambda"),
				IsIndex: &isIndex,
			}.Build(),
		},
	}.Build()

	images := transformImages(manifest)
	if len(images["lambda-arm64"].Attestations) != 0 {
		t.Fatalf("lambda attestations = %#v, want none", images["lambda-arm64"].Attestations)
	}
}

func TestRecordReleaseRequestMarshalsAttestations(t *testing.T) {
	req := &RecordReleaseRequest{
		Org:                "example",
		Name:               "baton-example",
		Version:            "v1.2.3",
		ManifestURL:        "https://dist.example.com/manifest.json",
		SignatureBundleURL: "https://dist.example.com/manifest.json.sigstore.json",
		Assets: map[string]*ReleaseAsset{
			"linux-amd64": {
				Platform: "linux-amd64",
				Attestations: []*ReleaseAttestation{
					{Type: slsaProvenance, URL: "https://dist.example.com/provenance.sigstore.json"},
				},
			},
		},
		Images: map[string]*ReleaseImage{
			"ecrPublic": {
				Platform:     "ecrPublic",
				Attestations: []*ReleaseAttestation{{Type: slsaProvenance}},
			},
		},
	}

	body, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}

	var got struct {
		ManifestURL        string `json:"manifestUrl"`
		SignatureBundleURL string `json:"signatureBundleUrl"`
		Assets             map[string]struct {
			Attestations []ReleaseAttestation `json:"attestations"`
		} `json:"assets"`
		Images map[string]struct {
			Attestations []ReleaseAttestation `json:"attestations"`
		} `json:"images"`
	}
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("unmarshal request: %v", err)
	}

	if got.ManifestURL == "" || got.SignatureBundleURL == "" {
		t.Fatalf("manifest signature metadata was not marshaled: %#v", got)
	}
	if len(got.Assets["linux-amd64"].Attestations) != 1 {
		t.Fatalf("asset attestations = %#v, want one entry", got.Assets["linux-amd64"].Attestations)
	}
	if got.Assets["linux-amd64"].Attestations[0].URL == "" {
		t.Fatal("asset attestation URL was not marshaled")
	}
	if len(got.Images["ecrPublic"].Attestations) != 1 {
		t.Fatalf("image attestations = %#v, want one entry", got.Images["ecrPublic"].Attestations)
	}
	if got.Images["ecrPublic"].Attestations[0].URL != "" {
		t.Fatalf("image attestation URL = %q, want empty", got.Images["ecrPublic"].Attestations[0].URL)
	}
}

// TestTruncateChangelogFitsRegistryLimit: a release body far past the
// registry's 10000-character limit (a year of goreleaser commit lines, as
// baton-sap-grc v0.3.0 shipped at 15972) comes back under the limit, cut on a
// line boundary, ending with the truncation note.
func TestTruncateChangelogFitsRegistryLimit(t *testing.T) {
	var b strings.Builder
	b.WriteString("## Changelog\n")
	for i := 0; i < 300; i++ {
		fmt.Fprintf(&b, "* %040x chore: update dependency & Go versions — entry %d\n", i, i)
	}
	body := b.String()
	if utf8.RuneCountInString(body) <= changelogMaxChars {
		t.Fatalf("fixture must exceed the limit, has %d characters", utf8.RuneCountInString(body))
	}
	got := truncateChangelog(body)
	if n := utf8.RuneCountInString(got); n > changelogMaxChars {
		t.Fatalf("truncated changelog has %d characters, limit %d", n, changelogMaxChars)
	}
	if !strings.HasSuffix(got, "truncated to the registry's 10000-character limit.") {
		t.Fatalf("truncation note missing or not last:\n%s", got[len(got)-200:])
	}
	// The kept part ends on a whole input line, not mid-commit.
	kept := strings.TrimSuffix(got, got[strings.LastIndex(got, "\n\n… truncated"):])
	lines := strings.Split(body, "\n")
	last := kept[strings.LastIndex(kept, "\n")+1:]
	found := false
	for _, l := range lines {
		if l == last {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("kept text ends mid-line: %q", last)
	}
	if !utf8.ValidString(got) {
		t.Fatalf("truncated changelog is not valid UTF-8")
	}
}

// TestTruncateChangelogLeavesShortAndExactAlone: at or under the limit the
// body is returned as is, including one that is exactly at the limit.
func TestTruncateChangelogLeavesShortAndExactAlone(t *testing.T) {
	for _, body := range []string{"", "## Changelog\n* one line\n", strings.Repeat("é", changelogMaxChars)} {
		if got := truncateChangelog(body); got != body {
			t.Fatalf("changelog of %d characters was changed", utf8.RuneCountInString(body))
		}
	}
}

// TestTruncateChangelogHandlesCRLF: a body with Windows line endings (GitHub
// keeps them when a release is edited in the browser) still ends on a whole
// line, with no stray carriage return before the note.
func TestTruncateChangelogHandlesCRLF(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 300; i++ {
		fmt.Fprintf(&b, "* %040x chore: update dependency & Go versions\r\n", i)
	}
	got := truncateChangelog(b.String())
	if n := utf8.RuneCountInString(got); n > changelogMaxChars {
		t.Fatalf("truncated changelog has %d characters, limit %d", n, changelogMaxChars)
	}
	if strings.Contains(got, "\r\n\n…") || strings.Contains(got, "\r\n…") {
		t.Fatalf("carriage return left before the truncation note")
	}
	if !strings.Contains(got, "\n\n… truncated") {
		t.Fatalf("truncation note missing")
	}
}

// TestTruncateChangelogCountsCharactersNotBytes: multibyte text is cut on rune
// boundaries and measured the way protovalidate measures max_len.
func TestTruncateChangelogCountsCharactersNotBytes(t *testing.T) {
	body := strings.Repeat("é", changelogMaxChars+500) // no line breaks at all
	got := truncateChangelog(body)
	if !utf8.ValidString(got) {
		t.Fatalf("cut landed inside a multibyte sequence")
	}
	if n := utf8.RuneCountInString(got); n > changelogMaxChars {
		t.Fatalf("truncated changelog has %d characters, limit %d", n, changelogMaxChars)
	}
	if !strings.HasSuffix(got, "-character limit.") {
		t.Fatalf("truncation note missing")
	}
}

func attestation(predicateType, bundleHref string) *pb.AttestationDescriptor {
	return pb.AttestationDescriptor_builder{
		AttestationType: strPtr(inTotoStatement),
		PredicateType:   strPtr(predicateType),
		BundleHref:      strPtr(bundleHref),
	}.Build()
}

func strPtr(s string) *string {
	return &s
}

// scriptedResponse is one canned reply from a scripted test server.
type scriptedResponse struct {
	status int
	body   string
}

// newScriptedServer replies with responses in order and fails the test if
// any request's body differs from wantBody: recordRelease must resend the
// same payload on every attempt.
func newScriptedServer(t *testing.T, wantBody []byte, responses []scriptedResponse) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		i := int(calls.Add(1)) - 1
		got, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("reading request body: %v", err)
		}
		if !bytes.Equal(got, wantBody) {
			t.Fatalf("request %d body = %q, want %q", i+1, got, wantBody)
		}
		idx := i
		if idx >= len(responses) {
			idx = len(responses) - 1
		}
		w.WriteHeader(responses[idx].status)
		fmt.Fprint(w, responses[idx].body)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

// stubSleep replaces the package-level sleep with a no-op for the duration
// of the test, so retries run instantly instead of waiting for real.
func stubSleep(t *testing.T) {
	t.Helper()
	orig := sleep
	sleep = func(time.Duration) {}
	t.Cleanup(func() { sleep = orig })
}

func TestRecordReleaseRetriesTransient503ThenSucceeds(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusServiceUnavailable, body: "upstream unavailable"},
		{status: http.StatusOK, body: ""},
	})

	var stdout, stderr bytes.Buffer
	if err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr); err != nil {
		t.Fatalf("recordRelease: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("requests = %d, want 2", got)
	}
	if !strings.Contains(stdout.String(), `"status":"success"`) {
		t.Fatalf("stdout = %q, want success JSON", stdout.String())
	}
	if n := strings.Count(stderr.String(), "attempt 1/5"); n != 1 {
		t.Fatalf("stderr = %q, want exactly one 'attempt 1/5' line", stderr.String())
	}
}

func TestRecordReleaseRetriesTruncatedResponseBody(t *testing.T) {
	stubSleep(t)
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			w.Header().Set("Content-Length", "100")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte("truncated"))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	var stdout, stderr bytes.Buffer
	if err := recordRelease(srv.Client(), srv.URL, []byte(`{"version":"v1.2.3"}`), "v1.2.3", defaultRetryPolicy, &stdout, &stderr); err != nil {
		t.Fatalf("recordRelease: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("requests = %d, want 2", got)
	}
	if !strings.Contains(stderr.String(), "reading response body") {
		t.Fatalf("stderr = %q, want response-read retry reason", stderr.String())
	}
}

func TestRecordReleaseRetries400WithUpstream5xxMessage(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusBadRequest, body: upstream503Body},
		{status: http.StatusOK, body: ""},
	})

	var stdout, stderr bytes.Buffer
	if err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr); err != nil {
		t.Fatalf("recordRelease: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("requests = %d, want 2", got)
	}
}

func TestRecordReleaseDoesNotRetryOrdinary400(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusBadRequest, body: ordinary400Body},
	})

	var stdout, stderr bytes.Buffer
	err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr)
	if err == nil {
		t.Fatal("recordRelease: want error, got nil")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("requests = %d, want 1", got)
	}
	if strings.Contains(stderr.String(), "attempt") {
		t.Fatalf("stderr = %q, want no retry line", stderr.String())
	}
	var regErr *registryError
	if !errors.As(err, &regErr) {
		t.Fatalf("error = %#v, want *registryError", err)
	}
	if regErr.status != http.StatusBadRequest || regErr.attempts != 1 {
		t.Fatalf("registryError = %+v, want status=400 attempts=1", regErr)
	}
}

func TestRecordReleaseGivesUpAfterFiveServerErrors(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusServiceUnavailable, body: "down"},
		{status: http.StatusServiceUnavailable, body: "down"},
		{status: http.StatusServiceUnavailable, body: "down"},
		{status: http.StatusServiceUnavailable, body: "down"},
		{status: http.StatusServiceUnavailable, body: "down"},
	})

	var stdout, stderr bytes.Buffer
	err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr)
	if err == nil {
		t.Fatal("recordRelease: want error, got nil")
	}
	if got := calls.Load(); got != 5 {
		t.Fatalf("requests = %d, want 5", got)
	}
	var regErr *registryError
	if !errors.As(err, &regErr) || regErr.attempts != 5 {
		t.Fatalf("error = %#v, want *registryError with attempts=5", err)
	}
	if !strings.Contains(err.Error(), "after 5 attempts") {
		t.Fatalf("error = %q, want 'after 5 attempts'", err.Error())
	}
}

func TestRecordReleaseRetries429ThenSucceeds(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusTooManyRequests, body: "slow down"},
		{status: http.StatusOK, body: ""},
	})

	var stdout, stderr bytes.Buffer
	if err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr); err != nil {
		t.Fatalf("recordRelease: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("requests = %d, want 2", got)
	}
}

func TestRecordReleaseReportsAlreadyExistsOn409(t *testing.T) {
	stubSleep(t)
	body := []byte(`{"version":"v1.2.3"}`)
	srv, calls := newScriptedServer(t, body, []scriptedResponse{
		{status: http.StatusConflict, body: ""},
	})

	var stdout, stderr bytes.Buffer
	if err := recordRelease(srv.Client(), srv.URL, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr); err != nil {
		t.Fatalf("recordRelease: %v", err)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("requests = %d, want 1", got)
	}
	if !strings.Contains(stdout.String(), `"status":"already_exists"`) {
		t.Fatalf("stdout = %q, want already_exists JSON", stdout.String())
	}
}

func TestRecordReleaseGivesUpAfterFiveTransportErrors(t *testing.T) {
	stubSleep(t)
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	client := srv.Client()
	endpoint := srv.URL
	srv.Close() // connections to endpoint now fail -- a genuine transport error.

	body := []byte(`{"version":"v1.2.3"}`)
	var stdout, stderr bytes.Buffer
	err := recordRelease(client, endpoint, body, "v1.2.3", defaultRetryPolicy, &stdout, &stderr)
	if err == nil {
		t.Fatal("recordRelease: want error, got nil")
	}
	var transErr *transportError
	if !errors.As(err, &transErr) || transErr.attempts != 5 {
		t.Fatalf("error = %#v, want *transportError with attempts=5", err)
	}
	if !strings.Contains(err.Error(), "request failed after 5 attempts") {
		t.Fatalf("error = %q, want 'request failed after 5 attempts'", err.Error())
	}
}
