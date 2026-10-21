package main

import (
	"crypto/sha1" // Windows Disallowed identifies certificates by SHA-1 thumbprint.
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
)

type store struct{ label, path string }

type commandRunner func(string, ...string) ([]byte, error)

func execute(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).CombinedOutput()
}

func executeOutput(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).Output()
}

func discoverStores(goos string, command commandRunner) []store {
	var stores []store
	switch goos {
	case "linux":
		stores = append(stores, store{"ubuntu-system", "/etc/ssl/certs/ca-certificates.crt"})
		if path, err := command("python3", "-c", "import certifi; print(certifi.where())"); err == nil && strings.TrimSpace(string(path)) != "" {
			stores = append(stores, store{"python-certifi", strings.TrimSpace(string(path))})
		}
	case "darwin":
		stores = append(stores, store{"macos-etc-ssl-cert-pem", "/etc/ssl/cert.pem"})
		if prefix, err := command("brew", "--prefix"); err == nil && strings.TrimSpace(string(prefix)) != "" {
			path := filepath.Join(strings.TrimSpace(string(prefix)), "etc", "ca-certificates", "cert.pem")
			if _, err := os.Stat(path); err == nil {
				stores = append(stores, store{"macos-brew", path})
			}
		}
	case "windows":
		output, _ := command("openssl", "version", "-d")
		parts := strings.Split(string(output), "\"")
		path := ""
		if len(parts) >= 3 {
			path = parts[1]
		}
		stores = append(stores, store{"windows-gitbash-openssl", filepath.Join(nativePath(path), "cert.pem")})
	}
	return stores
}

// Git Bash supplies its mount root through the glue driver. Translate paths
// only; this never changes which explicit roots are used for verification.
func nativePath(path string) string {
	if runtime.GOOS == "windows" {
		if len(path) >= 3 && path[0] == '/' && path[2] == '/' {
			return string(path[1]) + ":" + filepath.FromSlash(path[2:])
		}
		if root := os.Getenv("CA_GUARD_MSYS_ROOT"); root != "" && strings.HasPrefix(path, "/") {
			return filepath.Join(root, filepath.FromSlash(strings.TrimPrefix(path, "/")))
		}
	}
	return path
}

func parseStores(value string) ([]store, error) {
	var stores []store
	for _, entry := range strings.Split(value, ",") {
		label, path, ok := strings.Cut(entry, "=")
		if !ok || label == "" || path == "" {
			return nil, fmt.Errorf("invalid store %q; expected label=path", entry)
		}
		stores = append(stores, store{label, nativePath(path)})
	}
	return stores, nil
}

var sectrustDistrust = regexp.MustCompile(`(?i)not[_ ]trusted|revoked|untrusted|CSSMERR_TP_NOT_TRUSTED|kSecTrustResultDeny|result:.*Deny`)

func classifySecTrust(output string, err error) (string, string) {
	if err == nil {
		return trusts, ""
	}
	detail := strings.SplitN(output, "\n", 2)[0]
	if sectrustDistrust.MatchString(output) {
		return distrust, detail
	}
	if strings.Contains(output, "CSSMERR_TP_VERIFY_ACTION_FAILED") {
		return rejectedUnknown, detail
	}
	return probeError, detail
}

func warnBundleAge(dir string) {
	data, err := os.ReadFile(filepath.Join(dir, "clean-ca-bundle.pem"))
	if err != nil {
		return
	}
	const prefix = "## Certificate data from Mozilla as of: "
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, prefix) {
			continue
		}
		date, err := time.Parse("Mon Jan 2 15:04:05 2006 MST", strings.TrimPrefix(line, prefix))
		if err == nil && time.Since(date) > 90*24*time.Hour {
			fmt.Fprintln(os.Stderr, "::warning title=CA trust guard::clean bundle Mozilla data older than 90 days; refresh via PR")
		}
		return
	}
}

func probe(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("probe", flag.ContinueOnError)
	phase := flags.String("phase", "", "before or after")
	storesArg := flags.String("stores", "auto", "comma-separated label=path stores, or auto")
	chainsPath := flags.String("chains", "", "chains.json fixture descriptor")
	out := flags.String("out", "", "JSONL evidence file")
	apply := flags.Bool("apply-distrust", false, "include clean-bundle verification after applying environment")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if (*phase != "before" && *phase != "after") || *chainsPath == "" || *out == "" {
		return errors.New("probe requires --phase before|after, --chains and --out")
	}
	writer, err := openRows(*out, false, stdout)
	if err != nil {
		return err
	}
	defer writer.file.Close()
	fatal := func(err error) error {
		if writeErr := writer.emit(row{Phase: "positive-control", Store: "fixtures", Chain: "-", Verdict: probeError, Detail: err.Error()}); writeErr != nil {
			return writeErr
		}
		return err
	}
	dir := filepath.Dir(*chainsPath)
	if err := verifyFixtures(dir); err != nil {
		return fatal(err)
	}
	chains, err := loadChains(*chainsPath)
	if err != nil {
		return fatal(err)
	}
	verifier, err := newVerifier()
	if err != nil {
		return fatal(err)
	}
	writer.instrument = verifier
	// The very first trust observation is the positive control for EVERY
	// chain, before discovery or any physical/client store observation.
	positive := true
	for _, c := range chains {
		verdict, detail := verifier.classifyStore(c, filepath.Join(dir, "vulnerable-roots.pem"))
		if verdict != trusts {
			verdict = probeError
			positive = false
			detail = "positive control failed: " + detail
		}
		if err := writer.emit(row{Phase: "positive-control", Store: "vulnerable-roots", Chain: c.spec.ID, Verdict: verdict, Detail: detail}); err != nil {
			return err
		}
	}
	if !positive {
		return errors.New("positive control failed; refusing to probe or mutate stores")
	}
	warnBundleAge(dir)
	var stores []store
	if *storesArg == "auto" {
		stores = discoverStores(runtime.GOOS, executeOutput)
	} else {
		stores, err = parseStores(*storesArg)
		if err != nil {
			return err
		}
	}
	if *phase == "after" && *apply {
		stores = append(stores, store{"clean-ca-bundle", filepath.Join(dir, "clean-ca-bundle.pem")})
	}
	checkStore := func(s store) error {
		for _, c := range chains {
			verdict, detail := verifier.classifyStore(c, s.path)
			if err := writer.emit(row{Phase: *phase, Store: s.label, Chain: c.spec.ID, Verdict: verdict, Detail: detail}); err != nil {
				return err
			}
		}
		return nil
	}
	for _, s := range stores {
		if err := checkStore(s); err != nil {
			return err
		}
	}
	if runtime.GOOS == "linux" || runtime.GOOS == "darwin" {
		if _, err := exec.LookPath("node"); err == nil {
			if path := os.Getenv("NPM_CONFIG_CAFILE"); path != "" {
				if _, err := os.Stat(path); err == nil {
					if err := checkStore(store{"npm-effective-cafile", path}); err != nil {
						return err
					}
				}
			}
			if strings.Contains(os.Getenv("NODE_OPTIONS"), "--use-openssl-ca") {
				if err := writer.emit(row{Phase: *phase, Store: "node-effective", Chain: "-", Verdict: "DELEGATED-OPENSSL", Detail: "NODE_OPTIONS=--use-openssl-ca; see clean-ca-bundle rows"}); err != nil {
					return err
				}
			} else if data, err := executeOutput("node", "-e", `const t=require("tls"); const l=t.getCACertificates?t.getCACertificates("default"):t.rootCertificates; for(const c of l) process.stdout.write(c+"\n")`); err == nil {
				path := filepath.Join(filepath.Dir(*out), "node.pem")
				if err := os.WriteFile(path, data, 0600); err != nil {
					return err
				}
				if err := checkStore(store{"node-effective", path}); err != nil {
					return err
				}
			}
		}
	}
	if runtime.GOOS == "darwin" {
		for _, c := range chains {
			output, commandErr := execute("security", "verify-cert", "-c", c.leafPath, "-c", c.intermediatesPath, "-p", "ssl", "-L")
			verdict, detail := classifySecTrust(string(output), commandErr)
			if err := writer.emit(row{Phase: *phase, Store: "macos-sectrust", Chain: c.spec.ID, Verdict: verdict, Detail: detail}); err != nil {
				return err
			}
		}
	}
	if runtime.GOOS == "windows" {
		for _, c := range chains {
			digest := sha1.Sum(c.intermediates[0].Raw)
			thumbprint := strings.ToUpper(hex.EncodeToString(digest[:]))
			command := fmt.Sprintf("if (Get-ChildItem Cert:\\LocalMachine\\Disallowed | Where-Object Thumbprint -eq '%s') { exit 0 } else { exit 3 }", thumbprint)
			_, commandErr := execute("powershell", "-NoProfile", "-Command", command)
			verdict, detail := distrust, "intermediate thumbprint "+thumbprint+" in Disallowed"
			if commandErr != nil {
				verdict, detail = unknown, "intermediate thumbprint "+thumbprint+" not in Disallowed; revocation block not provable offline"
			}
			if err := writer.emit(row{Phase: *phase, Store: "windows-disallowed-store", Chain: c.spec.ID, Verdict: verdict, Detail: detail}); err != nil {
				return err
			}
		}
	}
	return nil // Store ERROR observations are gated by summarize, not this step.
}
