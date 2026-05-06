package main

import (
	"encoding/json"
	"testing"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func TestExtractPublicImages(t *testing.T) {
	images := make(map[string]*pb.Image)
	foundECR := extractPublicImages([]byte(`
ccc333  public.ecr.aws/conductorone/baton-example:0.1.2
`), "0.1.2", images)

	if !foundECR {
		t.Fatal("ECR public image was not found")
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
		"ecrPublic": pb.Image_builder{
			Ref:     strPtr("public.ecr.aws/conductorone/baton-example:0.1.2"),
			Digest:  strPtr("sha256:ecr"),
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
	if got[4:15] != `"ecrPublic"` {
		t.Fatalf("first key was not ecrPublic in sorted output:\n%s", got)
	}
}

func strPtr(s string) *string {
	return &s
}

func boolPtr(v bool) *bool {
	return &v
}
