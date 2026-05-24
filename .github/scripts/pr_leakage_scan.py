#!/usr/bin/env python3
"""PR leakage scanner.

Reads a single text input (the concatenated PR title + body + every commit
message) and applies a set of always-on and context-sensitive regexes plus an
external customer-name denylist. Exits 0 when clean, non-zero on any finding.

Pure stdlib: no PyYAML. The banned-tokens file is parsed as a small subset of
YAML (flat keys, list items prefixed with `-`, simple `key: value` mappings,
block scalars via `|`). The format is intentionally restricted so that a single
regex per pattern is unambiguous.

Usage:
  pr_leakage_scan.py --tokens <tokens.yaml> --input <file>
                    [--customer-names <names.txt>]
                    [--allowlist <allowlist.txt>]
                    [--actor <github-actor>]
                    [--expect-fail]

The --expect-fail flag inverts the exit semantics: 0 when the scanner found
≥1 finding (regression test on captured-leak fixtures), non-zero otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# btipling.d2.ductone.com is Bjorn's personal dev box and is the documented
# exception to the *.ductone.com tenant subdomain rule.
DUCTONE_EXCEPTION = "btipling.d2.ductone.com"
SKIP_TOKEN = "[skip-leakage-check]"


@dataclass
class Rule:
    rid: str
    pattern: re.Pattern
    description: str
    # For context-sensitive rules: list of literal strings that must appear
    # within `window` chars of the match.
    adjacent_any: list[str] = field(default_factory=list)
    window: int = 0
    # For C2 quote-or-error context: require either a quoted enclosure or
    # the word "error" within `window` chars.
    require_quote_or_error: bool = False


@dataclass
class Finding:
    rid: str
    description: str
    match_text: str
    line_no: int
    line_excerpt: str


def parse_tokens_file(path: Path) -> tuple[list[Rule], list[Rule]]:
    """Parse the restricted YAML token file.

    Expected shape:

        always_on:
          - id: R1
            pattern: '...'
            description: '...'
          - ...
        context_sensitive:
          - id: C1
            pattern: '...'
            description: '...'
            adjacent_any: [tenant, Tenant, tnt_]
            window: 40
          - id: C2
            pattern: '...'
            description: '...'
            require_quote_or_error: true
            window: 40
    """
    text = path.read_text()
    always_on: list[Rule] = []
    context_sensitive: list[Rule] = []
    current_section: list[Rule] | None = None
    current: dict | None = None

    def flush():
        nonlocal current, current_section
        if current is None or current_section is None:
            return
        rule = Rule(
            rid=current["id"],
            pattern=re.compile(current["pattern"]),
            description=current.get("description", ""),
            adjacent_any=current.get("adjacent_any", []) or [],
            window=int(current.get("window", 0) or 0),
            require_quote_or_error=bool(current.get("require_quote_or_error", False)),
        )
        current_section.append(rule)
        current = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line == "always_on:":
            flush()
            current_section = always_on
            continue
        if line == "context_sensitive:":
            flush()
            current_section = context_sensitive
            continue
        if line.startswith("  - "):
            flush()
            current = {}
            kv = line[4:].strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                current[k.strip()] = _coerce_scalar(v.strip())
            continue
        if line.startswith("    ") and current is not None:
            kv = line.strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    inner = v[1:-1].strip()
                    items = (
                        [s.strip().strip("'").strip('"') for s in inner.split(",") if s.strip()]
                        if inner
                        else []
                    )
                    current[k.strip()] = items
                else:
                    current[k.strip()] = _coerce_scalar(v)
            continue
    flush()
    return always_on, context_sensitive


def _coerce_scalar(v: str):
    if v == "":
        return ""
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.isdigit():
        return int(v)
    return v


def load_customer_names(path: Path | None) -> list[re.Pattern]:
    if path is None or not path.exists():
        return []
    patterns: list[re.Pattern] = []
    for raw in path.read_text().splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        # Whole-word, case-insensitive. Escape so a customer name with
        # punctuation does not get interpreted as a regex.
        escaped = re.escape(name)
        # Replace each escaped whitespace with \s+ so multi-word names match
        # across runs of whitespace (newlines included).
        escaped = re.sub(r"\\\s+", r"\\s+", escaped)
        patterns.append(re.compile(rf"\b{escaped}\b", re.IGNORECASE))
    return patterns


def load_skip_allowlist(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text().splitlines():
        login = raw.strip()
        if not login or login.startswith("#"):
            continue
        out.add(login.lower())
    return out


def _line_lookup(text: str) -> list[tuple[int, int, str]]:
    """Return a list of (start, end, line_text) per line for fast line lookup."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return spans


def _line_no_for_offset(spans: list[tuple[int, int, str]], offset: int) -> tuple[int, str]:
    for i, (start, end, line) in enumerate(spans):
        if start <= offset <= end:
            return i + 1, line
    return -1, ""


def _adjacent_context_ok(text: str, m: re.Match, rule: Rule) -> bool:
    """Return True if the adjacency rule for a context-sensitive match is satisfied."""
    start = max(0, m.start() - rule.window)
    end = min(len(text), m.end() + rule.window)
    window_text = text[start:end]
    if rule.adjacent_any:
        if not any(token in window_text for token in rule.adjacent_any):
            return False
    if rule.require_quote_or_error:
        # Check for a quoted enclosure around the match OR the word "error" in window.
        if "error" in window_text.lower():
            return True
        before = text[max(0, m.start() - rule.window) : m.start()]
        after = text[m.end() : min(len(text), m.end() + rule.window)]
        # Look for the nearest preceding quote and following quote of same kind.
        for q in ("'", '"'):
            if q in before and q in after:
                return True
        return False
    return True


def scan(
    text: str,
    always_on: Iterable[Rule],
    context_sensitive: Iterable[Rule],
    customer_patterns: Iterable[re.Pattern],
) -> list[Finding]:
    spans = _line_lookup(text)
    findings: list[Finding] = []

    for rule in always_on:
        for m in rule.pattern.finditer(text):
            matched = m.group(0)
            if rule.rid == "R2" and matched.lower() == DUCTONE_EXCEPTION:
                continue
            line_no, line = _line_no_for_offset(spans, m.start())
            findings.append(
                Finding(
                    rid=rule.rid,
                    description=rule.description,
                    match_text=matched,
                    line_no=line_no,
                    line_excerpt=line.strip()[:200],
                )
            )

    for rule in context_sensitive:
        for m in rule.pattern.finditer(text):
            if not _adjacent_context_ok(text, m, rule):
                continue
            matched = m.group(0)
            line_no, line = _line_no_for_offset(spans, m.start())
            findings.append(
                Finding(
                    rid=rule.rid,
                    description=rule.description,
                    match_text=matched,
                    line_no=line_no,
                    line_excerpt=line.strip()[:200],
                )
            )

    for i, pat in enumerate(customer_patterns):
        for m in pat.finditer(text):
            line_no, line = _line_no_for_offset(spans, m.start())
            findings.append(
                Finding(
                    rid=f"CUST{i+1}",
                    description="customer-name denylist match",
                    match_text=m.group(0),
                    line_no=line_no,
                    line_excerpt=line.strip()[:200],
                )
            )

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan PR text for data-leak patterns.")
    ap.add_argument("--tokens", required=True, type=Path)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--customer-names", type=Path)
    ap.add_argument("--allowlist", type=Path)
    ap.add_argument("--actor", default="")
    ap.add_argument("--expect-fail", action="store_true",
                    help="invert exit code for regression-test usage")
    args = ap.parse_args()

    always_on, context_sensitive = parse_tokens_file(args.tokens)
    customer_patterns = load_customer_names(args.customer_names)
    allowlist = load_skip_allowlist(args.allowlist)

    text = args.input.read_text()

    skip_present = SKIP_TOKEN in text
    actor_ok = args.actor.lower() in allowlist if args.actor else False

    if skip_present and actor_ok:
        print(f"::warning::skip honored for {args.actor}")
        return 0 if not args.expect_fail else 1

    if skip_present and not actor_ok:
        print(f"::error::{SKIP_TOKEN} present but actor '{args.actor}' not allowlisted")
        return 1

    findings = scan(text, always_on, context_sensitive, customer_patterns)

    if args.expect_fail:
        if findings:
            print(f"expected-fail OK: {len(findings)} finding(s)")
            return 0
        print("::error::expected at least one finding on this fixture; got zero")
        return 1

    if not findings:
        print("scan clean: no findings")
        return 0

    for f in findings:
        # GitHub Actions error annotation so the failures show up in the PR UI.
        print(
            f"::error file={args.input.name},line={f.line_no}::"
            f"[{f.rid}] {f.description} — matched '{f.match_text}' in: {f.line_excerpt}"
        )
    print(f"scan failed: {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
