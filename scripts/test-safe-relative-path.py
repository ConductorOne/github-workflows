#!/usr/bin/env python3
"""Compare the consolidated path checks with the pre-refactor Bash behavior."""

import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/release.yaml"
JOB = '.jobs."determine-workflows-ref"'
HELPER = ROOT / "scripts/validate-safe-relative-path.sh"
# Frozen pre-refactor predicate, intentionally independent of the production helper.
LEGACY = r'''if [[ "$1" == /* ]] || [[ "$1" == *".."* ]] || [[ ! "$1" =~ ^[A-Za-z0-9._/-]+$ ]]; then exit 1; fi'''
CASES = (
    "", "a", "A-Z_09/file.sh", "-script.sh", ".", "./script.sh", "scripts/", "a//b",
    "/", "/absolute.sh", "//server/share", "..", "../file", "a/../file", "a..b",
    "C:/file", r"scripts\file.sh", "a b", "a\tb", "a\nb", "a\rb", "a;b", "a&b",
    "$(touch injected)", "a|b", "a'b", 'a"b', "a`b", "a*b", "a?b", "a[b", "a:b",
    "a=b", "a#b", "a%b", "a+b", "café.sh", "日本語.sh", "scripts/hook.sh",
)


def query(expression):
    return subprocess.check_output(["yq", "-r", expression, str(WORKFLOW)], text=True).strip()


def run(args, env=None, cwd=ROOT):
    result = subprocess.run(args, env={**os.environ, **(env or {})}, cwd=cwd, capture_output=True)
    return result.returncode, result.stdout, result.stderr


def step(name, field):
    return query(f'{JOB}.steps[] | select(.name == "{name}").{field}')


def test_predicate_and_workflow_sites():
    names = query(f"{JOB}.steps[].name").splitlines()
    checkout = names.index("Checkout pinned connector workflows")
    normalize = names.index("Normalize release options")
    assert query(f"{JOB}.needs") == "validate-inputs"
    assert query(f"{JOB}.permissions.actions") == "read"
    assert query(f"{JOB}.permissions.contents") == "read"
    assert query(f'{JOB}.steps[] | select(.name == "Checkout pinned connector workflows").with.ref') == "${{ job.workflow_sha }}"
    sites = (
        ("Validate dockerfile_template has safe path", "DOCKERFILE_TEMPLATE", "dockerfile_template"),
        ("Validate msi_wxs_path has no path traversal", "WXS_PATH", "msi_wxs_path"),
        ("Validate windows_pre_build_script has safe path", "WINDOWS_PRE_BUILD_SCRIPT", "windows_pre_build_script"),
    )
    for name, variable, input_name in sites:
        assert checkout < names.index(name) < normalize
        assert step(name, '"working-directory"') == "_workflows"
        assert step(name, "if") == f"inputs.{input_name} != ''"
        assert step(name, f"env.{variable}") == "${{ inputs." + input_name + " }}"
        command = step(name, "run")
        for candidate in CASES:
            legacy = run(["bash", "-c", LEGACY, "legacy", candidate])
            assert run(["bash", str(HELPER), candidate]) == legacy, repr(candidate)
            invalid = legacy[0] != 0
            message = (f"::error::{input_name} must be a relative path with safe characters "
                       f"and without '..' traversal. Got: {candidate}\n").encode()
            expected = (1, message, b"") if invalid else (0, b"", b"")
            assert run(["bash", "-c", command], {variable: candidate}) == expected, (name, repr(candidate))


def test_extra_files_preserves_trimming_and_diagnostics():
    name = "Validate docker_extra_files have safe paths"
    assert step(name, '"working-directory"') == "_workflows"
    assert step(name, "if") == "inputs.docker_extra_files != ''"
    command = step(name, "run")
    legacy = r'''IFS=',' read -ra FILES <<< "$DOCKER_EXTRA_FILES"
for file in "${FILES[@]}"; do
  file="${file#"${file%%[![:space:]]*}"}"
  file="${file%"${file##*[![:space:]]}"}"
  if [[ -z "$file" ]] || [[ "$file" == /* ]] || [[ "$file" == *".."* ]] || [[ ! "$file" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "::error::docker_extra_files entries must be relative paths with safe characters and without '..' traversal. Got: $file"
    exit 1
  fi
done'''
    values = CASES + (" a , b ", "a,,b", "a,b,", ",a", " ", "a, /bad", "a, ../bad", "a,\tb\t")
    for candidate in values:
        env = {"DOCKER_EXTRA_FILES": candidate}
        assert run(["bash", "-c", command], env) == run(["bash", "-c", legacy], env), repr(candidate)


def test_renderer_preserves_error_streams_and_default_bytes():
    baseline = (ROOT / "scripts/testdata/goreleaser-windows-default.yaml").read_bytes()
    renderer = ROOT / "scripts/render-goreleaser-windows-config.sh"
    with tempfile.TemporaryDirectory(prefix="safe-path-render-") as temporary:
        caller = Path(temporary)
        (caller / "scripts").mkdir()
        (caller / "scripts/hook.sh").write_text("exit 0\n")
        env = {"REPO_NAME": "bridge-client", "GO_MAIN_PACKAGE": "./cmd/bridge-client", "WXS_PATH": "app.wxs"}
        for candidate in CASES:
            actual = run(["bash", str(renderer), candidate, str(caller)], env)
            if not candidate:
                expected = (0, baseline, b"")
            elif run(["bash", "-c", LEGACY, "legacy", candidate])[0]:
                expected = (1, b"", ("::error::windows_pre_build_script must be a relative path with safe characters "
                                      f"and without '..' traversal. Got: {candidate}\n").encode())
            elif not (caller / candidate).is_file():
                expected = (1, b"", f"::error::Windows pre-build script not found: {candidate}\n".encode())
            else:
                block = f'before:\n  hooks:\n    - "bash ./{candidate}"\n'.encode()
                expected = (0, baseline.replace(b"builds:\n", block + b"builds:\n", 1), b"")
            assert actual == expected, repr(candidate)


if __name__ == "__main__":
    test_predicate_and_workflow_sites()
    test_extra_files_preserves_trimming_and_diagnostics()
    test_renderer_preserves_error_streams_and_default_bytes()
    print("shared path predicates, caller diagnostics, and default config bytes unchanged")
