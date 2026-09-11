#!/usr/bin/env python3
"""Verify shared GoReleaser templates consume normalized release options."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = {
    "binaries": ROOT / "templates/.goreleaser-binaries-template.yaml.tmpl",
    "linux": ROOT / "templates/.goreleaser-linux-template.yaml.tmpl",
    "windows": ROOT / "templates/.goreleaser-windows-template.yaml.tmpl",
    "oci": ROOT / "templates/.goreleaser-docker-oci-template.yaml.tmpl",
    "lambda": ROOT / "templates/.goreleaser-docker-lambda-template.yaml.tmpl",
}
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def render(path: Path, values: dict[str, str]) -> str:
    source = path.read_text()

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in values:
            raise AssertionError(f"{path.name} references unprovided variable {name}")
        return values[name]

    return VARIABLE.sub(substitute, source)


def assert_contains(rendered: str, needle: str, label: str) -> None:
    if needle not in rendered:
        raise AssertionError(f"{label} missing {needle!r}")


def assert_main(rendered: str, package: str, count: int, label: str) -> None:
    needle = f'main: "{package}"'
    actual = rendered.count(needle)
    if actual != count:
        raise AssertionError(f"{label} has {actual} {needle!r} entries, want {count}")


def verify_case(go_main_package: str, brew_tap: str) -> None:
    values = {
        "REPO_NAME": "bridge-client",
        "GO_MAIN_PACKAGE": go_main_package,
        "WXS_PATH": "app.wxs",
        "BREW_TAP": brew_tap,
        "BREW_SKIP_UPLOAD": "false",
        "DIST_DIR": "dist/test",
        "DOCKERFILE_PATH": "Dockerfile",
        "DOCKERFILE_LAMBDA_PATH": "Dockerfile.lambda",
        "PUBLIC_ECR_PUBLISH_TAG": "candidate",
        "EXTRA_FILES_BLOCK": "",
    }
    rendered = {name: render(path, values) for name, path in TEMPLATES.items()}

    # The binaries template covers darwin amd64 + arm64; linux moved to its own template
    # so it can build on an ubuntu runner in parallel.
    assert_main(rendered["binaries"], go_main_package, 2, "binaries template")
    assert_main(rendered["linux"], go_main_package, 1, "linux template")
    assert_main(rendered["windows"], go_main_package, 1, "windows template")
    assert_main(rendered["oci"], go_main_package, 1, "OCI template")
    assert_main(rendered["lambda"], go_main_package, 1, "Lambda template")

    # Each build job must own a disjoint set of GOOS values, otherwise two jobs would
    # publish the same archive to the same immutable S3 key.
    assert_contains(rendered["binaries"], "- darwin", "binaries template")
    assert_contains(rendered["linux"], "- linux", "linux template")
    if "- linux" in rendered["binaries"]:
        raise AssertionError("binaries template still builds linux targets")
    if "- darwin" in rendered["linux"]:
        raise AssertionError("linux template must not build darwin targets")

    # The formula is rendered by cmd/generate-brew-formula from the merged manifest;
    # GoReleaser can no longer see every platform, so no template may declare brews.
    for name, text in rendered.items():
        if "brews:" in text:
            raise AssertionError(f"{name} template still declares a brews block")


def main() -> int:
    verify_case("./cmd/bridge-client", "homebrew-baton")
    verify_case("./", "homebrew-cone")

    for path in TEMPLATES.values():
        if "./cmd/${REPO_NAME}" in path.read_text():
            raise AssertionError(f"{path.name} retains an unparameterized Go main package")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as err:
        print(f"test-release-config-templates: {err}", file=sys.stderr)
        raise SystemExit(1)
