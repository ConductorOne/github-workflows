package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const (
	defaultGitHubAPIURL = "https://api.github.com"
	defaultRegistryURL  = "https://dist.conductorone.com/api/v1"
	requestTimeout      = 10 * time.Second
)

type config struct {
	manifestURL  string
	repo         string
	tag          string
	githubAPIURL string
	registryURL  string
	githubToken  string
	commitSHA    string
	outputPath   string
}

func main() {
	cfg := config{
		githubAPIURL: defaultGitHubAPIURL,
		registryURL:  defaultRegistryURL,
	}
	flag.StringVar(&cfg.manifestURL, "manifest-url", "", "Published manifest.json URL on the CDN")
	flag.StringVar(&cfg.repo, "repo", "", "GitHub repository as owner/name")
	flag.StringVar(&cfg.tag, "tag", "", "Release tag")
	flag.StringVar(&cfg.githubAPIURL, "github-api-url", defaultGitHubAPIURL, "GitHub API base URL")
	flag.StringVar(&cfg.registryURL, "registry-url", defaultRegistryURL, "Connector registry API base URL")
	flag.StringVar(&cfg.githubToken, "github-token", "", "GitHub token; defaults to the GITHUB_TOKEN environment variable")
	flag.StringVar(&cfg.commitSHA, "commit-sha", "", "Commit SHA this push is for: the tag's commit, or the annotated tag object the push delivered")
	flag.StringVar(&cfg.outputPath, "output", "", "Optional path to append already_released/reason lines to (e.g. $GITHUB_OUTPUT)")
	flag.Usage = func() {
		fmt.Fprintf(flag.CommandLine.Output(), "Usage: release-published-check -manifest-url URL -repo OWNER/NAME -tag TAG -commit-sha SHA [-github-api-url URL] [-registry-url URL] [-github-token TOKEN] [-output FILE]\n\n")
		fmt.Fprintf(flag.CommandLine.Output(), "Reports whether a release tag is already fully published, so a re-delivered tag push can end the workflow as a no-op instead of failing in build jobs that refuse to overwrite published artifacts.\n\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	if err := cfg.validate(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		flag.Usage()
		os.Exit(2)
	}
	if cfg.githubToken == "" {
		cfg.githubToken = os.Getenv("GITHUB_TOKEN")
	}

	client := &http.Client{Timeout: requestTimeout}
	if _, _, err := run(cfg, client, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func (c config) validate() error {
	var missing []string
	if c.manifestURL == "" {
		missing = append(missing, "-manifest-url")
	}
	if c.repo == "" {
		missing = append(missing, "-repo")
	}
	if c.tag == "" {
		missing = append(missing, "-tag")
	}
	if c.commitSHA == "" {
		missing = append(missing, "-commit-sha")
	}
	if len(missing) > 0 {
		return fmt.Errorf("release-published-check: error: missing required flags: %s", strings.Join(missing, ", "))
	}
	if c.githubAPIURL == "" {
		return fmt.Errorf("release-published-check: error: -github-api-url must not be empty")
	}
	if c.registryURL == "" {
		return fmt.Errorf("release-published-check: error: -registry-url must not be empty")
	}
	return nil
}

// run checks both publication signals and reports the outcome. It only
// returns an error when -output is set but cannot be written; an unverifiable
// publication state is reported through the returned reason, never as an error,
// so the caller falls back to a normal run.
func run(cfg config, client *http.Client, stdout, stderr io.Writer) (bool, string, error) {
	alreadyReleased, reason := checkPublication(cfg, client)
	// Workflow commands and $GITHUB_OUTPUT are both line-based; a wrapped
	// error must not split either.
	reason = oneLine(reason)

	fmt.Fprintln(stdout, reason)
	if alreadyReleased {
		fmt.Fprintf(stdout, "::notice::%s\n", reason)
	}

	if cfg.outputPath != "" {
		if err := appendOutput(cfg.outputPath, alreadyReleased, reason); err != nil {
			return alreadyReleased, reason, err
		}
	}

	return alreadyReleased, reason, nil
}

func checkPublication(cfg config, client *http.Client) (bool, string) {
	manifestPublished, err := checkManifestPublished(client, cfg.manifestURL)
	if err != nil {
		return false, fmt.Sprintf("could not verify publication: %s", err)
	}
	if !manifestPublished {
		return false, "no published manifest yet"
	}

	releasePublished, unpublishedReason, err := checkGitHubRelease(client, cfg)
	if err != nil {
		return false, fmt.Sprintf("could not verify publication: %s", err)
	}
	if !releasePublished {
		return false, unpublishedReason
	}

	tagObject, commit, unresolvedReason, err := resolveTagCommit(client, cfg)
	if err != nil {
		return false, fmt.Sprintf("could not verify publication: %s", err)
	}
	if unresolvedReason != "" {
		return false, unresolvedReason
	}
	if cfg.commitSHA != tagObject && cfg.commitSHA != commit {
		return false, fmt.Sprintf("tag %s now points at %s; this push was for %s", cfg.tag, commit, cfg.commitSHA)
	}

	registryPublished, unpublishedReason, err := checkRegistryRelease(client, cfg, commit)
	if err != nil {
		return false, fmt.Sprintf("could not verify publication: %s", err)
	}
	if !registryPublished {
		return false, unpublishedReason
	}

	return true, fmt.Sprintf("release %s is already published (manifest, GitHub release, and registry record present)", cfg.tag)
}

// checkManifestPublished treats 200 as published and 404 or 403 as not yet
// published (an object-store-backed CDN answers 403 for a missing key); any
// other status or transport error is reported back as an error so the caller
// can fall back to a normal run instead of failing closed.
func checkManifestPublished(client *http.Client, manifestURL string) (bool, error) {
	req, err := http.NewRequest(http.MethodGet, manifestURL, nil)
	if err != nil {
		return false, fmt.Errorf("building manifest request: %w", err)
	}
	req.Header.Set("Cache-Control", "no-cache")

	resp, err := client.Do(req)
	if err != nil {
		return false, fmt.Errorf("fetching manifest: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		return true, nil
	case http.StatusNotFound, http.StatusForbidden:
		return false, nil
	default:
		return false, fmt.Errorf("manifest check returned status %d", resp.StatusCode)
	}
}

// checkGitHubRelease treats a published (non-draft) release as the second
// signal; a draft or missing release reports why, without erroring.
func checkGitHubRelease(client *http.Client, cfg config) (bool, string, error) {
	releaseURL := strings.TrimRight(cfg.githubAPIURL, "/") + "/repos/" + cfg.repo + "/releases/tags/" + cfg.tag

	req, err := http.NewRequest(http.MethodGet, releaseURL, nil)
	if err != nil {
		return false, "", fmt.Errorf("building release request: %w", err)
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	if cfg.githubToken != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.githubToken)
	}

	resp, err := client.Do(req)
	if err != nil {
		return false, "", fmt.Errorf("fetching GitHub release: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		var release struct {
			Draft bool `json:"draft"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&release); err != nil {
			return false, "", fmt.Errorf("parsing GitHub release response: %w", err)
		}
		if release.Draft {
			return false, fmt.Sprintf("GitHub release for %s is still a draft", cfg.tag), nil
		}
		return true, "", nil
	case http.StatusNotFound:
		return false, fmt.Sprintf("no GitHub release found for %s", cfg.tag), nil
	default:
		return false, "", fmt.Errorf("release check returned status %d", resp.StatusCode)
	}
}

// resolveTagCommit peels the tag through the GitHub API, the same way every
// other job in the workflow does, so an annotated tag's object SHA (what
// github.sha delivers) and its underlying commit are both known to the caller.
func resolveTagCommit(client *http.Client, cfg config) (tagObject, commit, reason string, err error) {
	refURL := strings.TrimRight(cfg.githubAPIURL, "/") + "/repos/" + cfg.repo + "/git/ref/tags/" + cfg.tag
	object, reason, err := fetchGitHubObject(client, cfg, refURL)
	if err != nil || reason != "" {
		return "", "", reason, err
	}

	switch object.Type {
	case "commit":
		return object.SHA, object.SHA, "", nil
	case "tag":
		tagURL := strings.TrimRight(cfg.githubAPIURL, "/") + "/repos/" + cfg.repo + "/git/tags/" + object.SHA
		target, reason, err := fetchGitHubObject(client, cfg, tagURL)
		if err != nil || reason != "" {
			return "", "", reason, err
		}
		if target.Type != "commit" {
			return "", "", "", fmt.Errorf("tag %s does not point at a commit", cfg.tag)
		}
		return object.SHA, target.SHA, "", nil
	default:
		return "", "", "", fmt.Errorf("tag %s resolves to unsupported object type %q", cfg.tag, object.Type)
	}
}

// fetchGitHubObject fetches a ref or tag object from the GitHub API; a 404
// reports the given tag as not found instead of erroring, matching
// checkGitHubRelease's not-yet-published-is-not-an-error contract.
func fetchGitHubObject(client *http.Client, cfg config, apiURL string) (object struct {
	Type string `json:"type"`
	SHA  string `json:"sha"`
}, reason string, err error) {
	req, err := http.NewRequest(http.MethodGet, apiURL, nil)
	if err != nil {
		return object, "", fmt.Errorf("building tag request: %w", err)
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	if cfg.githubToken != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.githubToken)
	}

	resp, err := client.Do(req)
	if err != nil {
		return object, "", fmt.Errorf("fetching tag: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		var body struct {
			Object struct {
				Type string `json:"type"`
				SHA  string `json:"sha"`
			} `json:"object"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			return object, "", fmt.Errorf("parsing tag response: %w", err)
		}
		return body.Object, "", nil
	case http.StatusNotFound:
		return object, fmt.Sprintf("no tag %s found", cfg.tag), nil
	default:
		return object, "", fmt.Errorf("tag check returned status %d", resp.StatusCode)
	}
}

// checkRegistryRelease is the completion signal for the phases after artifact
// publication. The registry job runs only after verification succeeds, and
// records the tag commit, so its public version record proves both that the
// release reached the end of the workflow and that it belongs to this push.
func checkRegistryRelease(client *http.Client, cfg config, commit string) (bool, string, error) {
	owner, name, ok := strings.Cut(cfg.repo, "/")
	if !ok || owner == "" || name == "" {
		return false, "", fmt.Errorf("repository %q is not owner/name", cfg.repo)
	}
	releaseURL := strings.TrimRight(cfg.registryURL, "/") + "/connectors/" +
		url.PathEscape(owner) + "/" + url.PathEscape(name) + "/versions/" + url.PathEscape(cfg.tag)

	req, err := http.NewRequest(http.MethodGet, releaseURL, nil)
	if err != nil {
		return false, "", fmt.Errorf("building registry release request: %w", err)
	}
	resp, err := client.Do(req)
	if err != nil {
		return false, "", fmt.Errorf("fetching registry release: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		var result struct {
			Release struct {
				CommitSHA string `json:"commitSha"`
			} `json:"release"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
			return false, "", fmt.Errorf("parsing registry release response: %w", err)
		}
		if result.Release.CommitSHA != commit {
			return false, fmt.Sprintf("registry release for %s records commit %s, not %s",
				cfg.tag, result.Release.CommitSHA, commit), nil
		}
		return true, "", nil
	case http.StatusNotFound:
		return false, fmt.Sprintf("no registry release found for %s", cfg.tag), nil
	default:
		return false, "", fmt.Errorf("registry release check returned status %d", resp.StatusCode)
	}
}

// oneLine collapses all whitespace runs, newlines included, to single spaces.
func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

func appendOutput(path string, alreadyReleased bool, reason string) error {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("release-published-check: error: opening output file %s: %w", path, err)
	}
	defer f.Close()

	if _, err := fmt.Fprintf(f, "already_released=%t\nreason=%s\n", alreadyReleased, oneLine(reason)); err != nil {
		return fmt.Errorf("release-published-check: error: writing output file %s: %w", path, err)
	}
	return nil
}
