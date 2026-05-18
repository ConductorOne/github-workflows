#!/usr/bin/env node
//
// MDX lint: validates that docs/connector.mdx parses without errors and avoids
// unsafe MDX constructs.
//
// Uses parse()/compile() only; never run()/eval(). This validates syntax and
// walks the MDX AST without executing any code from the input. Safe to run on
// untrusted PR content.
//
// Usage:
//   node mdx-lint.mjs < docs/connector.mdx
//
// Exit codes:
//   0 - valid MDX
//   1 - validation or compilation error (message on stderr)

import { pathToFileURL } from "node:url";

import { compile } from "@mdx-js/mdx";
import remarkFrontmatter from "remark-frontmatter";
import remarkGfm from "remark-gfm";
import remarkMdx from "remark-mdx";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { visit } from "unist-util-visit";

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

const URL_ATTRIBUTE_NAMES = new Set(["href", "src", "action", "formaction"]);

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
  const decoded = decodeHtmlEntities(String(input));
  const normalized = [...decoded]
    .filter((ch) => !/[\s\p{C}]/u.test(ch))
    .join("")
    .toLowerCase();

  return (
    normalized.includes("javascript:") ||
    normalized.includes("vbscript:") ||
    normalized.includes("data:")
  );
}

function at(node) {
  return node.position?.start?.line
    ? `line ${node.position.start.line}`
    : "document";
}

function fail(node, message) {
  throw new Error(`${at(node)} ${message}`);
}

function validateUrlNode(node) {
  if (node.url && containsDangerousUrl(node.url)) {
    fail(node, "contains a dangerous URL scheme");
  }
}

function validateJsxAttribute(attribute, node) {
  if (attribute.type === "mdxJsxExpressionAttribute") {
    fail(node, "contains a JSX expression attribute");
  }
  if (attribute.type !== "mdxJsxAttribute") {
    fail(node, "contains an unsupported JSX attribute");
  }

  const name = String(attribute.name ?? "");
  if (/^on[a-z]/i.test(name)) {
    fail(node, "contains an event handler attribute");
  }
  if (attribute.value && typeof attribute.value === "object") {
    fail(node, "contains a JSX attribute expression");
  }
  if (typeof attribute.value === "string") {
    if (URL_ATTRIBUTE_NAMES.has(name.toLowerCase())) {
      if (containsDangerousUrl(attribute.value)) {
        fail(node, "contains a dangerous URL scheme");
      }
    }
    if (containsDangerousUrl(attribute.value)) {
      fail(node, "contains a dangerous URL scheme");
    }
  }
}

function validateJsxElement(node) {
  if (!node.name) {
    fail(node, "contains a JSX fragment");
  }
  if (node.name.includes(".")) {
    fail(node, `contains disallowed JSX component "${node.name}"`);
  }
  if (!ALLOWED_COMPONENTS.has(node.name)) {
    fail(node, `contains disallowed JSX component "${node.name}"`);
  }

  for (const attribute of node.attributes ?? []) {
    validateJsxAttribute(attribute, node);
  }
}

function validateTree(tree) {
  visit(tree, (node) => {
    switch (node.type) {
      case "mdxjsEsm":
        fail(node, "contains MDX import/export");
        break;
      case "mdxFlowExpression":
      case "mdxTextExpression":
        fail(node, "contains an MDX expression");
        break;
      case "mdxJsxFlowElement":
      case "mdxJsxTextElement":
        validateJsxElement(node);
        break;
      case "html":
        fail(node, "contains raw HTML");
        break;
      case "link":
      case "image":
      case "definition":
        validateUrlNode(node);
        break;
      case "text":
        if (containsDangerousUrl(node.value)) {
          fail(node, "contains a dangerous URL scheme");
        }
        break;
    }
  });
}

function parseMdx(content) {
  const processor = unified()
    .use(remarkParse)
    .use(remarkMdx)
    .use(remarkFrontmatter)
    .use(remarkGfm);
  return processor.parse(content);
}

export async function lintMdxContent(content) {
  if (!content.trim()) {
    throw new Error("documentation cannot be empty");
  }
  if (content.includes("\0")) {
    throw new Error("documentation contains NUL bytes");
  }
  if (content.includes("\ufeff")) {
    throw new Error("documentation contains byte order marks");
  }

  const tree = parseMdx(content);
  validateTree(tree);

  try {
    await compile(content, {
      outputFormat: "function-body",
      remarkPlugins: [remarkGfm, remarkFrontmatter],
    });
  } catch (err) {
    throw new Error(err.message);
  }
}

async function readStdin() {
  let content = "";
  for await (const chunk of process.stdin) {
    content += chunk;
  }
  return content;
}

async function main() {
  try {
    await lintMdxContent(await readStdin());
  } catch (err) {
    process.stderr.write(`mdx-lint: ${err.message}\n`);
    process.exit(1);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
