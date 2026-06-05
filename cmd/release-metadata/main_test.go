package main

import (
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
