package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

const (
	// AttestationTypeInTotoV1 is the in-toto Statement v1 envelope type
	AttestationTypeInTotoV1 = "https://in-toto.io/Statement/v1"
	// PredicateTypeSLSAProvenanceV1 is the SLSA v1 provenance predicate type
	PredicateTypeSLSAProvenanceV1 = "https://slsa.dev/provenance/v1"
)

func main() {
	var (
		binariesManifest string
		imagesManifest   string
		windowsManifest  string
	)
	flag.StringVar(&binariesManifest, "binaries-manifest", "", "JSON string of binaries manifest")
	flag.StringVar(&imagesManifest, "images-manifest", "", "JSON string of images manifest (optional)")
	flag.StringVar(&windowsManifest, "windows-manifest", "", "JSON string of Windows manifest (optional)")
	flag.Parse()

	if binariesManifest == "" {
		fmt.Fprintf(os.Stderr, "merge-manifests: error: binaries-manifest is required\n")
		os.Exit(1)
	}

	// Parse binaries manifest
	manifest := &pb.Manifest{}
	opts := protojson.UnmarshalOptions{
		DiscardUnknown: true,
	}
	if err := opts.Unmarshal([]byte(binariesManifest), manifest); err != nil {
		fmt.Fprintf(os.Stderr, "merge-manifests: ::error::Invalid JSON in binaries_manifest output\n")
		fmt.Fprintf(os.Stderr, "merge-manifests: Raw content:\n%s\n", binariesManifest)
		fmt.Fprintf(os.Stderr, "merge-manifests: Error: %v\n", err)
		os.Exit(1)
	}

	if manifest.GetVersion() == "" {
		fmt.Fprintf(os.Stderr, "merge-manifests: ::error::Binaries manifest is empty\n")
		os.Exit(1)
	}

	// Marshal options with frontend consumption in mind. Ensures all fields are present for predictable structure.
	marshalOpts := protojson.MarshalOptions{
		Multiline:       true,
		Indent:          "  ",
		EmitUnpopulated: true,
	}

	// Merge images if present
	if imagesManifest != "" && imagesManifest != "{}" {
		// Unmarshal images manifest as a map[string]*pb.Image using protojson
		// The JSON format is: { "key": { "ref": "...", "digest": "..." }, ... }
		var imagesMapJSON map[string]json.RawMessage
		if err := json.Unmarshal([]byte(imagesManifest), &imagesMapJSON); err != nil {
			fmt.Fprintf(os.Stderr, "merge-manifests: ::error::Invalid JSON in images_manifest output\n")
			fmt.Fprintf(os.Stderr, "merge-manifests: Raw content:\n%s\n", imagesManifest)
			fmt.Fprintf(os.Stderr, "merge-manifests: Error: %v\n", err)
			os.Exit(1)
		}

		// Convert each JSON value to proto Image message
		images := manifest.GetImages()
		if images == nil {
			images = make(map[string]*pb.Image)
			manifest.SetImages(images)
		}

		unmarshalOpts := protojson.UnmarshalOptions{
			DiscardUnknown: true,
		}
		for key, imageJSON := range imagesMapJSON {
			image := &pb.Image{}
			if err := unmarshalOpts.Unmarshal(imageJSON, image); err != nil {
				fmt.Fprintf(os.Stderr, "merge-manifests: error: unmarshaling image %s: %v\n", key, err)
				os.Exit(1)
			}
			images[key] = image
		}

		// Set manifest-level image attestation descriptor
		// Images use OCI referrers for attestation discovery, so bundle_href is omitted
		attestationType := AttestationTypeInTotoV1
		predicateType := PredicateTypeSLSAProvenanceV1
		manifest.SetImageAttestation(pb.AttestationDescriptor_builder{
			AttestationType: &attestationType,
			PredicateType:   &predicateType,
		}.Build())

		fmt.Fprintf(os.Stderr, "✅ Added %d images to manifest\n", len(images))
	} else {
		fmt.Fprintln(os.Stderr, "ℹ️  No images to add to manifest (docker job may have been skipped if no Dockerfile)")
	}

	// Merge Windows artifacts if present (zip and MSI from goreleaser-windows job)
	// Format is map[string]*pb.Asset (same pattern as images)
	if windowsManifest != "" && windowsManifest != "{}" {
		// Unmarshal Windows manifest as a map[string]json.RawMessage using standard json
		var windowsMapJSON map[string]json.RawMessage
		if err := json.Unmarshal([]byte(windowsManifest), &windowsMapJSON); err != nil {
			fmt.Fprintf(os.Stderr, "merge-manifests: ::error::Invalid JSON in windows_manifest output\n")
			fmt.Fprintf(os.Stderr, "merge-manifests: Raw content:\n%s\n", windowsManifest)
			fmt.Fprintf(os.Stderr, "merge-manifests: Error: %v\n", err)
			os.Exit(1)
		}

		// Get the existing assets map
		assets := manifest.GetAssets()
		if assets == nil {
			assets = make(map[string]*pb.Asset)
			manifest.SetAssets(assets)
		}

		// Convert each JSON value to proto Asset message using protojson
		unmarshalOpts := protojson.UnmarshalOptions{
			DiscardUnknown: true,
		}
		for key, assetJSON := range windowsMapJSON {
			asset := &pb.Asset{}
			if err := unmarshalOpts.Unmarshal(assetJSON, asset); err != nil {
				fmt.Fprintf(os.Stderr, "merge-manifests: error: unmarshaling Windows asset %s: %v\n", key, err)
				os.Exit(1)
			}
			assets[key] = asset
			fmt.Fprintf(os.Stderr, "✅ Added Windows asset: %s\n", key)
		}

		fmt.Fprintf(os.Stderr, "✅ Added %d Windows artifacts to manifest\n", len(windowsMapJSON))
	} else {
		fmt.Fprintln(os.Stderr, "ℹ️  No Windows artifacts to add to manifest")
	}

	// Set manifest-level asset attestation descriptor if any assets have attestations
	hasAssetAttestations := false
	for _, asset := range manifest.GetAssets() {
		if len(asset.GetAttestations()) > 0 {
			hasAssetAttestations = true
			break
		}
	}
	if hasAssetAttestations {
		attestationType := AttestationTypeInTotoV1
		predicateType := PredicateTypeSLSAProvenanceV1
		manifest.SetAssetAttestation(pb.AttestationDescriptor_builder{
			AttestationType: &attestationType,
			PredicateType:   &predicateType,
		}.Build())
		fmt.Fprintln(os.Stderr, "✅ Set asset_attestation descriptor")
	}

	// Marshal to JSON and write to stdout
	jsonBytes, err := marshalOpts.Marshal(manifest)
	if err != nil {
		fmt.Fprintf(os.Stderr, "merge-manifests: error: marshaling merged manifest: %v\n", err)
		os.Exit(1)
	}

	// Write JSON to stdout (progress messages go to stderr)
	fmt.Println(string(jsonBytes))
	fmt.Fprintln(os.Stderr, "✅ Merged manifest complete")
}
