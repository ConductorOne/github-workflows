package main

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

const (
	inTotoStatement = "https://in-toto.io/Statement/v1"
	slsaProvenance  = "https://slsa.dev/provenance/v1"
	spdxDocument    = "https://spdx.dev/Document"
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
