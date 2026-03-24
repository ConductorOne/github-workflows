#!/usr/bin/env node
//
// MDX lint: validates that docs/connector.mdx parses without errors.
//
// Uses compile() only — no run()/eval. This validates syntax (malformed tags,
// unclosed components, bad nesting) without executing any code from the input.
// Safe to run on untrusted PR content.
//
// Also checks that JSX component names are in the allowed set (compile() alone
// doesn't validate component names — that only fails at runtime).
//
// Usage:
//   node mdx-lint.mjs < docs/connector.mdx
//
// Exit codes:
//   0 - valid MDX
//   1 - compilation error (message on stderr)

import { compile } from "@mdx-js/mdx";
import remarkGfm from "remark-gfm";
import remarkFrontmatter from "remark-frontmatter";

// Components supported by the registry's MDX renderer (mdx-compile.mjs).
// Keep in sync with the component map in the registry-api's ui/mdx-compile.mjs.
const ALLOWED_COMPONENTS = new Set([
  "Tip",
  "Warning",
  "Note",
  "Info",
  "Icon",
  "Frame",
  "Card",
  "Tabs",
  "Tab",
  "Steps",
  "Step",
]);

async function main() {
  let content = "";
  for await (const chunk of process.stdin) {
    content += chunk;
  }

  if (!content.trim()) {
    process.exit(0);
  }

  // Compile: catches syntax errors (malformed tags, bad nesting, etc.)
  let compiled;
  try {
    compiled = String(
      await compile(content, {
        outputFormat: "function-body",
        remarkPlugins: [remarkGfm, remarkFrontmatter],
      }),
    );
  } catch (err) {
    process.stderr.write(`mdx-lint: ${err.message}\n`);
    process.exit(1);
  }

  // Check for unknown components: compile() generates _missingMdxReference("Name", true)
  // for any JSX component not provided at runtime. We scan the compiled output for these
  // references — this avoids false positives from angle brackets in inline text (e.g.
  // `<YOUR_DOMAIN>` inside backticks).
  const refPattern = /_missingMdxReference\("([^"]+)"/g;
  const unknown = new Set();
  let match;
  while ((match = refPattern.exec(compiled)) !== null) {
    const name = match[1];
    if (!ALLOWED_COMPONENTS.has(name)) {
      unknown.add(name);
    }
  }

  if (unknown.size > 0) {
    for (const name of unknown) {
      process.stderr.write(
        `mdx-lint: Unknown component <${name}>. Allowed: ${[...ALLOWED_COMPONENTS].join(", ")}\n`,
      );
    }
    process.exit(1);
  }
}

main();
