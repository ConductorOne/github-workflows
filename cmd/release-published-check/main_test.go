package main

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReleasePublishedCheckBothPresent(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":false}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !alreadyReleased {
		t.Fatalf("already_released = false, want true (reason: %s)", reason)
	}
	if !strings.Contains(reason, "already published (manifest, GitHub release, and registry record present)") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckManifestMissing(t *testing.T) {
	manifest := newServer(t, http.StatusNotFound, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":false}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if reason != "no published manifest yet" {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckReleaseMissing(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusNotFound, "")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "no GitHub release found") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckReleaseDraft(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":true}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "still a draft") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckRegistryReleaseMissing(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusNotFound, "")

	alreadyReleased, reason, err := runForTestWithRegistry(t, manifest.URL, http.StatusOK, `{"draft":false}`, registry.URL)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "no registry release found") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckRegistryRecordsAnotherCommit(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"old-commit"}}`)

	alreadyReleased, reason, err := runForTestWithRegistry(t, manifest.URL, http.StatusOK, `{"draft":false}`, registry.URL)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "records commit old-commit, not tag-commit") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckLightweightTagWithCommitSHA(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`)
	fake := githubFake{
		release: githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:     githubFakeResponse{status: http.StatusOK, body: `{"object":{"type":"commit","sha":"tag-commit"}}`},
	}

	alreadyReleased, reason, err := runWithGitHubFake(t, manifest.URL, fake, registry.URL, "tag-commit")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !alreadyReleased {
		t.Fatalf("already_released = false, want true (reason: %s)", reason)
	}
}

func TestReleasePublishedCheckAnnotatedTagIsPeeled(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`)
	fake := githubFake{
		release:   githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:       githubFakeResponse{status: http.StatusOK, body: `{"object":{"type":"tag","sha":"tag-object"}}`},
		tagObject: githubFakeResponse{status: http.StatusOK, body: `{"object":{"type":"commit","sha":"tag-commit"}}`},
	}

	// cfg.commitSHA is "tag-object": an annotated tag push delivers github.sha
	// as the tag object's SHA, not the commit it points to.
	alreadyReleased, reason, err := runWithGitHubFake(t, manifest.URL, fake, registry.URL, "tag-object")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !alreadyReleased {
		t.Fatalf("already_released = false, want true (reason: %s)", reason)
	}
}

func TestReleasePublishedCheckPushForAnotherCommit(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	var registryCalls int
	registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		registryCalls++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"release":{"commitSha":"tag-commit"}}`))
	}))
	t.Cleanup(registry.Close)
	fake := githubFake{
		release: githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:     githubFakeResponse{status: http.StatusOK, body: `{"object":{"type":"commit","sha":"tag-commit"}}`},
	}

	alreadyReleased, reason, err := runWithGitHubFake(t, manifest.URL, fake, registry.URL, "other-commit")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "now points at tag-commit; this push was for other-commit") {
		t.Fatalf("reason = %q", reason)
	}
	if registryCalls != 0 {
		t.Fatalf("registry calls = %d, want 0: a commit mismatch must short-circuit before the registry check", registryCalls)
	}
}

func TestReleasePublishedCheckTagRefMissing(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`)
	fake := githubFake{
		release: githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:     githubFakeResponse{status: http.StatusNotFound},
	}

	alreadyReleased, reason, err := runWithGitHubFake(t, manifest.URL, fake, registry.URL, "tag-commit")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if reason != "no tag v1.2.3 found" {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckTagRefUnavailable(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`)
	fake := githubFake{
		release: githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:     githubFakeResponse{status: http.StatusServiceUnavailable},
	}

	alreadyReleased, reason, err := runWithGitHubFake(t, manifest.URL, fake, registry.URL, "tag-commit")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "could not verify") {
		t.Fatalf("reason = %q", reason)
	}
}

// TestReleasePublishedCheckDraftFieldPlantValidated is the plant validation
// for the draft check: it fails until the draft field gates publication.
func TestReleasePublishedCheckDraftFieldPlantValidated(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")

	alreadyReleased, _, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":true}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("a draft release must not be treated as already published")
	}
}

// An object-store-backed CDN answers 403, not 404, for a key that does not
// exist, so a fresh release must read as "not yet published", not as an error.
func TestReleasePublishedCheckManifestForbiddenMeansMissing(t *testing.T) {
	manifest := newServer(t, http.StatusForbidden, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":false}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if reason != "no published manifest yet" {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckManifestUnavailable(t *testing.T) {
	manifest := newServer(t, http.StatusServiceUnavailable, "")

	alreadyReleased, reason, err := runForTest(t, manifest.URL, http.StatusOK, `{"draft":false}`)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "could not verify") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckManifestTransportError(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	manifestURL := manifest.URL
	manifest.Close() // closed server: connection refused

	alreadyReleased, reason, err := runForTest(t, manifestURL, http.StatusOK, `{"draft":false}`)
	if err != nil {
		t.Fatalf("run: %v, want nil (unverifiable state must not exit non-zero)", err)
	}
	if alreadyReleased {
		t.Fatal("already_released = true, want false")
	}
	if !strings.Contains(reason, "could not verify") {
		t.Fatalf("reason = %q", reason)
	}
}

func TestReleasePublishedCheckWritesOutputFile(t *testing.T) {
	manifest := newServer(t, http.StatusOK, "")
	github := newGitHubFake(t, githubFake{
		release: githubFakeResponse{status: http.StatusOK, body: `{"draft":false}`},
		ref:     defaultGitHubRef,
	})

	outputPath := filepath.Join(t.TempDir(), "output.txt")
	cfg := config{
		manifestURL:  manifest.URL,
		repo:         "conductorone/example",
		tag:          "v1.2.3",
		githubAPIURL: github.URL,
		registryURL:  newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`).URL,
		commitSHA:    "tag-commit",
		outputPath:   outputPath,
	}
	var stdout, stderr bytes.Buffer
	if _, _, err := run(cfg, http.DefaultClient, &stdout, &stderr); err != nil {
		t.Fatalf("run: %v", err)
	}

	got, err := os.ReadFile(outputPath)
	if err != nil {
		t.Fatalf("read output file: %v", err)
	}
	want := fmt.Sprintf("already_released=true\nreason=release %s is already published (manifest, GitHub release, and registry record present)\n", cfg.tag)
	if string(got) != want {
		t.Fatalf("output file = %q, want %q", got, want)
	}
}

// A reason carrying a newline must still land as one key=value line.
func TestReleasePublishedCheckOutputFlattensReason(t *testing.T) {
	outputPath := filepath.Join(t.TempDir(), "output.txt")
	if err := appendOutput(outputPath, false, "could not verify publication: line one\nline two"); err != nil {
		t.Fatalf("appendOutput: %v", err)
	}
	got, err := os.ReadFile(outputPath)
	if err != nil {
		t.Fatalf("read output file: %v", err)
	}
	want := "already_released=false\nreason=could not verify publication: line one line two\n"
	if string(got) != want {
		t.Fatalf("output file = %q, want %q", got, want)
	}
}

func newServer(t *testing.T, status int, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		if body != "" {
			_, _ = w.Write([]byte(body))
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

type githubFakeResponse struct {
	status int
	body   string
}

// githubFake is path-routed because release-published-check now calls three
// distinct GitHub API endpoints behind one base URL.
type githubFake struct {
	release, ref, tagObject githubFakeResponse
}

func newGitHubFake(t *testing.T, fake githubFake) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := githubFakeResponse{status: http.StatusNotFound}
		switch {
		case strings.HasSuffix(r.URL.Path, "/releases/tags/v1.2.3"):
			resp = fake.release
		case strings.HasSuffix(r.URL.Path, "/git/ref/tags/v1.2.3"):
			resp = fake.ref
		case strings.Contains(r.URL.Path, "/git/tags/"):
			resp = fake.tagObject
		}
		w.WriteHeader(resp.status)
		if resp.body != "" {
			_, _ = w.Write([]byte(resp.body))
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// defaultGitHubRef is a lightweight tag pointing straight at a commit, the
// shape every existing (pre-annotated-tag) test should keep exercising.
var defaultGitHubRef = githubFakeResponse{status: http.StatusOK, body: `{"object":{"type":"commit","sha":"tag-commit"}}`}

func runForTest(t *testing.T, manifestURL string, releaseStatus int, releaseBody string) (bool, string, error) {
	t.Helper()
	registry := newServer(t, http.StatusOK, `{"release":{"commitSha":"tag-commit"}}`)
	return runForTestWithRegistry(t, manifestURL, releaseStatus, releaseBody, registry.URL)
}

func runForTestWithRegistry(t *testing.T, manifestURL string, releaseStatus int, releaseBody string, registryURL string) (bool, string, error) {
	t.Helper()
	fake := githubFake{
		release: githubFakeResponse{status: releaseStatus, body: releaseBody},
		ref:     defaultGitHubRef,
	}
	return runWithGitHubFake(t, manifestURL, fake, registryURL, "tag-commit")
}

// runWithGitHubFake is for tests that need to control the ref/tagObject
// responses (annotated-tag peeling) or the pushed commit SHA directly.
func runWithGitHubFake(t *testing.T, manifestURL string, fake githubFake, registryURL, commitSHA string) (bool, string, error) {
	t.Helper()
	github := newGitHubFake(t, fake)
	cfg := config{
		manifestURL:  manifestURL,
		repo:         "conductorone/example",
		tag:          "v1.2.3",
		githubAPIURL: github.URL,
		registryURL:  registryURL,
		commitSHA:    commitSHA,
	}
	var stdout, stderr bytes.Buffer
	return run(cfg, http.DefaultClient, &stdout, &stderr)
}
