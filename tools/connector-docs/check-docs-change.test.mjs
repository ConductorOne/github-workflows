import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { checkConnectorDocsChange } from "./check-docs-change.mjs";

function mockFetch(pages) {
  const calls = [];
  const fetchFn = async (url, options) => {
    calls.push({ url, options });
    const files = pages[calls.length - 1] ?? [];
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      async json() {
        return files;
      },
    };
  };
  fetchFn.calls = calls;
  return fetchFn;
}

describe("checkConnectorDocsChange", () => {
  it("returns unknown outside pull requests", async () => {
    const result = await checkConnectorDocsChange();
    assert.deepEqual(result, { validate: "unknown", reason: "not_pull_request" });
  });

  it("returns false when connector docs are unchanged", async () => {
    const fetchFn = mockFetch([[{ filename: "README.md" }]]);
    const result = await checkConnectorDocsChange({
      fetchFn,
      prNumber: "12",
      repository: "example/repo",
      token: "token",
    });
    assert.deepEqual(result, {
      validate: "false",
      reason: "docs_path_unchanged",
    });
    assert.equal(fetchFn.calls.length, 1);
    assert.equal(fetchFn.calls[0].options.headers.Authorization, "Bearer token");
  });

  it("detects direct connector docs changes", async () => {
    const fetchFn = mockFetch([[{ filename: "docs/connector.mdx" }]]);
    const result = await checkConnectorDocsChange({
      fetchFn,
      prNumber: "12",
      repository: "example/repo",
    });
    assert.deepEqual(result, { validate: "true", reason: "docs_path_changed" });
  });

  it("detects connector docs renames", async () => {
    const fetchFn = mockFetch([
      [{ filename: "docs/old-connector.mdx", previous_filename: "docs/connector.mdx" }],
    ]);
    const result = await checkConnectorDocsChange({
      fetchFn,
      prNumber: "12",
      repository: "example/repo",
    });
    assert.deepEqual(result, { validate: "true", reason: "docs_path_changed" });
  });

  it("forces validation at the GitHub PR files API cap", async () => {
    const fullPage = Array.from({ length: 100 }, (_, index) => ({
      filename: `generated/file-${index}.txt`,
    }));
    const fetchFn = mockFetch(Array.from({ length: 30 }, () => fullPage));
    const result = await checkConnectorDocsChange({
      fetchFn,
      prNumber: "12",
      repository: "example/repo",
    });
    assert.deepEqual(result, { validate: "true", reason: "api_cap" });
    assert.equal(fetchFn.calls.length, 30);
  });

  it("retries transient GitHub API errors", async () => {
    const calls = [];
    const fetchFn = async () => {
      calls.push(null);
      if (calls.length === 1) {
        return {
          ok: false,
          status: 404,
          statusText: "Not Found",
        };
      }
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        async json() {
          return [{ filename: "README.md" }];
        },
      };
    };
    const result = await checkConnectorDocsChange({
      fetchFn,
      maxAttempts: 3,
      prNumber: "12",
      repository: "example/repo",
      retryDelayMs: 0,
      sleepFn: async () => {},
    });
    assert.deepEqual(result, {
      validate: "false",
      reason: "docs_path_unchanged",
    });
    assert.equal(calls.length, 2);
  });

  it("does not retry authorization-style GitHub API errors", async () => {
    const calls = [];
    const fetchFn = async () => {
      calls.push(null);
      return {
        ok: false,
        status: 403,
        statusText: "Forbidden",
      };
    };
    await assert.rejects(
      () =>
        checkConnectorDocsChange({
          fetchFn,
          maxAttempts: 3,
          prNumber: "12",
          repository: "example/repo",
          retryDelayMs: 0,
          sleepFn: async () => {},
        }),
      /GitHub PR files request failed after 1 attempt\(s\): 403 Forbidden/,
    );
    assert.equal(calls.length, 1);
  });

  it("reports GitHub API errors with endpoint context", async () => {
    const fetchFn = async () => ({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
    });
    await assert.rejects(
      () =>
        checkConnectorDocsChange({
          fetchFn,
          maxAttempts: 2,
          prNumber: "12",
          repository: "example/repo",
          retryDelayMs: 0,
          sleepFn: async () => {},
        }),
      /GitHub PR files request failed after 2 attempt\(s\): 502 Bad Gateway. Endpoint: https:\/\/api.github.com\/repos\/example\/repo\/pulls\/12\/files\?per_page=100&page=1/,
    );
  });
});
