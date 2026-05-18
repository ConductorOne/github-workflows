import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { lintMdxContent } from "./mdx-lint.mjs";

async function expectValid(content) {
  await assert.doesNotReject(() => lintMdxContent(content));
}

async function expectInvalid(content, pattern) {
  await assert.rejects(() => lintMdxContent(content), pattern);
}

describe("mdx-lint policy", () => {
  it("allows supported connector docs content", async () => {
    await expectValid(`---
title: Example Connector
---

# Example Connector

<Info>Use this connector to sync users and groups.</Info>

<Steps>
  <Step title="Create an API token">
    Create a read-only token.
  </Step>
</Steps>

<Check />

\`\`\`json
{"tenant": "{tenant}", "url": "data:image/png;base64,abc"}
\`\`\`

Inline placeholders like \`<org name>\` and \`{tenant}\` are allowed.
`);
  });

  it("allows normal prose and safe links", async () => {
    await expectValid(`This connector syncs users and groups.

Read the public setup guide at https://example.com/docs.
`);
  });

  it("rejects unknown JSX components", async () => {
    await expectInvalid("<Unknown />\n", /disallowed JSX component "Unknown"/);
  });

  it("rejects MDX imports and exports", async () => {
    await expectInvalid('import Thing from "./thing.js"\n', /MDX import\/export/);
    await expectInvalid("export const value = 1\n", /MDX import\/export/);
  });

  it("rejects MDX expressions", async () => {
    await expectInvalid("{process.env.SECRET}\n", /MDX expression/);
  });

  it("rejects raw HTML elements", async () => {
    await expectInvalid("<div>raw html</div>\n", /disallowed JSX component "div"/);
  });

  it("rejects event handler attributes", async () => {
    await expectInvalid('<Card onClick="alert(1)" />\n', /event handler attribute/);
  });

  it("rejects encoded dangerous URLs", async () => {
    await expectInvalid(
      "[link](java&#x73;cript:alert(1))\n",
      /dangerous URL scheme/,
    );
  });

  it("rejects data URLs", async () => {
    await expectInvalid(
      "[image](data:image/png;base64,abc)\n",
      /dangerous URL scheme/,
    );
  });

  it("rejects BOM and NUL bytes", async () => {
    await expectInvalid("\ufeff# Title\n", /byte order marks/);
    await expectInvalid("hello\0world\n", /NUL bytes/);
  });
});
