package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func main() {
	var (
		binariesManifest string
		imagesManifest   string
		outputFile       string
	)
	flag.StringVar(&binariesManifest, "binaries-manifest", "", "JSON string of binaries manifest")
	flag.StringVar(&imagesManifest, "images-manifest", "", "JSON string of images manifest (optional)")
	flag.StringVar(&outputFile, "output", "manifest.json", "Output file path")
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

		fmt.Println("✅ Added images to manifest:")
		for key, image := range images {
			imageJSON, _ := marshalOpts.Marshal(image)
			fmt.Printf("  %s: %s\n", key, string(imageJSON))
		}
	} else {
		fmt.Println("ℹ️  No images to add to manifest (docker job may have been skipped if no Dockerfile)")
	}

	// Marshal to JSON
	jsonBytes, err := marshalOpts.Marshal(manifest)
	if err != nil {
		fmt.Fprintf(os.Stderr, "merge-manifests: error: marshaling merged manifest: %v\n", err)
		os.Exit(1)
	}

	// Write to output file
	if err := os.WriteFile(outputFile, jsonBytes, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "merge-manifests: error: writing manifest file: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("✅ Merged manifest written to: %s\n", outputFile)
}
