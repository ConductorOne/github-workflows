package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestGitHubReleaseMetadataWins(t *testing.T) {
	repo := initRepo(t)
	commitFile(t, repo, "connector.go", "package main\n", "initial")
	runGit(t, repo, "tag", "-a", "v1.0.0", "-m", "annotated notes")
	releaseJSON := filepath.Join(t.TempDir(), "release.json")
	if err := os.WriteFile(releaseJSON, []byte(`{"body":"GitHub release notes\n","published_at":"2026-06-05T14:54:03Z","created_at":"2026-06-05T14:00:00Z"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	md, err := computeReleaseMetadata(repo, "v1.0.0", "2026-06-05T15:00:00Z", releaseJSON)
	if err != nil {
		t.Fatal(err)
	}
	if md.Changelog != "GitHub release notes\n" || md.ChangelogSource != "github-release-body" {
		t.Fatalf("changelog = %q from %s", md.Changelog, md.ChangelogSource)
	}
	if md.ReleasedAt != "2026-06-05T14:54:03Z" || md.ReleasedAtSource != "github-release-published-at" {
		t.Fatalf("releasedAt = %q from %s", md.ReleasedAt, md.ReleasedAtSource)
	}
}

func TestAnnotatedTagFallback(t *testing.T) {
	repo := initRepo(t)
	commitFile(t, repo, "connector.go", "package main\n", "initial")
	runGitWithEnv(t, repo, []string{"GIT_COMMITTER_DATE=2026-06-04T12:34:56Z"}, "tag", "-a", "v1.0.0", "-m", "annotated notes")

	md, err := computeReleaseMetadata(repo, "v1.0.0", "2026-06-05T15:00:00Z", "")
	if err != nil {
		t.Fatal(err)
	}
	if md.Changelog != "annotated notes" || md.ChangelogSource != "annotated-tag-message" {
		t.Fatalf("changelog = %q from %s", md.Changelog, md.ChangelogSource)
	}
	if md.ReleasedAt != "2026-06-04T12:34:56Z" || md.ReleasedAtSource != "annotated-tagger-time" {
		t.Fatalf("releasedAt = %q from %s", md.ReleasedAt, md.ReleasedAtSource)
	}
}

func TestGeneratedCommitListFallback(t *testing.T) {
	repo := initRepo(t)
	commitFile(t, repo, "connector.go", "package main\n", "initial")
	runGit(t, repo, "tag", "v1.0.0")
	commitFile(t, repo, "connector.go", "package main\n// second\n", "second change")
	runGit(t, repo, "tag", "v1.1.0")

	md, err := computeReleaseMetadata(repo, "v1.1.0", "2026-06-05T15:00:00Z", "")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(md.Changelog, "- second change") || md.ChangelogSource != "commit-list:v1.0.0..v1.1.0" {
		t.Fatalf("changelog = %q from %s", md.Changelog, md.ChangelogSource)
	}
	if md.ReleasedAt != "2026-06-05T15:00:00Z" || md.ReleasedAtSource != "workflow-time" {
		t.Fatalf("releasedAt = %q from %s", md.ReleasedAt, md.ReleasedAtSource)
	}
}

// TestGeneratedCommitListCollapsesRepeats: the generated fallback folds
// commits that share a subject into one counted line, at the seam the
// workflow uses.
func TestGeneratedCommitListCollapsesRepeats(t *testing.T) {
	repo := initRepo(t)
	commitFile(t, repo, "connector.go", "package main\n", "initial")
	runGit(t, repo, "tag", "v1.0.0")
	commitFile(t, repo, "go.mod", "module example\n\ngo 1.25\n", "chore: update dependency & Go versions")
	commitFile(t, repo, "connector.go", "package main\n// second\n", "second change")
	commitFile(t, repo, "go.mod", "module example\n\ngo 1.25.2\n", "chore: update dependency & Go versions")
	runGit(t, repo, "tag", "v1.1.0")

	md, err := computeReleaseMetadata(repo, "v1.1.0", "2026-06-05T15:00:00Z", "")
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(md.Changelog), "\n")
	if len(lines) != 2 {
		t.Fatalf("changelog has %d lines, want 2:\n%s", len(lines), md.Changelog)
	}
	if !strings.HasPrefix(lines[0], "- chore: update dependency & Go versions (2 commits, first listed ") {
		t.Fatalf("first line = %q", lines[0])
	}
	if !strings.HasPrefix(lines[1], "- second change (") {
		t.Fatalf("second line = %q", lines[1])
	}
}

// TestGitHubReleaseBodyCollapsesRepeats: a goreleaser release body made of
// one subject repeated eighty times comes back as one counted line under
// the heading, with the source still reported as the release body.
func TestGitHubReleaseBodyCollapsesRepeats(t *testing.T) {
	repo := initRepo(t)
	commitFile(t, repo, "connector.go", "package main\n", "initial")
	runGit(t, repo, "tag", "v1.0.0")
	var body strings.Builder
	body.WriteString("## Changelog\n")
	for i := range 80 {
		fmt.Fprintf(&body, "* %040x chore: update dependency & Go versions\n", i)
	}
	release, err := json.Marshal(map[string]string{"body": body.String(), "published_at": "2026-06-05T14:54:03Z"})
	if err != nil {
		t.Fatal(err)
	}
	releaseJSON := filepath.Join(t.TempDir(), "release.json")
	if err := os.WriteFile(releaseJSON, release, 0o600); err != nil {
		t.Fatal(err)
	}

	md, err := computeReleaseMetadata(repo, "v1.0.0", "2026-06-05T15:00:00Z", releaseJSON)
	if err != nil {
		t.Fatal(err)
	}
	want := "## Changelog\n* chore: update dependency & Go versions (80 commits, first listed " + fmt.Sprintf("%040x", 0) + ")\n"
	if md.Changelog != want || md.ChangelogSource != "github-release-body" {
		t.Fatalf("changelog = %q from %s", md.Changelog, md.ChangelogSource)
	}
}

// TestCollapseRepeatedCommitsFoldsGoreleaserBody: repeated subjects fold into
// the position of their first appearance, keeping that line's SHA; one-off
// entries and the heading are untouched.
func TestCollapseRepeatedCommitsFoldsGoreleaserBody(t *testing.T) {
	sha := func(i int) string { return fmt.Sprintf("%040x", i) }
	body := "## Changelog\n" +
		"* " + sha(1) + " Update vulnerable dependencies (auto-release)\n" +
		"* " + sha(2) + " chore: update version files via baton-admin\n" +
		"* " + sha(3) + " chore: update dependency & Go versions\n" +
		"* " + sha(4) + " chore: update version files via baton-admin\n" +
		"* " + sha(5) + " Migrate to DefineConfigurationV2 / RunConnector (#17)\n" +
		"* " + sha(6) + " chore: update dependency & Go versions\n" +
		"* " + sha(7) + " chore: update version files via baton-admin\n"
	want := "## Changelog\n" +
		"* " + sha(1) + " Update vulnerable dependencies (auto-release)\n" +
		"* chore: update version files via baton-admin (3 commits, first listed " + sha(2) + ")\n" +
		"* chore: update dependency & Go versions (2 commits, first listed " + sha(3) + ")\n" +
		"* " + sha(5) + " Migrate to DefineConfigurationV2 / RunConnector (#17)\n"
	got, folded := collapseRepeatedCommits(body)
	if got != want {
		t.Fatalf("collapsed changelog:\n%s\nwant:\n%s", got, want)
	}
	if folded != 3 {
		t.Fatalf("folded = %d, want 3", folded)
	}
}

// TestCollapseRepeatedCommitsFoldsGeneratedList: the "- subject (sha)" shape
// folds the same way, keeping its bullet.
func TestCollapseRepeatedCommitsFoldsGeneratedList(t *testing.T) {
	list := "- chore: update dependency & Go versions (abc1234)\n" +
		"- second change (def5678)\n" +
		"- chore: update dependency & Go versions (0123abc)\n"
	want := "- chore: update dependency & Go versions (2 commits, first listed abc1234)\n" +
		"- second change (def5678)\n"
	got, folded := collapseRepeatedCommits(list)
	if got != want {
		t.Fatalf("collapsed list:\n%s\nwant:\n%s", got, want)
	}
	if folded != 1 {
		t.Fatalf("folded = %d, want 1", folded)
	}
}

// TestCollapseRepeatedCommitsLeavesUniqueAndProseAlone: nothing to fold means
// the text comes back byte for byte, including prose that happens to repeat.
func TestCollapseRepeatedCommitsLeavesUniqueAndProseAlone(t *testing.T) {
	for _, body := range []string{
		"",
		"## Changelog\n* " + fmt.Sprintf("%040x", 1) + " one\n* " + fmt.Sprintf("%040x", 2) + " two\n",
		"Release notes\n\nSame line\nSame line\n",
		"annotated notes",
	} {
		got, folded := collapseRepeatedCommits(body)
		if got != body || folded != 0 {
			t.Fatalf("changelog %q changed to %q (folded %d)", body, got, folded)
		}
	}
}

func TestCollapseRepeatedCommitsKeepsHeadingSectionsSeparate(t *testing.T) {
	for _, heading := range []string{"## Earlier changes", "Earlier changes\n---------------", "Earlier changes\n==============="} {
		body := "## Recent changes\n* aaaaaaa update dependencies\n" + heading + "\n* bbbbbbb update dependencies\n"
		got, folded := collapseRepeatedCommits(body)
		if got != body || folded != 0 {
			t.Fatalf("merged entries across heading %q: folded %d, got %q", heading, folded, got)
		}
	}
}

func initRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	runGit(t, dir, "init", "-b", "main")
	runGit(t, dir, "config", "user.email", "test@example.com")
	runGit(t, dir, "config", "user.name", "Test User")
	return dir
}

func commitFile(t *testing.T, repo, name, contents, message string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(repo, name), []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	runGit(t, repo, "add", name)
	runGit(t, repo, "commit", "-m", message)
}

func runGit(t *testing.T, repo string, args ...string) {
	t.Helper()
	runGitWithEnv(t, repo, nil, args...)
}

func runGitWithEnv(t *testing.T, repo string, env []string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = repo
	cmd.Env = append(os.Environ(), env...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
}
