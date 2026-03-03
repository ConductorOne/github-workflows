# Verify Workflow

The `verify.yaml` workflow runs linting, tests, and optional regression verification for connector repositories.

## Overview

When a pull request is opened or code is pushed to main, the shared verify workflow:

1. Runs `golangci-lint` on the connector code
2. Runs `go test` (optional, enabled by default)
3. Runs baton-regression verification (optional, when `connector` is provided)

## Jobs

### lint

Checks out the caller repo and runs `golangci-lint` with a 6-minute timeout. If `RELENG_GITHUB_TOKEN` is available, configures git for private module access.

### test

Runs `go test -v -covermode=count -json ./...` and annotates results. Skipped if `run_tests: false`.

### regression

Runs the [baton-regression](https://github.com/ConductorOne/baton-regression) verification when `connector` is non-empty. The workflow is hosted in this repo but checks out baton-regression source from main at runtime. The regression job:

1. Checks out baton-regression and the connector repo
2. Builds both the regression tool and the connector binary
3. Runs axiom-based structural verification
4. Runs static nil pointer analysis
5. Uploads verification reports as artifacts
6. Posts a summary with coverage metrics

The regression job requires `RELENG_GITHUB_TOKEN` to be passed from the caller workflow to check out the private baton-regression repo.

## Inputs

| Parameter | Required | Default | Description |
|-|-|-|-|
| `ref` | Yes | - | Git ref to check out |
| `run_tests` | No | `true` | Whether to run `go test` |
| `connector` | No | `""` | Connector name (e.g., `baton-okta`). Triggers regression when set |

## Secrets

| Secret | Required | Description |
|-|-|-|
| `RELENG_GITHUB_TOKEN` | No | GitHub token for private module and repo access |

## Usage

### Basic (lint + test only)

```yaml
name: Verify

on:
  pull_request:
    types: [opened, reopened, synchronize]
  push:
    branches:
      - main

jobs:
  verify:
    uses: ConductorOne/github-workflows/.github/workflows/verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
```

### With regression testing

```yaml
jobs:
  verify:
    uses: ConductorOne/github-workflows/.github/workflows/verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
      connector: baton-okta
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
```

### Skip tests

```yaml
jobs:
  verify:
    uses: ConductorOne/github-workflows/.github/workflows/verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
      run_tests: false
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
```

## Controlling Regression per Connector

Regression is enabled when the connector's `verify.yaml` includes a `connector:` parameter. This is controlled by baton-admin's `connectors.yaml`:

- `run_regression: false` in a connector's verify config omits the `connector:` parameter, disabling regression
- When `run_regression` is absent (default), the `connector:` parameter is included and regression runs

To add a connector to regression testing, ensure it passes baton-regression verification locally before removing the `run_regression: false` flag.
