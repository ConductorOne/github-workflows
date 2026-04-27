package main

import (
	"encoding/json"
	"reflect"
	"testing"

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
			"ghcr": pb.Image_builder{
				Ref:     strPtr("ghcr.io/example/baton-example:v1.2.3"),
				Digest:  strPtr("sha256:ghcr"),
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

func TestRecordReleaseRequestMarshalsAttestations(t *testing.T) {
	req := &RecordReleaseRequest{
		Org:     "example",
		Name:    "baton-example",
		Version: "v1.2.3",
		Assets: map[string]*ReleaseAsset{
			"linux-amd64": {
				Platform: "linux-amd64",
				Attestations: []*ReleaseAttestation{
					{Type: slsaProvenance, URL: "https://dist.example.com/provenance.sigstore.json"},
				},
			},
		},
		Images: map[string]*ReleaseImage{
			"ghcr": {
				Platform:     "ghcr",
				Attestations: []*ReleaseAttestation{{Type: slsaProvenance}},
			},
		},
	}

	body, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}

	var got struct {
		Assets map[string]struct {
			Attestations []ReleaseAttestation `json:"attestations"`
		} `json:"assets"`
		Images map[string]struct {
			Attestations []ReleaseAttestation `json:"attestations"`
		} `json:"images"`
	}
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("unmarshal request: %v", err)
	}

	if len(got.Assets["linux-amd64"].Attestations) != 1 {
		t.Fatalf("asset attestations = %#v, want one entry", got.Assets["linux-amd64"].Attestations)
	}
	if got.Assets["linux-amd64"].Attestations[0].URL == "" {
		t.Fatal("asset attestation URL was not marshaled")
	}
	if len(got.Images["ghcr"].Attestations) != 1 {
		t.Fatalf("image attestations = %#v, want one entry", got.Images["ghcr"].Attestations)
	}
	if got.Images["ghcr"].Attestations[0].URL != "" {
		t.Fatalf("image attestation URL = %q, want empty", got.Images["ghcr"].Attestations[0].URL)
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
