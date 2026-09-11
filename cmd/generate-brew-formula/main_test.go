package main

import (
	"strings"
	"testing"

	pb "github.com/ConductorOne/github-workflows/pb/artifacts/v1"
)

func asset(href, sha string) *pb.Asset {
	return pb.Asset_builder{Href: &href, Sha256: &sha}.Build()
}

func TestFormulaClass(t *testing.T) {
	for name, want := range map[string]string{
		"baton-okta":         "BatonOkta",
		"baton-aws":          "BatonAws",
		"bridge-client":      "BridgeClient",
		"cone":               "Cone",
		"baton_some_thing":   "BatonSomeThing",
		"baton-multi-part-x": "BatonMultiPartX",
	} {
		if got := formulaClass(name); got != want {
			t.Errorf("formulaClass(%q) = %q, want %q", name, got, want)
		}
	}
}

func TestBlockForSkipsIncompleteAssets(t *testing.T) {
	assets := map[string]*pb.Asset{
		"linux-amd64":  asset("https://example.test/a.tar.gz", "abc"),
		"linux-arm64":  asset("", "abc"),
		"darwin-arm64": asset("https://example.test/b.zip", ""),
	}

	if _, ok := blockFor(assets, "linux-amd64", "cond"); !ok {
		t.Error("complete asset should produce a block")
	}
	// An asset missing an href or hash would render a formula that cannot install.
	if _, ok := blockFor(assets, "linux-arm64", "cond"); ok {
		t.Error("asset without href should be skipped")
	}
	if _, ok := blockFor(assets, "darwin-arm64", "cond"); ok {
		t.Error("asset without sha256 should be skipped")
	}
	if _, ok := blockFor(assets, "windows-amd64", "cond"); ok {
		t.Error("absent asset should be skipped")
	}
}

func TestFormulaClassEmptySegments(t *testing.T) {
	// FieldsFunc drops empty segments, so a stray separator must not panic on part[:1].
	if got := formulaClass("baton--okta-"); got != "BatonOkta" {
		t.Errorf("formulaClass with repeated separators = %q", got)
	}
}

func TestRenderFormulaMatchesTapLayout(t *testing.T) {
	out := mustRender(t, formulaData{
		Class:    "BatonOkta",
		Binary:   "baton-okta",
		Homepage: "https://conductorone.com",
		Version:  "0.5.36",
		MacOS: []platformBlock{
			{Condition: "Hardware::CPU.intel?", URL: "https://cdn.test/darwin-amd64.zip", SHA256: "aaa"},
		},
		Linux: []platformBlock{
			{Condition: "Hardware::CPU.intel? && Hardware::CPU.is_64_bit?", URL: "https://cdn.test/linux-amd64.tar.gz", SHA256: "bbb"},
		},
	})

	for _, needle := range []string{
		"class BatonOkta < Formula",
		`version "0.5.36"`,
		"on_macos do",
		"on_linux do",
		`url "https://cdn.test/darwin-amd64.zip"`,
		`sha256 "bbb"`,
		`bin.install "baton-okta"`,
		`system "#{bin}/baton-okta -v"`,
	} {
		if !strings.Contains(out, needle) {
			t.Errorf("rendered formula missing %q\n%s", needle, out)
		}
	}
}

func TestRenderFormulaOmitsEmptyOSBlock(t *testing.T) {
	// A connector with no darwin build must not emit a dangling empty on_macos block.
	out := mustRender(t, formulaData{
		Class:   "BatonLinuxOnly",
		Binary:  "baton-linux-only",
		Version: "1.0.0",
		Linux: []platformBlock{
			{Condition: "Hardware::CPU.intel? && Hardware::CPU.is_64_bit?", URL: "https://cdn.test/l.tar.gz", SHA256: "ccc"},
		},
	})

	if strings.Contains(out, "on_macos") {
		t.Errorf("formula with no darwin assets should omit on_macos\n%s", out)
	}
	if !strings.Contains(out, "on_linux do") {
		t.Errorf("formula should retain on_linux\n%s", out)
	}
}

func mustRender(t *testing.T, data formulaData) string {
	t.Helper()
	out, err := renderFormula(data)
	if err != nil {
		t.Fatalf("renderFormula: %v", err)
	}
	return string(out)
}
