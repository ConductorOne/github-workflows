#!/usr/bin/env node

import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const CONNECTOR_DOCS_PATH = "docs/connector.mdx";
const PER_PAGE = 100;
const PAGE_LIMIT = 30;

function normalizeApiUrl(apiUrl) {
  return apiUrl.replace(/\/+$/, "");
}

function docsPathChanged(file) {
  return (
    file?.filename === CONNECTOR_DOCS_PATH ||
    file?.previous_filename === CONNECTOR_DOCS_PATH
  );
}

function writeOutput(name, value) {
  const line = `${name}=${value}\n`;
  if (process.env.GITHUB_OUTPUT) {
    appendFileSync(process.env.GITHUB_OUTPUT, line);
  } else {
    process.stdout.write(line);
  }
}

export async function checkConnectorDocsChange({
  apiUrl = "https://api.github.com",
  fetchFn = fetch,
  prNumber,
  repository,
  token,
} = {}) {
  if (!prNumber) {
    return { validate: "unknown", reason: "not_pull_request" };
  }
  if (!repository) {
    throw new Error("repository is required when prNumber is set");
  }

  for (let page = 1; ; page += 1) {
    const url =
      `${normalizeApiUrl(apiUrl)}/repos/${repository}/pulls/${prNumber}/files` +
      `?per_page=${PER_PAGE}&page=${page}`;
    const headers = {
      Accept: "application/vnd.github+json",
    };
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const response = await fetchFn(url, { headers });
    if (!response.ok) {
      throw new Error(
        `GitHub PR files request failed: ${response.status} ${response.statusText}`,
      );
    }

    const files = await response.json();
    if (!Array.isArray(files)) {
      throw new Error("GitHub PR files response was not an array");
    }

    if (files.some(docsPathChanged)) {
      return { validate: "true", reason: "docs_path_changed" };
    }

    // GitHub's PR files API is capped at 3000 files. If the final reachable
    // page is full, the file list might be incomplete; force validation rather
    // than proving docs/connector.mdx unchanged from partial data.
    if (page >= PAGE_LIMIT && files.length === PER_PAGE) {
      return { validate: "true", reason: "api_cap" };
    }

    if (files.length < PER_PAGE) {
      return { validate: "false", reason: "docs_path_unchanged" };
    }
  }
}

async function main() {
  const result = await checkConnectorDocsChange({
    apiUrl: process.env.GITHUB_API_URL || "https://api.github.com",
    prNumber: process.env.PR_NUMBER || "",
    repository: process.env.REPOSITORY || process.env.GITHUB_REPOSITORY || "",
    token: process.env.GITHUB_TOKEN || process.env.GH_TOKEN || "",
  });

  writeOutput("validate", result.validate);
  if (result.reason === "docs_path_unchanged") {
    process.stdout.write(`${CONNECTOR_DOCS_PATH} unchanged; skipping MDX validation\n`);
  }
  if (result.reason === "api_cap") {
    process.stdout.write(
      "::warning::PR file list reached the GitHub API cap; validating " +
        `${CONNECTOR_DOCS_PATH} because changed files cannot be proven complete.\n`,
    );
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    process.stderr.write(`${err.message}\n`);
    process.exit(1);
  });
}
