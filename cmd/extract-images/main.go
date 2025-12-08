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
		githubOut  string
	)
	flag.StringVar(&assetDir, "asset-dir", "../_caller/dist", "Directory containing asset files")
	flag.StringVar(&digestFile, "digest-file", "", "Path to digest file (if not provided, will be constructed from repo-name, tag, and asset-dir)")
	flag.StringVar(&repoName, "repo-name", "", "Repository name")
	flag.StringVar(&tag, "tag", "", "Release tag (e.g., v0.1.65 or 0.1.65)")
	flag.StringVar(&githubOut, "github-output", "", "Path to GITHUB_OUTPUT file")
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
	foundIndex := false

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

		// We only care about GHCR; skip ECR lines
		if !strings.HasPrefix(ref, "ghcr.io/conductorone/") {
			continue
		}

		digest := fmt.Sprintf("sha256:%s", digestHex)

		// multi-arch image
		if strings.HasSuffix(ref, fmt.Sprintf(":%s", version)) {
			image := &pb.Image{}
			image.SetRef(ref)
			image.SetDigest(digest)
			images["index"] = image
			foundIndex = true
		} else if strings.HasSuffix(ref, fmt.Sprintf(":%s-amd64", version)) {
			image := &pb.Image{}
			image.SetRef(ref)
			image.SetDigest(digest)
			images["linux-amd64"] = image
		} else if strings.HasSuffix(ref, fmt.Sprintf(":%s-arm64", version)) {
			image := &pb.Image{}
			image.SetRef(ref)
			image.SetDigest(digest)
			images["linux-arm64"] = image
		}
	}

	if !foundIndex {
		fmt.Fprintf(os.Stderr, "extract-images: ::error::Could not find GHCR index line in %s\n", digestFile)
		fmt.Fprintf(os.Stderr, "extract-images: Contents of digest file:\n%s\n", content)
		os.Exit(1)
	}

	// Marshal images map to JSON using protojson
	// Marshal each image individually and build the JSON object
	// Marshal options with frontend consumption in mind. Ensures all fields are present for predictable structure.
	opts := protojson.MarshalOptions{
		Multiline:       true,
		Indent:          "  ",
		EmitUnpopulated: true,
	}

	// Marshal each image individually and build the JSON object
	// Since protojson doesn't directly support map[string]*Image, we'll construct it manually
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
	imagesJSON := []byte(strings.Join(imagesJSONParts, "\n"))

	// If github-output is set, write to GITHUB_OUTPUT
	if githubOut != "" {
		f, err := os.OpenFile(githubOut, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "extract-images: error: opening GITHUB_OUTPUT: %v\n", err)
			os.Exit(1)
		}
		defer f.Close()

		fmt.Fprintf(f, "images_manifest<<EOF\n")
		f.Write(imagesJSON)
		fmt.Fprintf(f, "\nEOF\n")
	}

	fmt.Println("✅ Extracted image digests:")
	fmt.Println(string(imagesJSON))
}
