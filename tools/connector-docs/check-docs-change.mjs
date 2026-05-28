#!/usr/bin/env node

import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const CONNECTOR_DOCS_PATH = "docs/connector.mdx";
const PER_PAGE = 100;
const PAGE_LIMIT = 30;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_RETRY_DELAY_MS = 1000;

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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function retryLimitForStatus(status, maxAttempts) {
  if (status === 404) {
    return Math.min(maxAttempts, 2);
  }
  if (status === 408 || status === 429 || status >= 500) {
    return maxAttempts;
  }
  return 1;
}

function retryDelay(response, fallbackDelayMs, attempt) {
  const retryAfter = response.headers?.get?.("retry-after");
  if (!retryAfter) {
    return fallbackDelayMs * attempt;
  }

  const seconds = Number.parseInt(retryAfter, 10);
  if (Number.isFinite(seconds)) {
    return seconds * 1000;
  }

  const retryAt = Date.parse(retryAfter);
  if (Number.isFinite(retryAt)) {
    return Math.max(0, retryAt - Date.now());
  }

  return fallbackDelayMs * attempt;
}

function requestId(response) {
  return response.headers?.get?.("x-github-request-id") || "unavailable";
}

async function fetchWithRetry({
  fetchFn,
  url,
  headers,
  maxAttempts,
  retryDelayMs,
  sleepFn,
}) {
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const response = await fetchFn(url, { headers });
    const retryLimit = retryLimitForStatus(response.status, maxAttempts);
    if (response.ok || attempt >= retryLimit) {
      return { attempts: attempt, response };
    }

    process.stderr.write(
      `GitHub PR files request returned ${response.status} ${response.statusText}; ` +
        `retrying (${attempt}/${retryLimit}). ` +
        `Request ID: ${requestId(response)}.\n`,
    );
    await sleepFn(retryDelay(response, retryDelayMs, attempt));
  }
}

export async function checkConnectorDocsChange({
  apiUrl = "https://api.github.com",
  fetchFn = fetch,
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  prNumber,
  repository,
  retryDelayMs = DEFAULT_RETRY_DELAY_MS,
  sleepFn = sleep,
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

    const { attempts, response } = await fetchWithRetry({
      fetchFn,
      url,
      headers,
      maxAttempts,
      retryDelayMs,
      sleepFn,
    });
    if (!response.ok) {
      throw new Error(
        `GitHub PR files request failed after ${attempts} attempt(s): ` +
          `${response.status} ${response.statusText}. ` +
          `Endpoint: ${url}. Repository: ${repository}. PR: ${prNumber}. ` +
          `Request ID: ${requestId(response)}.`,
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
