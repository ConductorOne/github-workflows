package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"strings"
)

const defaultRegistryURI = "public.ecr.aws/conductorone"

var sha256DigestPattern = regexp.MustCompile(`^(?:sha256:)?([A-Fa-f0-9]{64})$`)

type config struct {
	repositoryName string
	versionTag     string
	candidateTag   string
	digestFile     string
	registryURI    string
}

type awsRunner interface {
	Run(args ...string) ([]byte, []byte, error)
}

type execAWSRunner struct {
	binary string
}

func (r execAWSRunner) Run(args ...string) ([]byte, []byte, error) {
	var stderr bytes.Buffer
	cmd := exec.Command(r.binary, args...)
	cmd.Stderr = &stderr
	stdout, err := cmd.Output()
	return stdout, stderr.Bytes(), err
}

func main() {
	cfg := config{
		registryURI: defaultRegistryURI,
	}
	flag.StringVar(&cfg.repositoryName, "repository-name", "", "Public ECR repository name")
	flag.StringVar(&cfg.versionTag, "version-tag", "", "Immutable release version tag")
	flag.StringVar(&cfg.candidateTag, "candidate-tag", "", "Temporary candidate image tag pushed by GoReleaser")
	flag.StringVar(&cfg.digestFile, "digest-file", "", "Path to the GoReleaser docker_digest file")
	flag.StringVar(&cfg.registryURI, "registry-uri", defaultRegistryURI, "Public ECR registry URI")
	flag.Usage = func() {
		fmt.Fprintf(flag.CommandLine.Output(), "Usage: publish-public-ecr-release-tags -repository-name REPO -version-tag TAG -candidate-tag TAG -digest-file FILE [-registry-uri URI]\n\n")
		fmt.Fprintf(flag.CommandLine.Output(), "Promotes a pushed candidate Public ECR image to the immutable release version tag after checking any existing version tag digest. The latest tag is mutable convenience metadata and is not consulted by the preflight.\n\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	if err := cfg.validate(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		flag.Usage()
		os.Exit(2)
	}

	awsCLI := os.Getenv("AWS_CLI")
	if awsCLI == "" {
		awsCLI = "aws"
	}

	if err := publish(cfg, execAWSRunner{binary: awsCLI}, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func (c config) validate() error {
	var missing []string
	if c.repositoryName == "" {
		missing = append(missing, "-repository-name")
	}
	if c.versionTag == "" {
		missing = append(missing, "-version-tag")
	}
	if c.candidateTag == "" {
		missing = append(missing, "-candidate-tag")
	}
	if c.digestFile == "" {
		missing = append(missing, "-digest-file")
	}
	if len(missing) > 0 {
		return fmt.Errorf("publish-public-ecr-release-tags: error: missing required flags: %s", strings.Join(missing, ", "))
	}
	if c.registryURI == "" {
		return fmt.Errorf("publish-public-ecr-release-tags: error: -registry-uri must not be empty")
	}
	return nil
}

func publish(cfg config, aws awsRunner, stdout, stderr io.Writer) error {
	imageBase := strings.TrimRight(cfg.registryURI, "/") + "/" + cfg.repositoryName
	candidateRef := imageBase + ":" + cfg.candidateTag
	versionRef := imageBase + ":" + cfg.versionTag

	candidateDigest, err := candidateDigestFromFile(cfg.digestFile, candidateRef)
	if err != nil {
		return err
	}

	existingDigest, err := describeImageDigest(aws, cfg.repositoryName, cfg.versionTag)
	if err != nil {
		return err
	}
	versionTagExists := existingDigest != ""

	if versionTagExists {
		if existingDigest != candidateDigest {
			return fmt.Errorf("::error::Public ECR tag %s:%s already points at %s, refusing to replace it with %s", cfg.repositoryName, cfg.versionTag, existingDigest, candidateDigest)
		}
		fmt.Fprintf(stdout, "Public ECR version tag already points at %s; keeping release idempotent\n", candidateDigest)
	} else {
		fmt.Fprintf(stdout, "Public ECR version tag %s:%s is available\n", cfg.repositoryName, cfg.versionTag)
	}

	manifest, err := imageManifest(aws, cfg.repositoryName, candidateDigest)
	if err != nil {
		return err
	}

	if !versionTagExists {
		if err := putImageTag(aws, cfg.repositoryName, manifest, cfg.versionTag); err != nil {
			return err
		}
	} else {
		fmt.Fprintf(stdout, "Skipped Public ECR version tag write because %s:%s already has %s\n", cfg.repositoryName, cfg.versionTag, candidateDigest)
	}

	publishedDigest, err := describeImageDigest(aws, cfg.repositoryName, cfg.versionTag)
	if err != nil {
		return err
	}
	if publishedDigest == "" {
		return fmt.Errorf("::error::Public ECR tag %s:%s was not found after publication", cfg.repositoryName, cfg.versionTag)
	}
	if publishedDigest != candidateDigest {
		return fmt.Errorf("::error::Public ECR tag %s:%s points at %s after publication, expected %s", cfg.repositoryName, cfg.versionTag, publishedDigest, candidateDigest)
	}

	if err := putImageTag(aws, cfg.repositoryName, manifest, "latest"); err != nil {
		return err
	}

	if err := os.WriteFile(cfg.digestFile, []byte(fmt.Sprintf("%s  %s\n", candidateDigest, versionRef)), 0o644); err != nil {
		return fmt.Errorf("publish-public-ecr-release-tags: error: rewriting digest file: %w", err)
	}

	if err := deleteImageTag(aws, cfg.repositoryName, cfg.candidateTag); err != nil {
		fmt.Fprintf(stderr, "::warning::Could not remove temporary Public ECR candidate tag %s: %v\n", cfg.candidateTag, err)
	} else {
		fmt.Fprintf(stdout, "Removed temporary Public ECR candidate tag %s\n", cfg.candidateTag)
	}

	fmt.Fprintf(stdout, "Published %s at %s\n", versionRef, candidateDigest)
	return nil
}

func candidateDigestFromFile(path, candidateRef string) (string, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("Digest file not found: %s", path)
		}
		return "", fmt.Errorf("publish-public-ecr-release-tags: error: reading digest file %s: %w", path, err)
	}

	for _, line := range strings.Split(string(content), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 || fields[1] != candidateRef {
			continue
		}
		digest, err := normalizeDigest(fields[0])
		if err != nil {
			return "", fmt.Errorf("publish-public-ecr-release-tags: error: candidate digest for %s in %s is invalid: %w", candidateRef, path, err)
		}
		return digest, nil
	}

	return "", fmt.Errorf("Could not find candidate ref %s in %s\n%s", candidateRef, path, content)
}

func describeImageDigest(aws awsRunner, repositoryName, tag string) (string, error) {
	args := []string{
		"ecr-public", "describe-images",
		"--repository-name", repositoryName,
		"--image-ids", "imageTag=" + tag,
		"--output", "json",
	}
	stdout, stderr, err := aws.Run(args...)
	if err != nil {
		if isImageNotFound(stderr) {
			return "", nil
		}
		return "", awsError(args, stderr, err)
	}

	var response struct {
		ImageDetails []struct {
			ImageDigest string `json:"imageDigest"`
		} `json:"imageDetails"`
	}
	if err := json.Unmarshal(stdout, &response); err != nil {
		return "", fmt.Errorf("publish-public-ecr-release-tags: error: parsing describe-images response for %s:%s: %w", repositoryName, tag, err)
	}
	if len(response.ImageDetails) == 0 || response.ImageDetails[0].ImageDigest == "" {
		return "", nil
	}

	digest, err := normalizeDigest(response.ImageDetails[0].ImageDigest)
	if err != nil {
		return "", fmt.Errorf("publish-public-ecr-release-tags: error: describe-images returned invalid digest for %s:%s: %w", repositoryName, tag, err)
	}
	return digest, nil
}

func imageManifest(aws awsRunner, repositoryName, digest string) (string, error) {
	args := []string{
		"ecr-public", "batch-get-image",
		"--repository-name", repositoryName,
		"--image-ids", "imageDigest=" + digest,
		"--accepted-media-types",
		"application/vnd.oci.image.index.v1+json",
		"application/vnd.docker.distribution.manifest.list.v2+json",
		"application/vnd.oci.image.manifest.v1+json",
		"application/vnd.docker.distribution.manifest.v2+json",
		"--output", "json",
	}
	stdout, stderr, err := aws.Run(args...)
	if err != nil {
		return "", awsError(args, stderr, err)
	}

	var response struct {
		Images []struct {
			ImageManifest string `json:"imageManifest"`
		} `json:"images"`
	}
	if err := json.Unmarshal(stdout, &response); err != nil {
		return "", fmt.Errorf("publish-public-ecr-release-tags: error: parsing batch-get-image response for %s@%s: %w", repositoryName, digest, err)
	}
	if len(response.Images) == 0 || response.Images[0].ImageManifest == "" {
		return "", fmt.Errorf("Could not fetch manifest for %s@%s\n%s", repositoryName, digest, stdout)
	}
	return response.Images[0].ImageManifest, nil
}

func putImageTag(aws awsRunner, repositoryName, manifest, tag string) error {
	args := []string{
		"ecr-public", "put-image",
		"--repository-name", repositoryName,
		"--image-manifest", manifest,
		"--image-tag", tag,
	}
	_, stderr, err := aws.Run(args...)
	if err != nil {
		return awsError(args, stderr, err)
	}
	return nil
}

func deleteImageTag(aws awsRunner, repositoryName, tag string) error {
	args := []string{
		"ecr-public", "batch-delete-image",
		"--repository-name", repositoryName,
		"--image-ids", "imageTag=" + tag,
	}
	_, stderr, err := aws.Run(args...)
	if err != nil {
		return awsError(args, stderr, err)
	}
	return nil
}

func normalizeDigest(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	matches := sha256DigestPattern.FindStringSubmatch(raw)
	if matches == nil {
		return "", fmt.Errorf("expected sha256 digest, got %q", raw)
	}
	return "sha256:" + strings.ToLower(matches[1]), nil
}

func isImageNotFound(stderr []byte) bool {
	message := string(stderr)
	return strings.Contains(message, "ImageNotFound") ||
		strings.Contains(message, "ImageNotFoundException") ||
		strings.Contains(message, "RepositoryNotFound") ||
		strings.Contains(message, "RepositoryNotFoundException")
}

func awsError(args []string, stderr []byte, err error) error {
	message := strings.TrimSpace(string(stderr))
	if message == "" {
		return fmt.Errorf("publish-public-ecr-release-tags: error: aws %s: %w", strings.Join(args, " "), err)
	}
	return fmt.Errorf("publish-public-ecr-release-tags: error: aws %s: %s: %w", strings.Join(args, " "), message, err)
}
