#!/usr/bin/env node
//
// MDX-to-HTML compiler for connector documentation.
//
// Reads MDX from stdin, compiles it to static HTML using the same Mintlify
// component mappings as the registry UI, writes HTML to stdout.
//
// Usage:
//   cat docs/connector.mdx | node ui/mdx-compile.mjs
//   echo "$MDX_CONTENT" | node ui/mdx-compile.mjs
//
// Exit codes:
//   0 - success (HTML on stdout)
//   1 - compilation error (message on stderr)

import { compile, run } from "@mdx-js/mdx";
import * as runtime from "react/jsx-runtime";
import React from "react";
import ReactDOMServer from "react-dom/server";
import remarkGfm from "remark-gfm";
import remarkFrontmatter from "remark-frontmatter";

// ── Mintlify component mappings (HTML equivalents) ──────────────────

function Tip({ children }) {
  return React.createElement(
    "div",
    { className: "mdx-alert mdx-alert-tip" },
    React.createElement("div", { className: "mdx-alert-icon" }, "\u2139\uFE0F"),
    React.createElement("div", { className: "mdx-alert-content" }, children),
  );
}

function Warning({ children }) {
  return React.createElement(
    "div",
    { className: "mdx-alert mdx-alert-warning" },
    React.createElement("div", { className: "mdx-alert-icon" }, "\u26A0\uFE0F"),
    React.createElement("div", { className: "mdx-alert-content" }, children),
  );
}

function Note({ children }) {
  return React.createElement(
    "div",
    { className: "mdx-alert mdx-alert-note" },
    React.createElement("div", { className: "mdx-alert-icon" }, "\u2139\uFE0F"),
    React.createElement("div", { className: "mdx-alert-content" }, children),
  );
}

function Icon({ icon, color }) {
  if (icon === "square-check") {
    return React.createElement(
      "span",
      {
        className: "mdx-icon mdx-icon-check",
        style: { color: color || "#4caf50" },
      },
      "\u2611",
    );
  }
  return null;
}

function Frame({ children, caption }) {
  return React.createElement(
    "div",
    { className: "mdx-frame" },
    children,
    caption
      ? React.createElement(
          "div",
          { className: "mdx-frame-caption" },
          caption,
        )
      : null,
  );
}

function Card({ children, title }) {
  return React.createElement(
    "div",
    { className: "mdx-card" },
    title ? React.createElement("h4", null, title) : null,
    children,
  );
}

function Tabs({ children }) {
  const childArray = Array.isArray(children) ? children : [children];
  const tabs = childArray.filter((c) => c?.props?.title);

  if (tabs.length === 0) return React.createElement(React.Fragment, null, children);

  return React.createElement(
    "div",
    { className: "mdx-tabs" },
    React.createElement(
      "div",
      { className: "mdx-tabs-nav", role: "tablist" },
      tabs.map((tab, i) =>
        React.createElement(
          "button",
          {
            key: i,
            className: `mdx-tab-btn${i === 0 ? " mdx-tab-active" : ""}`,
            "data-tab-index": i,
            role: "tab",
            type: "button",
          },
          tab.props.title,
        ),
      ),
    ),
    tabs.map((tab, i) =>
      React.createElement(
        "div",
        {
          key: i,
          className: `mdx-tab-panel${i === 0 ? " mdx-tab-visible" : ""}`,
          "data-tab-index": i,
          role: "tabpanel",
        },
        tab.props.children,
      ),
    ),
  );
}

function Tab({ children }) {
  return React.createElement(React.Fragment, null, children);
}

function Steps({ children }) {
  const childArray = Array.isArray(children) ? children : [children];
  const steps = childArray.filter((c) => c?.props);
  return React.createElement(
    "ol",
    { className: "mdx-steps" },
    steps.map((child, i) =>
      React.createElement(
        "li",
        { key: i, className: "mdx-step" },
        child?.props?.children,
      ),
    ),
  );
}

function Step({ children }) {
  return React.createElement(React.Fragment, null, children);
}

// ── Component map ───────────────────────────────────────────────────

const components = {
  Tip,
  Warning,
  Note,
  Icon,
  Frame,
  Card,
  Tabs,
  Tab,
  Steps,
  Step,
};

// ── Main ────────────────────────────────────────────────────────────

async function main() {
  let content = "";
  for await (const chunk of process.stdin) {
    content += chunk;
  }

  if (!content.trim()) {
    process.exit(0);
  }

  try {
    const compiled = await compile(content, {
      outputFormat: "function-body",
      remarkPlugins: [remarkGfm, remarkFrontmatter],
    });

    const { default: MDXContent } = await run(String(compiled), {
      ...runtime,
      baseUrl: "file:///",
    });

    const html = ReactDOMServer.renderToStaticMarkup(
      React.createElement(MDXContent, { components }),
    );

    process.stdout.write(html);
  } catch (err) {
    process.stderr.write(`mdx-compile: ${err.message}\n`);
    process.exit(1);
  }
}

main();
