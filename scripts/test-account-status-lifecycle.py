#!/usr/bin/env python3
"""Prove lifecycle action return-code handling without live connector access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "actions/account-status-lifecycle-test/account-status-lifecycle-test.sh"

MOCK_CONNECTOR = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['FIXTURE_STATE'])
invocations = pathlib.Path(os.environ['FIXTURE_INVOCATIONS'])
if pathlib.Path(sys.argv[0]).name == 'baton':
    assert sys.argv[1:3] == ['resources', '-t']
    print(json.dumps({'resources': [{'resource': {'id': {'resource': 'fixture-user'},
                                                'status': {'status': state.read_text().strip()}}}]}))
    sys.exit(0)
for argument in sys.argv[1:]:
    if argument.startswith('--invoke-action='):
        action = argument.split('=', 1)[1]
        payload = next(value.split('=', 1)[1] for value in sys.argv[1:] if value.startswith('--invoke-action-args='))
        assert json.loads(payload) == {'user_id': 'fixture-user'}
        with invocations.open('a') as output:
            output.write(action + '\n')
        if action == os.environ.get('FIXTURE_FAIL_ACTION'):
            print('simulated connector failure')
            sys.exit(7)
        state.write_text('RESOURCE_STATUS_ENABLED' if action == 'enable_user' else 'RESOURCE_STATUS_DISABLED')
        print('fixture action completed')
'''


class LifecycleActionTest(unittest.TestCase):
    def run_flow(self, flow, initial_status, failed_action=""):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            state = directory / "state"
            state.write_text(initial_status)
            invocations = directory / "invocations"
            for name in ("baton", "connector"):
                executable = directory / name
                executable.write_text(MOCK_CONNECTOR)
                executable.chmod(0o755)
            environment = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}",
                               BATON_CONNECTOR=str(directory / "connector"), CONNECTOR_ACCOUNT_ID_STATUS="fixture-user",
                               TEST_FLOW=flow, ENABLE_ACTION_NAME="enable_user", DISABLE_ACTION_NAME="disable_user",
                               ID_PARAMETER_NAME="user_id", SLEEP="", FIXTURE_STATE=str(state),
                               FIXTURE_INVOCATIONS=str(invocations), FIXTURE_FAIL_ACTION=failed_action)
            result = subprocess.run(["bash", str(ACTION)], cwd=directory, env=environment,
                                    text=True, capture_output=True)
            calls = invocations.read_text().splitlines() if invocations.exists() else []
            return result, state.read_text(), calls

    def test_successful_enable_and_disable_flows(self):
        for flow, initial, expected, calls in (
            ("disable-enable", "RESOURCE_STATUS_ENABLED", "RESOURCE_STATUS_ENABLED", ["disable_user", "enable_user"]),
            ("enable-disable", "RESOURCE_STATUS_DISABLED", "RESOURCE_STATUS_DISABLED", ["enable_user", "disable_user"]),
        ):
            with self.subTest(flow=flow):
                result, status, actual_calls = self.run_flow(flow, initial)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(status, expected)
                self.assertEqual(actual_calls, calls)

    def test_enable_failure_is_not_masked_by_local_declaration(self):
        result, status, calls = self.run_flow("enable-only", "RESOURCE_STATUS_DISABLED", "enable_user")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Enable user action failed", result.stdout)
        self.assertIn("simulated connector failure", result.stdout)
        self.assertEqual(status, "RESOURCE_STATUS_DISABLED")
        self.assertEqual(calls, ["enable_user"])

    def test_disable_failure_is_not_masked_by_local_declaration(self):
        result, status, calls = self.run_flow("disable-only", "RESOURCE_STATUS_ENABLED", "disable_user")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Disable user action failed", result.stdout)
        self.assertIn("simulated connector failure", result.stdout)
        self.assertEqual(status, "RESOURCE_STATUS_ENABLED")
        self.assertEqual(calls, ["disable_user"])


if __name__ == "__main__":
    unittest.main()
