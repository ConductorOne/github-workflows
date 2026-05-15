# Connector Docs Verify Workflow

The `connector-docs-verify.yaml` workflow runs a standalone
`docs/connector.mdx` validation check for connector repositories.

## Overview

This workflow is meant to be safe as a required status check on every connector
pull request:

1. It checks out the caller repository at the requested ref.
2. It checks whether `docs/connector.mdx` changed in the pull request.
3. If the file is unchanged, the job exits successfully.
4. If the file changed, the job validates that the file still exists and passes
   MDX safety checks.

The required check context is stable when the caller job id is
`connector-docs`:

```text
connector-docs / validate
```

## MDX Checks

The validator compiles MDX without evaluating PR content and rejects unsafe
constructs before compilation:

- empty documentation
- NUL bytes or byte-order marks
- unclosed code fences
- MDX imports or exports outside code fences
- MDX expression braces outside code fences
- event-handler attributes
- dangerous URL schemes after simple entity decoding
- unsupported JSX components

Allowed JSX components:

- `Card`
- `Check`
- `Frame`
- `Icon`
- `Info`
- `Note`
- `Step`
- `Steps`
- `Tab`
- `Tabs`
- `Tip`
- `Warning`

Keep the allowlist aligned with the connector registry renderer and server-side
documentation validators.

## Usage

```yaml
name: Connector Docs

on:
  pull_request:
    types: [opened, reopened, synchronize]
  push:
    branches:
      - main

jobs:
  connector-docs:
    uses: ConductorOne/github-workflows/.github/workflows/connector-docs-verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
```

## Rollout Notes

Do not add workflow-level `paths` filters to the caller workflow when this
check is required by branch rules. A required check that never runs leaves pull
requests stuck waiting.

The workflow itself handles the path check and reports success when
`docs/connector.mdx` is unchanged.

Use a staged rollout:

1. Merge this workflow and update the shared workflow ref used by connector
   repos, such as the `v4` tag, so callers can resolve
   `connector-docs-verify.yaml`.
2. Add the caller workflow to connector repos without requiring the status
   check yet.
3. Confirm `connector-docs / validate` appears and passes on both docs and
   non-doc pull requests.
4. Require `connector-docs / validate` in branch rules only after the check is
   present on targeted repos.

The existing `verify.yaml` docs job is a compatibility check. Use this
standalone workflow as the required docs safety gate.
