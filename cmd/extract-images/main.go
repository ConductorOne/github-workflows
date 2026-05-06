package main

import (
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

const lambdaArm64ImageKey = "lambda-arm64"

func main() {
	var (
		assetDir         string
		digestFile       string
		lambdaAssetDir   string
		lambdaDigestFile string
		repoName         string
		tag              string
		includePublic    bool
		includeLambda    bool
	)
	flag.StringVar(&assetDir, "asset-dir", "../_caller/dist", "Directory containing asset files")
	flag.StringVar(&digestFile, "digest-file", "", "Path to digest file (if not provided, will be constructed from repo-name, tag, and asset-dir)")
	flag.StringVar(&lambdaAssetDir, "lambda-asset-dir", "", "Directory containing Lambda asset files")
	flag.StringVar(&lambdaDigestFile, "lambda-digest-file", "", "Path to Lambda digest file (if not provided, will be constructed from repo-name, tag, and lambda-asset-dir)")
	flag.StringVar(&repoName, "repo-name", "", "Repository name")
	flag.StringVar(&tag, "tag", "", "Release tag (e.g., v0.1.65 or 0.1.65)")
	flag.BoolVar(&includePublic, "include-public", true, "Extract ECR public image metadata")
	flag.BoolVar(&includeLambda, "include-lambda", false, "Extract private Lambda image metadata")
	flag.Parse()

	if tag == "" {
		fmt.Fprintf(os.Stderr, "extract-images: error: tag is required\n")
		os.Exit(1)
	}
	if !includePublic && !includeLambda {
		fmt.Fprintf(os.Stderr, "extract-images: error: at least one of include-public or include-lambda must be true\n")
		os.Exit(1)
	}

	// Remove 'v' prefix if present to get version for matching image refs and file names
	version := strings.TrimPrefix(tag, "v")

	images := make(map[string]*pb.Image)

	if includePublic {
		// Construct digest file path if not provided
		if digestFile == "" {
			if repoName == "" {
				fmt.Fprintf(os.Stderr, "extract-images: error: either digest-file or repo-name must be provided\n")
				os.Exit(1)
			}
			digestFile = digestPath(assetDir, repoName, version)
		}

		content, err := os.ReadFile(digestFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "extract-images: ::error::Digest file not found: %s\n", digestFile)
			os.Exit(1)
		}

		foundECR := extractPublicImages(content, version, images)
		if !foundECR {
			fmt.Fprintf(os.Stderr, "extract-images: ::error::Could not find ECR public index image in %s\n", digestFile)
			fmt.Fprintf(os.Stderr, "extract-images: Contents of digest file:\n%s\n", content)
			os.Exit(1)
		}
	}

	if includeLambda {
		if repoName == "" {
			fmt.Fprintf(os.Stderr, "extract-images: error: repo-name is required for Lambda image extraction\n")
			os.Exit(1)
		}
		if lambdaAssetDir == "" {
			lambdaAssetDir = assetDir
		}
		if lambdaDigestFile == "" {
			lambdaDigestFile = digestPath(lambdaAssetDir, repoName, version)
		}

		content, err := os.ReadFile(lambdaDigestFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "extract-images: ::error::Lambda digest file not found: %s\n", lambdaDigestFile)
			os.Exit(1)
		}
		if !extractLambdaImage(content, repoName, version, images) {
			fmt.Fprintf(os.Stderr, "extract-images: ::error::Could not find Lambda arm64 image in %s\n", lambdaDigestFile)
			fmt.Fprintf(os.Stderr, "extract-images: Contents of digest file:\n%s\n", content)
			os.Exit(1)
		}
	}

	imagesJSON, err := marshalImages(images)
	if err != nil {
		fmt.Fprintf(os.Stderr, "extract-images: error: marshaling images: %v\n", err)
		os.Exit(1)
	}

	// Write JSON to stdout (progress messages go to stderr)
	fmt.Println(imagesJSON)
	fmt.Fprintln(os.Stderr, "✅ Extracted image digests")
}

func digestPath(assetDir, repoName, version string) string {
	return fmt.Sprintf("%s/%s_%s_digests.txt", assetDir, repoName, version)
}

func extractPublicImages(content []byte, version string, images map[string]*pb.Image) bool {
	var foundECR bool
	for _, line := range parseDigestLines(content) {
		// Only capture the multi-arch index images (tagged with just version, not version-arch)
		if !strings.HasSuffix(line.ref, fmt.Sprintf(":%s", version)) {
			continue
		}

		uri, ok := digestPinnedURI(line.ref, line.digest)
		if !ok {
			continue
		}

		if strings.HasPrefix(line.ref, "public.ecr.aws/conductorone/") {
			isIndex := true
			images["ecrPublic"] = pb.Image_builder{
				Ref:     &line.ref,
				Digest:  &line.digest,
				Tag:     &version,
				Uri:     &uri,
				IsIndex: &isIndex,
			}.Build()
			foundECR = true
		}
	}

	return foundECR
}

func extractLambdaImage(content []byte, repoName, version string, images map[string]*pb.Image) bool {
	tag := fmt.Sprintf("%s-arm64", version)
	for _, line := range parseDigestLines(content) {
		if !strings.HasSuffix(line.ref, fmt.Sprintf(":%s", tag)) {
			continue
		}

		ref := fmt.Sprintf("%s:%s", repoName, tag)
		uri := fmt.Sprintf("%s@%s", repoName, line.digest)
		isIndex := false
		images[lambdaArm64ImageKey] = pb.Image_builder{
			Ref:     &ref,
			Digest:  &line.digest,
			Tag:     &tag,
			Uri:     &uri,
			IsIndex: &isIndex,
		}.Build()
		return true
	}

	return false
}

type digestLine struct {
	digest string
	ref    string
}

func parseDigestLines(content []byte) []digestLine {
	var lines []digestLine
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

		lines = append(lines, digestLine{
			digest: normalizeDigest(parts[0]),
			ref:    parts[1],
		})
	}
	return lines
}

func normalizeDigest(raw string) string {
	if strings.HasPrefix(raw, "sha256:") {
		return raw
	}
	return fmt.Sprintf("sha256:%s", raw)
}

func digestPinnedURI(ref, digest string) (string, bool) {
	i := strings.LastIndex(ref, ":")
	if i < 0 {
		return "", false
	}
	return fmt.Sprintf("%s@%s", ref[:i], digest), true
}

func marshalImages(images map[string]*pb.Image) (string, error) {
	opts := protojson.MarshalOptions{
		Multiline:       true,
		Indent:          "  ",
		EmitUnpopulated: true,
	}

	imagesJSONParts := []string{"{"}
	keys := make([]string, 0, len(images))
	for key := range images {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	first := true
	for _, key := range keys {
		if !first {
			imagesJSONParts = append(imagesJSONParts, ",")
		}
		first = false
		imageJSON, err := opts.Marshal(images[key])
		if err != nil {
			return "", err
		}
		imagesJSONParts = append(imagesJSONParts, fmt.Sprintf("  %q: %s", key, string(imageJSON)))
	}
	imagesJSONParts = append(imagesJSONParts, "}")
	return strings.Join(imagesJSONParts, "\n"), nil
}
