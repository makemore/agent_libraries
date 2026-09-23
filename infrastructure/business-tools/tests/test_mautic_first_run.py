"""Isolated caller-proof tests. No SSH or application requests."""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mautic_first_run as setup


class ProofTests(unittest.TestCase):
    def test_security_checks_refuse_empty_or_insecure_session(self):
        for cookies in ([], [SimpleNamespace(secure=False, _rest={'HttpOnly': None})],
                        [SimpleNamespace(secure=True, _rest={})]):
            with self.assertRaises(RuntimeError):
                setup.verify_session_security(SimpleNamespace(cookies=cookies))
        setup.verify_session_security(SimpleNamespace(cookies=[
            SimpleNamespace(secure=True, _rest={'httponly': None})]))

    def test_proof_refuses_failed_incomplete_or_false_evidence(self):
        for status, data in ((1, {'fresh_install_verified': True}), (0, {}),
                             (0, {'fresh_install_verified': False})):
            with patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=status, stdout=json.dumps(data))):
                with self.assertRaises(RuntimeError):
                    setup.fresh_proof()

    def test_proof_has_explicit_read_only_target_and_no_secret_arguments(self):
        with patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='{"fresh_install_verified":true}')) as run:
            self.assertTrue(setup.fresh_proof())
        args = run.call_args.args[0]
        self.assertIn('--project=makemoredigital2025', args)
        self.assertIn('--zone=europe-west2-b', args)
        self.assertIn('--command=sudo python3 -', args)
        self.assertIn("{'tables': 0}", run.call_args.kwargs['input'])
        compile(setup.PROOF, '<synthetic-proof>', 'exec')

    def test_default_preview_never_loads_credentials_or_proof(self):
        with patch.object(setup, 'Client'), patch.object(setup, 'initialize', return_value={}), \
             patch.object(setup, 'protected_json') as secrets, patch.object(setup, 'fresh_proof') as proof, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main([]), 0)
        secrets.assert_not_called()
        proof.assert_not_called()


if __name__ == '__main__':
    unittest.main()