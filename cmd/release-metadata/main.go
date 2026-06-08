package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

var semverTagPattern = regexp.MustCompile(`^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$`)

type githubRelease struct {
	Body        string `json:"body"`
	PublishedAt string `json:"published_at"`
	CreatedAt   string `json:"created_at"`
}

type releaseMetadata struct {
	ReleasedAt       string
	ReleasedAtSource string
	Changelog        string
	ChangelogSource  string
}

func main() {
	var repoDir, tag, workflowTime, githubReleaseJSON, changelogFile string
	flag.StringVar(&repoDir, "repo-dir", "", "Path to the checked-out release repository")
	flag.StringVar(&tag, "tag", "", "Release tag")
	flag.StringVar(&workflowTime, "workflow-time", "", "Workflow timestamp fallback in RFC3339 format")
	flag.StringVar(&githubReleaseJSON, "github-release-json", "", "Optional GitHub Release JSON file")
	flag.StringVar(&changelogFile, "changelog-file", "", "Path to write the computed changelog")
	flag.Parse()

	if repoDir == "" || tag == "" || workflowTime == "" || changelogFile == "" {
		fmt.Fprintln(os.Stderr, "release-metadata: error: -repo-dir, -tag, -workflow-time, and -changelog-file are required")
		os.Exit(2)
	}

	md, err := computeReleaseMetadata(repoDir, tag, workflowTime, githubReleaseJSON)
	if err != nil {
		fmt.Fprintf(os.Stderr, "release-metadata: error: %v\n", err)
		os.Exit(1)
	}
	if err := os.WriteFile(changelogFile, []byte(md.Changelog), 0o600); err != nil {
		fmt.Fprintf(os.Stderr, "release-metadata: error: write changelog: %v\n", err)
		os.Exit(1)
	}
	writeOutput(os.Stdout, md, changelogFile)
}

func computeReleaseMetadata(repoDir, tag, workflowTime, githubReleaseJSON string) (releaseMetadata, error) {
	workflowReleasedAt, err := normalizeRFC3339(workflowTime)
	if err != nil {
		return releaseMetadata{}, fmt.Errorf("workflow time: %w", err)
	}

	var md releaseMetadata
	if githubReleaseJSON != "" {
		if release, ok := readGitHubRelease(githubReleaseJSON); ok {
			if strings.TrimSpace(release.Body) != "" {
				md.Changelog = release.Body
				md.ChangelogSource = "github-release-body"
			}
			if releasedAt, source := releaseTimestamp(release); releasedAt != "" {
				md.ReleasedAt = releasedAt
				md.ReleasedAtSource = source
			}
		}
	}

	tagMessage, taggerTime := annotatedTagMetadata(repoDir, tag)
	if md.Changelog == "" && strings.TrimSpace(tagMessage) != "" {
		md.Changelog = tagMessage
		md.ChangelogSource = "annotated-tag-message"
	}
	if md.ReleasedAt == "" && taggerTime != "" {
		md.ReleasedAt = taggerTime
		md.ReleasedAtSource = "annotated-tagger-time"
	}

	if md.Changelog == "" {
		changelog, source := generatedChangelog(repoDir, tag)
		if strings.TrimSpace(changelog) != "" {
			md.Changelog = changelog
			md.ChangelogSource = source
		}
	}

	if md.ReleasedAt == "" {
		md.ReleasedAt = workflowReleasedAt
		md.ReleasedAtSource = "workflow-time"
	}
	if md.ChangelogSource == "" {
		md.ChangelogSource = "empty"
	}
	return md, nil
}

func readGitHubRelease(path string) (githubRelease, bool) {
	data, err := os.ReadFile(path)
	if err != nil || len(strings.TrimSpace(string(data))) == 0 {
		return githubRelease{}, false
	}
	var release githubRelease
	if err := json.Unmarshal(data, &release); err != nil {
		return githubRelease{}, false
	}
	if release.Body == "" && release.PublishedAt == "" && release.CreatedAt == "" {
		return githubRelease{}, false
	}
	return release, true
}

func releaseTimestamp(release githubRelease) (string, string) {
	if ts, err := normalizeRFC3339(release.PublishedAt); err == nil && ts != "" {
		return ts, "github-release-published-at"
	}
	if ts, err := normalizeRFC3339(release.CreatedAt); err == nil && ts != "" {
		return ts, "github-release-created-at"
	}
	return "", ""
}

func annotatedTagMetadata(repoDir, tag string) (string, string) {
	ref := "refs/tags/" + tag
	tagType, err := gitOutput(repoDir, "cat-file", "-t", ref)
	if err != nil || strings.TrimSpace(tagType) != "tag" {
		return "", ""
	}
	out, err := gitOutput(repoDir, "for-each-ref", "--format=%(taggerdate:iso-strict)%00%(contents)", ref)
	if err != nil {
		return "", ""
	}
	parts := strings.SplitN(out, "\x00", 2)
	if len(parts) != 2 {
		return strings.TrimSpace(out), ""
	}
	taggerTime := ""
	if ts, err := normalizeRFC3339(strings.TrimSpace(parts[0])); err == nil {
		taggerTime = ts
	}
	return strings.TrimSpace(parts[1]), taggerTime
}

func generatedChangelog(repoDir, tag string) (string, string) {
	currentCommit, err := gitOutput(repoDir, "rev-list", "-n", "1", "refs/tags/"+tag)
	if err != nil {
		return "", "empty"
	}
	tagsOut, err := gitOutput(repoDir, "tag", "--merged", strings.TrimSpace(currentCommit), "--sort=-v:refname")
	if err != nil {
		return "", "empty"
	}

	foundPrevious := false
	for _, candidate := range strings.Fields(tagsOut) {
		if candidate == tag || !semverTagPattern.MatchString(candidate) {
			continue
		}
		foundPrevious = true
		changelog := commitList(repoDir, candidate+".."+tag)
		if strings.TrimSpace(changelog) != "" {
			return changelog, "commit-list:" + candidate + ".." + tag
		}
	}
	if !foundPrevious {
		changelog := commitList(repoDir, tag)
		if strings.TrimSpace(changelog) != "" {
			return changelog, "commit-list:" + tag
		}
	}
	return "", "empty"
}

func commitList(repoDir, rev string) string {
	out, err := gitOutput(repoDir, "log", "--no-merges", "--format=- %s (%h)", rev)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out) + "\n"
}

func normalizeRFC3339(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	t, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return "", err
	}
	return t.UTC().Format(time.RFC3339), nil
}

func gitOutput(repoDir string, args ...string) (string, error) {
	cmdArgs := append([]string{"-C", repoDir}, args...)
	cmd := exec.Command("git", cmdArgs...)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

func writeOutput(out io.Writer, md releaseMetadata, changelogFile string) {
	fmt.Fprintf(out, "released_at=%s\n", md.ReleasedAt)
	fmt.Fprintf(out, "released_at_source=%s\n", md.ReleasedAtSource)
	fmt.Fprintf(out, "changelog_file=%s\n", changelogFile)
	fmt.Fprintf(out, "changelog_source=%s\n", md.ChangelogSource)
}
