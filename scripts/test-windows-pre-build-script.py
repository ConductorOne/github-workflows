#!/usr/bin/env python3
"""Exercise the release workflow's Windows renderer with real bash/envsubst/yq."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/release.yaml"
BASELINE = ROOT / "scripts/testdata/goreleaser-windows-default.yaml"
WINDOWS_JOB = '.jobs."goreleaser-windows"'
SCRIPT_PATH = "scripts/release-windows-resources.sh"


def query(expression: str, path: Path = WORKFLOW) -> str:
    return subprocess.check_output(["yq", "-r", expression, str(path)], text=True).strip()


def step_field(name: str, field: str) -> str:
    return query(f'{WINDOWS_JOB}.steps[] | select(.name == "{name}").{field}')


def verify_forwarding() -> None:
    input_path = '.on.workflow_call.inputs.windows_pre_build_script'
    assert query(f"{input_path}.type") == "string"
    assert query(f"{input_path}.required") == "false"
    assert query(f"{input_path}.default") == ""
    assert step_field("Generate configs for Windows", "env.WINDOWS_PRE_BUILD_SCRIPT") == "${{ inputs.windows_pre_build_script }}"
    assert step_field("Run GoReleaser for Windows", "env.RELEASE_TAG") == "${{ inputs.tag }}"
    assert step_field("Run GoReleaser for Windows", "with.workdir") == "_caller"
    assert step_field("Run GoReleaser for Windows", "with.args") == "release --clean --skip=publish --config ../_workflows/_generated/.goreleaser.windows.yaml"
    # No pre-build opt-in or RELEASE_TAG forwarding on the other GoReleaser actions.
    for job, name in (
        ("goreleaser-binaries", "Run GoReleaser"),
        ("goreleaser-docker", "Run GoReleaser for Docker OCI"),
        ("goreleaser-docker", "Run GoReleaser for Lambda"),
    ):
        assert query(f'.jobs."{job}".steps[] | select(.name == "{name}").env.RELEASE_TAG') == "null"
        assert query(f'.jobs."{job}".steps[] | select(.name == "{name}").env.WINDOWS_PRE_BUILD_SCRIPT') == "null"


def verify_rendering() -> None:
    render_command = step_field("Generate configs for Windows", "run")
    validate_command = query('.jobs."determine-workflows-ref".steps[] | select(.name == "Validate windows_pre_build_script has safe path").run')
    assert query('.jobs."determine-workflows-ref".steps[] | select(.name == "Validate windows_pre_build_script has safe path").env.WINDOWS_PRE_BUILD_SCRIPT') == "${{ inputs.windows_pre_build_script }}"
    assert query('.jobs."determine-workflows-ref".steps[] | select(.name == "Validate windows_pre_build_script has safe path").if') == "inputs.windows_pre_build_script != ''"

    with tempfile.TemporaryDirectory(prefix="windows-pre-build-") as temporary:
        workspace = Path(temporary)
        caller = workspace / "_caller"
        workflows = workspace / "_workflows"
        caller.mkdir()
        workflows.mkdir()
        (workflows / "_generated").mkdir()
        (workflows / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        (workflows / "templates").symlink_to(ROOT / "templates", target_is_directory=True)
        generated = workflows / "_generated/.goreleaser.windows.yaml"
        env = {
            **os.environ,
            "REPO_NAME": "bridge-client",
            "GO_MAIN_PACKAGE": "./cmd/bridge-client",
            "WXS_PATH": "app.wxs",
            "WORKFLOWS_REF": "fixture-workflow-sha",
            "RELEASE_TAG": "v1.2.3-rc.1+build.5",
            "HOOK_EXIT_CODE": "0",
            # The renderer must reset, not inherit, a previous hooks block.
            "WINDOWS_BEFORE_HOOKS_BLOCK": "unexpected inherited hooks",
        }

        def render(script: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                ["bash", "-c", render_command],
                cwd=workflows,
                env={**env, "WINDOWS_PRE_BUILD_SCRIPT": script},
                capture_output=True,
            )

        def validate(script: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                ["bash", "-c", validate_command],
                cwd=workflows,
                env={**env, "WINDOWS_PRE_BUILD_SCRIPT": script},
                capture_output=True,
            )

        # Frozen, pre-hook rendering: compare bytes, including all newlines.
        result = render("")
        assert result.returncode == 0, result.stderr
        assert generated.read_bytes() == BASELINE.read_bytes(), "empty input changed default Windows config bytes"
        assert query(".before", generated) == "null"

        script = caller / SCRIPT_PATH
        script.parent.mkdir()
        script.write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            'printf "%s\\n" "$RELEASE_TAG" > generated-version\n'
            'exit "${HOOK_EXIT_CODE:-0}"\n'
        )
        assert validate(SCRIPT_PATH).returncode == 0
        result = render(SCRIPT_PATH)
        assert result.returncode == 0, result.stderr
        block = f'before:\n  hooks:\n    - "bash ./{SCRIPT_PATH}"\n'.encode()
        assert generated.read_bytes() == BASELINE.read_bytes().replace(b"builds:\n", block + b"builds:\n", 1)
        hook = query(".before.hooks[0]", generated)
        assert hook == f"bash ./{SCRIPT_PATH}"
        marker = caller / "generated-version"
        assert not marker.exists(), "rendering must not execute the caller script"
        result = subprocess.run(shlex.split(hook), cwd=caller, env=env, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == env["RELEASE_TAG"] + "\n"
        result = subprocess.run(shlex.split(hook), cwd=caller, env={**env, "HOOK_EXIT_CODE": "17"}, capture_output=True)
        assert result.returncode == 17, "hook command must preserve caller failure"

        # A missing script or directory fails before config generation.
        for path in ("scripts/missing.sh", "scripts"):
            assert validate(path).returncode == 0
            result = render(path)
            assert result.returncode != 0, path
            assert b"Windows pre-build script not found" in result.stderr

        for path in (
            "/absolute.sh", "../outside.sh", "scripts/../hook.sh", "scripts/foo..sh",
            "C:/hook.sh", "scripts\\hook.sh", "scripts/a b.sh", "scripts/a\tb.sh",
            "scripts/a\nb.sh", "scripts/hook.sh;echo", "$(touch injected)",
            'scripts/quote".sh', "scripts/{{template}}.sh",
        ):
            assert validate(path).returncode != 0, path
            result = render(path)
            assert result.returncode != 0, path
            assert b"relative path with safe characters" in result.stderr

        # Existing ../_caller files must not cause a default consumer to opt in.
        result = render("")
        assert result.returncode == 0, result.stderr
        assert generated.read_bytes() == BASELINE.read_bytes()


def main() -> int:
    verify_forwarding()
    verify_rendering()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as err:
        print(f"test-windows-pre-build-script: {err}", file=sys.stderr)
        raise SystemExit(1)
