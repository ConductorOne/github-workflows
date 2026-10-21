package main

import (
	"bytes"
	"fmt"
	"runtime"
	"strings"
	"testing"
)

func summaryConfigurations(platform string) []summaryOptions {
	var configurations []summaryOptions
	for _, mode := range []string{"report", "enforce"} {
		for _, apply := range []bool{false, true} {
			for _, deny := range []bool{false, true} {
				for _, elevation := range []string{"fail", "warn"} {
					configurations = append(configurations, summaryOptions{mode, apply, deny, elevation, platform})
				}
			}
		}
	}
	return configurations
}

func evidencePair(phase, store, verdict string) []row {
	return []row{{Phase: phase, Store: store, Chain: "1", Verdict: verdict},
		{Phase: phase, Store: store, Chain: "2", Verdict: verdict}}
}

func TestSummarizeModeFlagElevationCombinations(t *testing.T) {
	states := []struct {
		name     string
		baseline bool
		clean    bool
		physical string
		skipped  bool
		hasError bool
		npm      string
	}{
		{"healthy", true, true, distrust, false, false, distrust},
		{"already-clean-baseline", false, true, distrust, false, false, distrust},
		{"ineffective-clean", true, false, distrust, false, false, distrust},
		{"ineffective-clean-on-clean-baseline", false, false, distrust, false, false, distrust},
		{"physical-trusts", true, true, trusts, false, false, distrust},
		{"physical-unknown", true, true, unknown, false, false, distrust},
		{"physical-missing", true, true, "", false, false, distrust},
		{"not-elevated", true, true, distrust, true, false, distrust},
		{"not-elevated-physical-failed", true, true, unknown, true, false, distrust},
		{"probe-error", true, true, distrust, false, true, distrust},
		{"npm-ineffective", true, true, distrust, false, false, trusts},
		{"npm-absent", true, true, distrust, false, false, ""},
	}
	for _, platform := range []struct{ name, store string }{
		{"linux", "ubuntu-system"}, {"darwin", "macos-sectrust"}, {"windows", "windows-disallowed-store"},
	} {
		for _, options := range summaryConfigurations(platform.name) {
			for _, state := range states {
				name := fmt.Sprintf("%s/%s/apply=%t/deny=%t/elevation=%s/%s", platform.name,
					options.mode, options.apply, options.deny, options.elevation, state.name)
				t.Run(name, func(t *testing.T) {
					baselineVerdict := distrust
					if state.baseline {
						baselineVerdict = trusts
					}
					rows := evidencePair("before", "system", baselineVerdict)
					if state.physical != "" {
						rows = append(rows, evidencePair("after", platform.store, state.physical)...)
					}
					if state.clean {
						rows = append(rows, evidencePair("after", "clean-ca-bundle", distrust)...)
					}
					if state.npm != "" {
						rows = append(rows, evidencePair("after", "npm-effective-cafile", state.npm)...)
					}
					// These untouched bundles must remain observations, never OS-deny gates.
					rows = append(rows, evidencePair("after", "node-effective", trusts)...)
					if platform.name == "darwin" {
						rows = append(rows, evidencePair("after", "macos-etc-ssl-cert-pem", trusts)...)
						rows = append(rows, evidencePair("after", "macos-brew", trusts)...)
					}
					if state.skipped {
						rows = append(rows, row{Phase: "os-deny", Verdict: "SKIPPED-NOT-ELEVATED"})
					}
					if state.hasError {
						rows = append(rows, row{Phase: "after", Verdict: probeError})
					}
					result, err := decision(rows, options)
					physicalFailed := options.deny && state.physical != distrust
					elevationFailed := options.deny && state.skipped && options.elevation == "fail"
					cleanFailed := options.apply && (state.baseline || options.deny) &&
						(!state.clean || (state.npm != "" && state.npm != distrust))
					freeze := state.baseline && !options.apply
					wantFailure := state.hasError || (options.mode == "enforce" &&
						(physicalFailed || elevationFailed || cleanFailed || freeze))
					if (err != nil) != wantFailure {
						t.Fatalf("error=%v, want failure=%t, summary=%+v", err, wantFailure, result)
					}
					if !options.deny && result.MitigationScope != "clean-bundle-only" {
						t.Fatalf("wrong scope: %+v", result)
					}
					if options.mode == "enforce" && physicalFailed && !state.hasError && !elevationFailed &&
						!strings.Contains(err.Error(), platform.store) {
						t.Fatalf("failure must name store: %v", err)
					}
				})
			}
		}
	}
}

func TestSummarizeEvidenceFiles(t *testing.T) {
	for _, options := range summaryConfigurations(runtime.GOOS) {
		for _, skipped := range []bool{false, true} {
			name := fmt.Sprintf("%s/apply=%t/deny=%t/elevation=%s/skipped=%t", options.mode,
				options.apply, options.deny, options.elevation, skipped)
			t.Run(name, func(t *testing.T) {
				dir := t.TempDir()
				for _, phase := range []string{"before", "after"} {
					rows := evidencePair("positive-control", "vulnerable-roots", trusts)
					if phase == "before" {
						rows = append(rows, evidencePair(phase, "system", trusts)...)
					} else {
						for _, label := range requiredPhysicalStores(runtime.GOOS) {
							rows = append(rows, evidencePair(phase, label, distrust)...)
						}
						rows = append(rows, evidencePair(phase, "clean-ca-bundle", distrust)...)
					}
					writeTestRows(t, dir, phase, rows)
				}
				verdict := "DENIED"
				if skipped {
					verdict = "SKIPPED-NOT-ELEVATED"
				}
				writeTestRows(t, dir, "os-deny", []row{{Phase: "os-deny", Verdict: verdict}})
				writeTestRows(t, dir, "apply-clean-bundle", []row{{Phase: "apply-clean-bundle", Verdict: "APPLIED"}})
				var stdout bytes.Buffer
				err := summarize([]string{"--mode", options.mode, fmt.Sprintf("--apply-distrust=%t", options.apply),
					fmt.Sprintf("--os-deny=%t", options.deny), "--os-deny-elevation", options.elevation, "--in", dir}, &stdout)
				wantFailure := options.mode == "enforce" && (!options.apply ||
					(options.deny && skipped && options.elevation == "fail"))
				if (err != nil) != wantFailure {
					t.Fatalf("error=%v, want failure=%t; %s", err, wantFailure, &stdout)
				}
			})
		}
	}
}

func TestDecisionUnknownsAndCleanCompleteness(t *testing.T) {
	rows := []row{{Phase: "before", Verdict: unknown}, {Phase: "after", Verdict: rejectedUnknown}}
	result, err := decision(rows, summaryOptions{mode: "report"})
	if err != nil || result.Summary != "ok-with-2-unknown-cause-rejections" {
		t.Fatalf("%+v %v", result, err)
	}
	rows = []row{{Phase: "before", Verdict: trusts},
		{Phase: "after", Store: "clean-ca-bundle", Chain: "1", Verdict: distrust},
		{Phase: "after", Store: "clean-ca-bundle", Chain: "1", Verdict: distrust}}
	if _, err := decision(rows, summaryOptions{mode: "enforce", apply: true}); err == nil {
		t.Fatal("duplicate chain cannot replace missing clean chain 2")
	}
	if err := summarize([]string{"--in", t.TempDir()}, &bytes.Buffer{}); err == nil {
		t.Fatal("missing evidence must fail closed")
	}
	if err := summarize([]string{"--in", t.TempDir(), "--os-deny-elevation", "typo"}, &bytes.Buffer{}); err == nil {
		t.Fatal("invalid elevation policy must fail closed")
	}
}
