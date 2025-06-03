# Github Workflows

This repository contains shared GitHub workflows for ConductorOne connector repositories.

## Available Workflows

### Release Workflow

The release workflow handles the release process for connector repos, including:

- Rendering the latest goreleaser and gon configuration files from template
- Building binaries for multiple platforms
- Creating GitHub releases
- Building and pushing Docker images
- Building and pushing ECR images
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

3. Remove all GoReleaser and gon files from your repository, if they were previously created there.

#### Input Parameters

The release workflow accepts the following input parameters:

| Parameter | Required | Description                      |
| --------- | -------- | -------------------------------- |
| `tag`     | Yes      | The release tag (e.g., "v1.0.0") |

## Development

To modify these workflows:

1. Make your changes in this repository
2. Test the changes in a connector repository _pointing at your branch_
3. Create a pull request for review
4. Once approved, merge to main
5. Tag the release: `git tag v1.1.0`
6. Push the tag: `git push origin v1.1.0`
7. Update the major version tag `git tag -f v1 v1.1.0`
8. Push the major version tag `git push origin v1 --force`

## Versioning

The workflows are versioned using Git tags. When testing a new version of the workflows in your repository, you can specify a specific version:

```yaml
uses: ConductorOne/github-workflows/.github/workflows/release.yaml@my-branch
```

Github does not resolve semantic versioning - tags must match exactly.  The major version must _float_.
