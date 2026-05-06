package main

import (
	"encoding/json"
	"testing"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func TestExtractPublicImages(t *testing.T) {
	images := make(map[string]*pb.Image)
	foundGHCR, foundECR := extractPublicImages([]byte(`
aaa111  ghcr.io/conductorone/baton-example:0.1.2
bbb222  ghcr.io/conductorone/baton-example:0.1.2-amd64
ccc333  public.ecr.aws/conductorone/baton-example:0.1.2
`), "0.1.2", images)

	if !foundGHCR || !foundECR {
		t.Fatalf("foundGHCR=%t foundECR=%t, want both true", foundGHCR, foundECR)
	}
	if images["ghcr"].GetDigest() != "sha256:aaa111" {
		t.Fatalf("ghcr digest = %q", images["ghcr"].GetDigest())
	}
	if !images["ghcr"].GetIsIndex() {
		t.Fatal("ghcr image should be marked as an index")
	}
	if images["ecrPublic"].GetUri() != "public.ecr.aws/conductorone/baton-example@sha256:ccc333" {
		t.Fatalf("ecrPublic uri = %q", images["ecrPublic"].GetUri())
	}
}

func TestExtractLambdaImageUsesGenericRef(t *testing.T) {
	images := make(map[string]*pb.Image)
	found := extractLambdaImage([]byte(`
ddd444  168442440833.dkr.ecr.us-west-2.amazonaws.com/baton-example:0.1.2-arm64
`), "baton-example", "0.1.2", images)

	if !found {
		t.Fatal("lambda image was not found")
	}
	image := images[lambdaArm64ImageKey]
	if image == nil {
		t.Fatalf("missing %s image", lambdaArm64ImageKey)
	}
	if image.GetRef() != "baton-example:0.1.2-arm64" {
		t.Fatalf("lambda ref = %q", image.GetRef())
	}
	if image.GetDigest() != "sha256:ddd444" {
		t.Fatalf("lambda digest = %q", image.GetDigest())
	}
	if image.GetIsIndex() {
		t.Fatal("lambda image should not be marked as an index")
	}
}

func TestMarshalImagesSortsKeys(t *testing.T) {
	isIndex := true
	images := map[string]*pb.Image{
		"lambda-arm64": pb.Image_builder{
			Ref:     strPtr("baton-example:0.1.2-arm64"),
			Digest:  strPtr("sha256:lambda"),
			IsIndex: boolPtr(false),
		}.Build(),
		"ghcr": pb.Image_builder{
			Ref:     strPtr("ghcr.io/conductorone/baton-example:0.1.2"),
			Digest:  strPtr("sha256:ghcr"),
			IsIndex: &isIndex,
		}.Build(),
	}

	got, err := marshalImages(images)
	if err != nil {
		t.Fatalf("marshalImages: %v", err)
	}

	var decoded map[string]json.RawMessage
	if err := json.Unmarshal([]byte(got), &decoded); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, got)
	}
	if len(decoded) != 2 {
		t.Fatalf("decoded keys = %d, want 2", len(decoded))
	}
	if got[4:10] != `"ghcr"` {
		t.Fatalf("first key was not ghcr in sorted output:\n%s", got)
	}
}

func strPtr(s string) *string {
	return &s
}

func boolPtr(v bool) *bool {
	return &v
}
