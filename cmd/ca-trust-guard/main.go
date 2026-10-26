// Command ca-trust-guard checks incident chains against explicit CA stores.
// Go orchestrates the gated OpenSSL measurement instrument; crypto/x509 parses
// pinned fixture identities, but must not verify these incident chains (its
// unconditional SPKI deny list defeats the positive control). Discovery commands
// for certifi, Homebrew and OpenSSL return paths only. SecTrust, Node's root dump
// and Windows Disallowed are separate client/platform observations.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
)

const (
	trusts          = "TRUSTS"
	distrust        = "VERIFIED-DISTRUST"
	probeError      = "ERROR"
	unknown         = "UNKNOWN"
	rejectedUnknown = "REJECTED-UNKNOWN-CAUSE"
)

type row struct {
	Image           string `json:"image,omitempty"`
	OS              string `json:"os,omitempty"`
	Phase           string `json:"phase,omitempty"`
	Store           string `json:"store,omitempty"`
	Chain           string `json:"chain,omitempty"`
	Verdict         string `json:"verdict,omitempty"`
	Detail          string `json:"detail,omitempty"`
	Summary         string `json:"summary,omitempty"`
	Mode            string `json:"mode,omitempty"`
	ApplyDistrust   string `json:"apply_distrust,omitempty"`
	OSDeny          string `json:"os_deny,omitempty"`
	ElevationPolicy string `json:"os_deny_elevation,omitempty"`
	MitigationScope string `json:"mitigation_scope,omitempty"`
	Residual        string `json:"residual,omitempty"`
	NotCovered      string `json:"not_covered,omitempty"`
	OpenSSLVersion  string `json:"openssl_version,omitempty"`
	OpenSSLPath     string `json:"openssl_path,omitempty"`
}

type rowWriter struct {
	file       *os.File
	stdout     io.Writer
	instrument opensslVerifier
}

func openRows(path string, appendRows bool, stdout io.Writer) (*rowWriter, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	flags := os.O_CREATE | os.O_WRONLY | os.O_TRUNC
	if appendRows {
		flags = os.O_CREATE | os.O_WRONLY | os.O_APPEND
	}
	file, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		return nil, err
	}
	return &rowWriter{file: file, stdout: stdout}, nil
}

func (w *rowWriter) emit(r row) error {
	r.Image = os.Getenv("ImageVersion")
	if r.Image == "" {
		r.Image = os.Getenv("ImageOS")
	}
	if r.Image == "" {
		r.Image = "unknown"
	}
	r.OS = runtime.GOOS
	r.OpenSSLVersion = w.instrument.version
	r.OpenSSLPath = w.instrument.path
	data, err := json.Marshal(r)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintln(w.file, string(data)); err != nil {
		return err
	}
	_, err = fmt.Fprintln(w.stdout, "ca-trust-guard "+string(data))
	return err
}

func record(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("record", flag.ContinueOnError)
	out := flags.String("out", "", "JSONL output file")
	r := row{Chain: "-"}
	flags.StringVar(&r.Phase, "phase", "os-deny", "observation phase")
	flags.StringVar(&r.Store, "store", "os-deny", "store label")
	flags.StringVar(&r.Verdict, "verdict", "", "outcome")
	flags.StringVar(&r.Detail, "detail", "", "outcome detail")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *out == "" || r.Verdict == "" {
		return errors.New("record requires --out and --verdict")
	}
	writer, err := openRows(*out, true, stdout)
	if err != nil {
		return err
	}
	defer writer.file.Close()
	writer.instrument = evidenceInstrument(filepath.Dir(*out))
	return writer.emit(r)
}

func run(args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("expected probe, summarize, or record")
	}
	switch args[0] {
	case "probe":
		return probe(args[1:], stdout)
	case "summarize":
		return summarize(args[1:], stdout)
	case "record":
		return record(args[1:], stdout)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "::error title=CA trust guard::"+err.Error())
		os.Exit(1)
	}
}
