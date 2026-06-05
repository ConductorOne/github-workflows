package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var (
	digestA    = "sha256:" + strings.Repeat("a", 64)
	digestAHex = strings.TrimPrefix(digestA, "sha256:")
	digestB    = "sha256:" + strings.Repeat("b", 64)
)

func TestPublishPublicECRTagsPublishesAvailableVersion(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestAHex))
	fake := &fakeAWS{
		describeResults: []describeResult{
			{notFound: true},
			{digest: digestA},
		},
	}

	if _, _, err := runPublishForTest(t, digestFile, fake); err != nil {
		t.Fatalf("publish: %v", err)
	}

	if got := fake.countDescribeTag("1.2.3"); got != 2 {
		t.Fatalf("version tag describe count = %d, want 2", got)
	}
	if !fake.calledPutTag("1.2.3") {
		t.Fatal("version tag was not published")
	}
	if !fake.calledPutTag("latest") {
		t.Fatal("latest tag was not published")
	}
	if fake.countDescribeTag("latest") != 0 {
		t.Fatal("latest must not be part of the ECR release preflight")
	}
	if !fake.calledCommand("batch-delete-image") {
		t.Fatal("temporary candidate tag was not deleted")
	}

	want := fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:1.2.3\n", digestA)
	got, err := os.ReadFile(digestFile)
	if err != nil {
		t.Fatalf("read digest file: %v", err)
	}
	if string(got) != want {
		t.Fatalf("digest file = %q, want %q", got, want)
	}
}

func TestPublishPublicECRTagsKeepsSameVersionIdempotent(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{
			{digest: digestA},
			{digest: digestA},
		},
	}

	if _, _, err := runPublishForTest(t, digestFile, fake); err != nil {
		t.Fatalf("publish: %v", err)
	}

	if fake.calledPutTag("1.2.3") {
		t.Fatal("same digest should not rewrite the version tag")
	}
	if !fake.calledPutTag("latest") {
		t.Fatal("latest tag should still be refreshed")
	}
}

func TestPublishPublicECRTagsRejectsDifferentExistingDigest(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{{digest: digestB}},
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "already points at "+digestB) {
		t.Fatalf("error = %v", err)
	}
	if fake.calledCommand("batch-get-image") {
		t.Fatal("different digest must fail before fetching the candidate manifest")
	}
	if fake.calledCommand("put-image") {
		t.Fatal("different digest must fail before tag publication")
	}
}

func TestPublishPublicECRTagsFailsClosedOnDescribeError(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{{stderr: "AccessDeniedException: denied"}},
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "AccessDeniedException") {
		t.Fatalf("error = %v", err)
	}
	if fake.calledCommand("put-image") {
		t.Fatal("AWS describe errors must fail before tag publication")
	}
}

func TestPublishPublicECRTagsRejectsMissingCandidateDigest(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:latest\n", digestA))
	fake := &fakeAWS{}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "release-candidate-123-1") {
		t.Fatalf("error = %v", err)
	}
	if len(fake.calls) != 0 {
		t.Fatalf("AWS calls = %#v, want none", fake.calls)
	}
}

func TestPublishPublicECRTagsRejectsMalformedCandidateDigest(t *testing.T) {
	digestFile := writeDigestFile(t, "not-a-digest  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n")
	fake := &fakeAWS{}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "candidate digest") {
		t.Fatalf("error = %v", err)
	}
	if len(fake.calls) != 0 {
		t.Fatalf("AWS calls = %#v, want none", fake.calls)
	}
}

func TestPublishPublicECRTagsRejectsPostWriteDigestMismatch(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{
			{notFound: true},
			{digest: digestB},
		},
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "after publication, expected "+digestA) {
		t.Fatalf("error = %v", err)
	}
	if !fake.calledPutTag("1.2.3") {
		t.Fatal("version tag should be written before post-write verification")
	}
	if fake.calledPutTag("latest") {
		t.Fatal("latest must not be published after version digest verification fails")
	}
}

func TestPublishPublicECRTagsRejectsMissingManifest(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	emptyManifest := ""
	fake := &fakeAWS{
		describeResults: []describeResult{{notFound: true}},
		manifest:        &emptyManifest,
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "Could not fetch manifest") {
		t.Fatalf("error = %v", err)
	}
	if fake.calledCommand("put-image") {
		t.Fatal("missing manifest must fail before tag publication")
	}
}

func TestPublishPublicECRTagsFailsClosedOnBatchGetImageError(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{{notFound: true}},
		batchGetErr:     "AccessDeniedException: denied",
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "AccessDeniedException") {
		t.Fatalf("error = %v", err)
	}
	if fake.calledCommand("put-image") {
		t.Fatal("batch-get-image errors must fail before tag publication")
	}
}

func TestPublishPublicECRTagsFailsClosedOnVersionPutImageError(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{{notFound: true}},
		putErr:          "AccessDeniedException: denied",
	}

	_, _, err := runPublishForTest(t, digestFile, fake)
	if err == nil {
		t.Fatal("publish succeeded, want failure")
	}
	if !strings.Contains(err.Error(), "AccessDeniedException") {
		t.Fatalf("error = %v", err)
	}
	if !fake.calledPutTag("1.2.3") {
		t.Fatal("version tag write should have been attempted")
	}
	if got := fake.countDescribeTag("1.2.3"); got != 1 {
		t.Fatalf("version tag describe count = %d, want 1", got)
	}
	if fake.calledPutTag("latest") {
		t.Fatal("latest must not be published after version tag write fails")
	}
}

func TestPublishPublicECRTagsCandidateCleanupIsBestEffort(t *testing.T) {
	digestFile := writeDigestFile(t, fmt.Sprintf("%s  public.ecr.aws/conductorone/bridge-client:release-candidate-123-1\n", digestA))
	fake := &fakeAWS{
		describeResults: []describeResult{
			{notFound: true},
			{digest: digestA},
		},
		deleteErr: "delete denied",
	}

	_, stderr, err := runPublishForTest(t, digestFile, fake)
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	if !strings.Contains(stderr, "::warning::Could not remove temporary Public ECR candidate tag") {
		t.Fatalf("stderr = %q", stderr)
	}
}

func runPublishForTest(t *testing.T, digestFile string, fake *fakeAWS) (string, string, error) {
	t.Helper()
	cfg := config{
		repositoryName: "bridge-client",
		versionTag:     "1.2.3",
		candidateTag:   "release-candidate-123-1",
		digestFile:     digestFile,
		registryURI:    defaultRegistryURI,
	}
	var stdout, stderr bytes.Buffer
	err := publish(cfg, fake, &stdout, &stderr)
	return stdout.String(), stderr.String(), err
}

func writeDigestFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "digests.txt")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write digest file: %v", err)
	}
	return path
}

type describeResult struct {
	digest   string
	notFound bool
	stderr   string
}

type fakeAWS struct {
	describeResults []describeResult
	manifest        *string
	batchGetErr     string
	putErr          string
	deleteErr       string
	calls           [][]string
}

func (f *fakeAWS) Run(args ...string) ([]byte, []byte, error) {
	f.calls = append(f.calls, append([]string(nil), args...))

	if len(args) < 2 || args[0] != "ecr-public" {
		return nil, []byte("unexpected aws call"), errors.New("aws failed")
	}

	switch args[1] {
	case "describe-images":
		if len(f.describeResults) == 0 {
			return nil, []byte("unexpected describe-images call"), errors.New("aws failed")
		}
		result := f.describeResults[0]
		f.describeResults = f.describeResults[1:]
		if result.stderr != "" {
			return nil, []byte(result.stderr), errors.New("aws failed")
		}
		if result.notFound {
			return nil, []byte("ImageNotFoundException: image not found"), errors.New("aws failed")
		}
		stdout, err := json.Marshal(map[string]any{
			"imageDetails": []map[string]string{{"imageDigest": result.digest}},
		})
		return stdout, nil, err
	case "batch-get-image":
		if f.batchGetErr != "" {
			return nil, []byte(f.batchGetErr), errors.New("aws failed")
		}
		manifest := "manifest-json"
		if f.manifest != nil {
			manifest = *f.manifest
		}
		stdout, err := json.Marshal(map[string]any{
			"images": []map[string]string{{"imageManifest": manifest}},
		})
		return stdout, nil, err
	case "put-image":
		if f.putErr != "" {
			return nil, []byte(f.putErr), errors.New("aws failed")
		}
		return []byte(`{"image":{}}`), nil, nil
	case "batch-delete-image":
		if f.deleteErr != "" {
			return nil, []byte(f.deleteErr), errors.New("aws failed")
		}
		return []byte(`{}`), nil, nil
	default:
		return nil, []byte("unexpected aws call"), errors.New("aws failed")
	}
}

func (f *fakeAWS) calledCommand(command string) bool {
	for _, call := range f.calls {
		if len(call) >= 2 && call[1] == command {
			return true
		}
	}
	return false
}

func (f *fakeAWS) calledPutTag(tag string) bool {
	for _, call := range f.calls {
		if len(call) >= 2 && call[1] == "put-image" && argValue(call, "--image-tag") == tag {
			return true
		}
	}
	return false
}

func (f *fakeAWS) countDescribeTag(tag string) int {
	var count int
	for _, call := range f.calls {
		if len(call) >= 2 && call[1] == "describe-images" && argValue(call, "--image-ids") == "imageTag="+tag {
			count++
		}
	}
	return count
}

func argValue(args []string, name string) string {
	for i, arg := range args {
		if arg == name && i+1 < len(args) {
			return args[i+1]
		}
	}
	return ""
}
