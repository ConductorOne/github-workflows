#!/usr/bin/env node
//
// MDX lint: validates that docs/connector.mdx parses without errors and avoids
// unsafe MDX constructs.
//
// Uses compile() only — no run()/eval. This validates syntax (malformed tags,
// unclosed components, bad nesting) without executing any code from the input.
// Safe to run on untrusted PR content.
//
// Also checks that JSX component names are in the allowed set (compile() alone
// doesn't validate component names — that only fails at runtime). Keep this in
// sync with the registry renderer and documentation update validators.
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
  "Check",
  "Tabs",
  "Tab",
  "Steps",
  "Step",
]);

const EVENT_HANDLER_RE = /\son[a-z]+\s*=/i;
const MDX_COMPONENT_TAG_RE = /<\/?([A-Za-z][A-Za-z0-9.]*)\b/g;

function decodeHtmlEntities(input) {
  return input
    .replace(/&#x([0-9a-f]+);?/gi, (_, hex) =>
      String.fromCodePoint(Number.parseInt(hex, 16)),
    )
    .replace(/&#([0-9]+);?/g, (_, dec) =>
      String.fromCodePoint(Number.parseInt(dec, 10)),
    )
    .replace(/&colon;/gi, ":")
    .replace(/&tab;/gi, "\t")
    .replace(/&newline;/gi, "\n")
    .replace(/&amp;/gi, "&");
}

function containsDangerousUrl(input) {
  const decoded = decodeHtmlEntities(input);
  const normalized = [...decoded]
    .filter((ch) => !/[\s\p{C}]/u.test(ch))
    .join("")
    .toLowerCase();

  return (
    normalized.includes("javascript:") ||
    normalized.includes("vbscript:") ||
    normalized.includes("data:text/html")
  );
}

function openingFence(line) {
  const match = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
  if (!match) {
    return null;
  }
  const marker = match[2];
  const info = match[3] ?? "";
  const char = marker[0];
  if (char === "`" && info.includes("`")) {
    return null;
  }
  return { char, length: marker.length };
}

function isClosingFence(line, fence) {
  const match = /^( {0,3})(`{3,}|~{3,})[ \t]*$/.exec(line);
  return (
    match !== null &&
    match[2][0] === fence.char &&
    match[2].length >= fence.length
  );
}

function stripInlineCode(line) {
  let output = "";
  let index = 0;
  while (index < line.length) {
    if (line[index] !== "`") {
      output += line[index];
      index += 1;
      continue;
    }

    const end = line.indexOf("`", index + 1);
    if (end === -1) {
      output += line.slice(index);
      break;
    }

    output += " ".repeat(end - index + 1);
    index = end + 1;
  }
  return output;
}

function validateSource(content) {
  if (!content.trim()) {
    throw new Error("documentation cannot be empty");
  }
  if (content.includes("\0")) {
    throw new Error("documentation contains NUL bytes");
  }
  if (content.includes("\ufeff")) {
    throw new Error("documentation contains byte order marks");
  }

  let inFence = false;
  let fence = null;
  const lines = content.split(/\r?\n/);
  for (const [index, line] of lines.entries()) {
    const lineNumber = index + 1;
    if (inFence) {
      if (isClosingFence(line, fence)) {
        inFence = false;
        fence = null;
      }
      continue;
    }

    const marker = openingFence(line);
    if (marker) {
      inFence = true;
      fence = marker;
      continue;
    }

    const checkLine = stripInlineCode(line);
    const trimmed = checkLine.trim().toLowerCase();
    if (
      trimmed === "import" ||
      trimmed.startsWith("import ") ||
      trimmed === "export" ||
      trimmed.startsWith("export ")
    ) {
      throw new Error(`line ${lineNumber} contains MDX import/export`);
    }
    if (checkLine.includes("{") || checkLine.includes("}")) {
      throw new Error(`line ${lineNumber} contains MDX expression braces`);
    }
    if (EVENT_HANDLER_RE.test(checkLine)) {
      throw new Error(`line ${lineNumber} contains an event handler attribute`);
    }
    if (containsDangerousUrl(checkLine)) {
      throw new Error(`line ${lineNumber} contains a dangerous URL scheme`);
    }
    for (const match of checkLine.matchAll(MDX_COMPONENT_TAG_RE)) {
      const component = match[1];
      if (!ALLOWED_COMPONENTS.has(component)) {
        throw new Error(
          `line ${lineNumber} contains disallowed JSX component "${component}"`,
        );
      }
    }
  }
  if (inFence) {
    throw new Error("documentation contains an unclosed code fence");
  }
}

async function main() {
  let content = "";
  for await (const chunk of process.stdin) {
    content += chunk;
  }

  try {
    validateSource(content);
  } catch (err) {
    process.stderr.write(`mdx-lint: ${err.message}\n`);
    process.exit(1);
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
