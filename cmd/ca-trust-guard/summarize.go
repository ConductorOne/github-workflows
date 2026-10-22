package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

const residuals = "action-tarball-downloads-before-step-1;runner-agent-dotnet-client;runner-image-go-bootstrap;" +
	"go(version-gated>=1.27.3);js-actions-and-node-steps-without-NODE_OPTIONS=--use-openssl-ca;" +
	"dockerd-and-buildkit-TLS-on-all-platforms(mitigated-by-image-digests+immutable-tags);windows-node-bundled-roots"

type summaryOptions struct {
	mode      string
	apply     bool
	deny      bool
	elevation string
	platform  string
}

func requiredPhysicalStores(platform string) []string {
	switch platform {
	case "linux":
		return []string{"ubuntu-system"}
	case "darwin":
		return []string{"macos-sectrust"}
	case "windows":
		return []string{"windows-disallowed-store"}
	default:
		return []string{"unsupported-platform"}
	}
}

func unverifiedStores(rows []row, labels []string) []string {
	verified := make(map[string]map[string]bool)
	for _, r := range rows {
		if r.Phase != "after" {
			continue
		}
		if verified[r.Store] == nil {
			verified[r.Store] = make(map[string]bool)
		}
		// Duplicate contradictory rows must never turn a failed store into proof.
		if previous, exists := verified[r.Store][r.Chain]; exists {
			verified[r.Store][r.Chain] = previous && r.Verdict == distrust
		} else {
			verified[r.Store][r.Chain] = r.Verdict == distrust
		}
	}
	var missing []string
	for _, label := range labels {
		for _, chain := range []string{"1", "2"} {
			if !verified[label][chain] {
				missing = append(missing, label+" chain "+chain)
			}
		}
	}
	return missing
}

func elevationSkipped(rows []row) bool {
	for _, r := range rows {
		if r.Phase == "os-deny" && r.Verdict == "SKIPPED-NOT-ELEVATED" {
			return true
		}
	}
	return false
}

func decision(rows []row, options summaryOptions) (row, error) {
	baselineTrusts, hasError, unknownCount := false, false, 0
	hasNPMStore := false
	for _, r := range rows {
		if r.Phase == "before" && r.Verdict == trusts {
			baselineTrusts = true
		}
		if r.Verdict == probeError {
			hasError = true
		}
		if r.Verdict == rejectedUnknown || r.Verdict == unknown {
			unknownCount++
		}
		if r.Phase == "after" && r.Store == "npm-effective-cafile" {
			hasNPMStore = true
		}
	}
	var missingPhysical []string
	if options.deny {
		missingPhysical = unverifiedStores(rows, requiredPhysicalStores(options.platform))
	}
	var missingClean []string
	if options.apply {
		labels := []string{"clean-ca-bundle"}
		if hasNPMStore {
			labels = append(labels, "npm-effective-cafile")
		}
		missingClean = unverifiedStores(rows, labels)
	}
	mitigated := options.apply && len(missingClean) == 0 && !hasError && len(missingPhysical) == 0
	result := row{Summary: "ok", Mode: options.mode, ApplyDistrust: strconv.FormatBool(options.apply),
		OSDeny: strconv.FormatBool(options.deny), ElevationPolicy: options.elevation,
		MitigationScope: "clean-bundle-only", Residual: "none", NotCovered: residuals}
	if options.deny {
		result.MitigationScope = "clean-bundle-and-required-physical-stores"
	}
	if unknownCount > 0 {
		result.Summary = fmt.Sprintf("ok-with-%d-unknown-cause-rejections", unknownCount)
	}
	if baselineTrusts {
		result.Summary, result.Residual = "STILL_TRUSTS_COMPROMISED", "physical-store-trusts-chain"
		if mitigated {
			result.Summary = "PHYSICAL_STORE_TRUSTS_BUT_MITIGATED_BY_CLEAN_BUNDLE"
		}
	}
	if hasError {
		result.Summary = probeError
		return result, errors.New("probe ERROR (fixture/store problem); failing closed")
	}
	if options.mode == "enforce" {
		if options.deny && options.elevation == "fail" && elevationSkipped(rows) {
			result.Summary = "OS_DENY_NOT_ELEVATED"
			return result, errors.New("OS deny could not run elevated (os-deny-elevation=fail)")
		}
		if len(missingPhysical) > 0 {
			result.Summary = "OS_DENY_POSTCONDITION_NOT_VERIFIED"
			return result, fmt.Errorf("OS deny not verified effective: %s", strings.Join(missingPhysical, ", "))
		}
		if options.apply && (baselineTrusts || options.deny) && len(missingClean) > 0 {
			return result, fmt.Errorf("clean-bundle environment not verified effective: %s", strings.Join(missingClean, ", "))
		}
		if baselineTrusts {
			if options.apply && !mitigated {
				return result, errors.New("baseline trusts incident chain and mitigation is not verified effective")
			}
			if !options.apply {
				return result, errors.New("baseline trusts incident chain; release jobs remain frozen")
			}
		}
	}
	return result, nil
}

func readRows(path string) ([]row, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	var rows []row
	for scanner.Scan() {
		var r row
		if err := json.Unmarshal(scanner.Bytes(), &r); err != nil {
			return nil, fmt.Errorf("decode %s: %w", path, err)
		}
		if r.Phase == "" || r.Verdict == "" {
			return nil, fmt.Errorf("incomplete evidence row in %s", path)
		}
		rows = append(rows, r)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("empty evidence file %s", path)
	}
	return rows, nil
}

func summarize(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("summarize", flag.ContinueOnError)
	mode := flags.String("mode", "enforce", "report or enforce")
	apply := flags.Bool("apply-distrust", true, "clean-bundle environment was enabled")
	deny := flags.Bool("os-deny", true, "OS deny was enabled")
	elevation := flags.String("os-deny-elevation", "fail", "fail or warn when OS denial cannot run elevated")
	dir := flags.String("in", "", "directory containing phase JSONL evidence")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if (*mode != "report" && *mode != "enforce") || (*elevation != "fail" && *elevation != "warn") || *dir == "" {
		return errors.New("summarize requires --mode report|enforce, --os-deny-elevation fail|warn and --in")
	}
	files := []string{"before", "after"}
	if *deny {
		files = append(files, "os-deny")
	}
	if *apply {
		files = append(files, "apply-clean-bundle")
	}
	var rows []row
	for _, name := range files {
		observations, err := readRows(filepath.Join(*dir, name+".jsonl"))
		if err != nil {
			rows = append(rows, row{Phase: name, Verdict: probeError, Detail: err.Error()})
			continue
		}
		if name == "before" || name == "after" {
			observed := false
			positive := make(map[string]bool)
			for _, r := range observations {
				if r.Phase == name {
					observed = true
				}
				if r.Phase == "positive-control" && r.Verdict == trusts {
					positive[r.Chain] = true
				}
			}
			if !observed || !positive["1"] || !positive["2"] {
				rows = append(rows, row{Phase: name, Verdict: probeError, Detail: "missing positive controls or physical-store observations"})
			}
		}
		rows = append(rows, observations...)
	}
	if *deny && elevationSkipped(rows) && (*elevation == "warn" || *mode == "report") {
		fmt.Fprintln(os.Stderr, "::warning title=CA trust guard::OS deny skipped: runner is not elevated; physical evidence is still required in enforce mode")
	}
	options := summaryOptions{*mode, *apply, *deny, *elevation, runtime.GOOS}
	result, gateError := decision(rows, options)
	writer, err := openRows(filepath.Join(*dir, "summary.jsonl"), false, stdout)
	if err != nil {
		return err
	}
	defer writer.file.Close()
	writer.instrument = evidenceInstrument(*dir)
	if err := writer.emit(result); err != nil {
		return err
	}
	return gateError
}
