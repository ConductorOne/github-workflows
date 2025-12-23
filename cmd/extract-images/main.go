package main

import (
	"flag"
	"fmt"
	"os"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func main() {
	var (
		assetDir   string
		digestFile string
		repoName   string
		tag        string
	)
	flag.StringVar(&assetDir, "asset-dir", "../_caller/dist", "Directory containing asset files")
	flag.StringVar(&digestFile, "digest-file", "", "Path to digest file (if not provided, will be constructed from repo-name, tag, and asset-dir)")
	flag.StringVar(&repoName, "repo-name", "", "Repository name")
	flag.StringVar(&tag, "tag", "", "Release tag (e.g., v0.1.65 or 0.1.65)")
	flag.Parse()

	if tag == "" {
		fmt.Fprintf(os.Stderr, "extract-images: error: tag is required\n")
		os.Exit(1)
	}

	// Remove 'v' prefix if present to get version for matching image refs and file names
	version := strings.TrimPrefix(tag, "v")

	// Construct digest file path if not provided
	if digestFile == "" {
		if repoName == "" {
			fmt.Fprintf(os.Stderr, "extract-images: error: either digest-file or repo-name must be provided\n")
			os.Exit(1)
		}
		digestFile = fmt.Sprintf("%s/%s_%s_digests.txt", assetDir, repoName, version)
	}

	content, err := os.ReadFile(digestFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "extract-images: ::error::Digest file not found: %s\n", digestFile)
		os.Exit(1)
	}

	images := make(map[string]*pb.Image)
	var foundGHCR, foundECR bool

	for _, line := range strings.Split(string(content), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}

		// docker_digest prints "<digest>  <ref>"
		parts := strings.Fields(line)
		if len(parts) != 2 {
			continue
		}

		digestHex, ref := parts[0], parts[1]
		digest := fmt.Sprintf("sha256:%s", digestHex)

		// Only capture the multi-arch index images (tagged with just version, not version-arch)
		if !strings.HasSuffix(ref, fmt.Sprintf(":%s", version)) {
			continue
		}

		// Build the canonical digest-pinned URI
		// ref is like "ghcr.io/conductorone/baton-ukg:0.1.98"
		// uri should be "ghcr.io/conductorone/baton-ukg@sha256:abc123..."
		refParts := strings.Split(ref, ":")
		if len(refParts) < 2 {
			continue
		}
		imageBase := refParts[0]
		uri := fmt.Sprintf("%s@%s", imageBase, digest)

		if strings.HasPrefix(ref, "ghcr.io/conductorone/") {
			isIndex := true
			images["ghcr"] = pb.Image_builder{
				Ref:     &ref,
				Digest:  &digest,
				Tag:     &version,
				Uri:     &uri,
				IsIndex: &isIndex,
			}.Build()
			foundGHCR = true
		} else if strings.HasPrefix(ref, "public.ecr.aws/conductorone/") {
			isIndex := true
			images["ecrPublic"] = pb.Image_builder{
				Ref:     &ref,
				Digest:  &digest,
				Tag:     &version,
				Uri:     &uri,
				IsIndex: &isIndex,
			}.Build()
			foundECR = true
		}
	}

	if !foundGHCR && !foundECR {
		fmt.Fprintf(os.Stderr, "extract-images: ::error::Could not find GHCR or ECR public index image in %s\n", digestFile)
		fmt.Fprintf(os.Stderr, "extract-images: Contents of digest file:\n%s\n", content)
		os.Exit(1)
	}
	if !foundGHCR {
		fmt.Fprintf(os.Stderr, "extract-images: ::warning::Could not find GHCR index image in %s\n", digestFile)
	}
	if !foundECR {
		fmt.Fprintf(os.Stderr, "extract-images: ::warning::Could not find ECR public index image in %s\n", digestFile)
	}

	// Marshal images map to JSON using protojson
	// Marshal each image individually and build the JSON object
	// Marshal options with frontend consumption in mind. Ensures all fields are present for predictable structure.
	opts := protojson.MarshalOptions{
		Multiline:       true,
		Indent:          "  ",
		EmitUnpopulated: true,
	}

	// Marshal each image individually and build the JSON object.
	// protojson.Marshal only works on proto.Message, not on Go maps, so we construct the JSON manually.
	imagesJSONParts := []string{"{"}
	first := true
	for key, image := range images {
		if !first {
			imagesJSONParts = append(imagesJSONParts, ",")
		}
		first = false
		imageJSON, err := opts.Marshal(image)
		if err != nil {
			fmt.Fprintf(os.Stderr, "extract-images: error: marshaling image: %v\n", err)
			os.Exit(1)
		}
		imagesJSONParts = append(imagesJSONParts, fmt.Sprintf("  %q: %s", key, string(imageJSON)))
	}
	imagesJSONParts = append(imagesJSONParts, "}")
	imagesJSON := strings.Join(imagesJSONParts, "\n")

	// Write JSON to stdout (progress messages go to stderr)
	fmt.Println(imagesJSON)
	fmt.Fprintln(os.Stderr, "✅ Extracted image digests")
}
