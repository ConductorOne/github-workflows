# Github Workflows

This repository contains shared GitHub Actions workflows for ConductorOne connectors.

## Available Workflows

### Release Workflow

The release workflow handles the release process for connectors, including:

- Building binaries for multiple platforms
- Creating GitHub releases
- Building and pushing Docker images
- Recording releases in the release tracking system

#### Usage

To use the release workflow in your connector repository:

1. Create a `.github/workflows/release.yaml` file with the following content:

```yaml
name: Release

on:
  push:
    tags:
      - "*"

jobs:
  release:
    uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v1
    with:
      tag: ${{ github.ref_name }}
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
      APPLE_SIGNING_KEY_P12: ${{ secrets.APPLE_SIGNING_KEY_P12 }}
      APPLE_SIGNING_KEY_P12_PASSWORD: ${{ secrets.APPLE_SIGNING_KEY_P12_PASSWORD }}
      AC_PASSWORD: ${{ secrets.AC_PASSWORD }}
      AC_PROVIDER: ${{ secrets.AC_PROVIDER }}
```

2. Ensure your repository has the following secrets configured:

   - `RELENG_GITHUB_TOKEN`: A GitHub token with permissions to create releases
   - `APPLE_SIGNING_KEY_P12`: Base64-encoded Apple signing key
   - `APPLE_SIGNING_KEY_P12_PASSWORD`: Password for the Apple signing key
   - `AC_PASSWORD`: Apple Connect password
   - `AC_PROVIDER`: Apple Connect provider

3. Configure your GoReleaser files:
   - `.goreleaser.yaml` for binary releases
   - `.goreleaser.docker.yaml` for Docker releases

#### Input Parameters

The release workflow accepts the following input parameters:

| Parameter | Required | Description                      |
| --------- | -------- | -------------------------------- |
| `tag`     | Yes      | The release tag (e.g., "v1.0.0") |

## Development

To modify these workflows:

1. Make your changes in this repository
2. Test the changes in a connector repository
3. Create a pull request for review
4. Once approved, merge to main

## Versioning

The workflows are versioned using Git tags. When using the workflows in your repository, you can specify a specific version:

```yaml
uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v1.0.0
```

Or use the latest version from a branch:

```yaml
uses: ConductorOne/github-workflows/.github/workflows/release.yaml@main
```
