"""Offline lifecycle and installation checks; never call a paid model."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / 'skills/codex-offload/scripts/run.py'


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='offload-test-')
        self.root = Path(self.temp.name)
        self.prompt = self.root / 'prompt.txt'
        self.prompt.write_text('Literal $HOME `touch should-not-exist` $(false)\nsecond line\n')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, script, tool='codex', timeout=0):
        return [sys.executable, str(RUNNER), 'run', '--tool', tool,
                '--cwd', str(self.root), '--prompt-file', str(self.prompt),
                '--run-dir', str(self.root / 'run'), '--timeout', str(timeout),
                '--', sys.executable, '-u', '-c', script]

    def test_success_and_literal_stdin(self):
        script = '''import json,sys
from pathlib import Path
Path('received.txt').write_text(sys.stdin.read())
print(json.dumps({'type':'thread.started','thread_id':'exact-id'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Done'}}))
print(json.dumps({'type':'turn.completed'}))
'''
        p = subprocess.run(self.command(script), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertTrue(result['transport_success'])
        self.assertEqual(result['session_id'], 'exact-id')
        self.assertEqual((self.root / 'received.txt').read_text(), self.prompt.read_text())
        self.assertFalse((self.root / 'should-not-exist').exists())
        self.assertEqual((self.root / 'run').stat().st_mode & 0o777, 0o700)

    def test_nonzero_exit_cannot_be_success(self):
        p = subprocess.run(self.command("print('{\"type\":\"turn.completed\"}'); raise SystemExit(7)"), capture_output=True, text=True)
        self.assertEqual(p.returncode, 7)
        self.assertFalse(json.loads(p.stdout)['transport_success'])

    def test_zero_without_terminal_event_is_incomplete(self):
        p = subprocess.run(self.command("print('{\"type\":\"turn.started\"}')"), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(json.loads(p.stdout)['transport_success'])

    def test_claude_denials_and_child_results(self):
        script = '''import json
print(json.dumps({'type':'system','subtype':'init','session_id':'parent'}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'child','parent_tool_use_id':'subagent'}))
print(json.dumps({'type':'result','subtype':'error_max_turns','is_error':True,'session_id':'parent','permission_denials':[{'tool_name':'Write'}]}))
'''
        p = subprocess.run(self.command(script, tool='claude'), capture_output=True, text=True)
        result = json.loads(p.stdout)
        self.assertFalse(result['transport_success'])
        self.assertEqual(result['session_id'], 'parent')
        self.assertEqual(result['permission_denials'][0]['tool_name'], 'Write')

    def test_partial_line_is_not_a_terminal_event(self):
        p = subprocess.run(self.command("import sys; sys.stdout.write('{\"type\":\"turn.completed\"}')"), capture_output=True, text=True)
        result = json.loads(p.stdout)
        self.assertTrue(result['partial_tail'])
        self.assertFalse(result['transport_success'])

    def test_timeout_and_live_status(self):
        p = subprocess.Popen(self.command("import time; print('{\"type\":\"turn.started\"}'); time.sleep(30)", timeout=1), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        metadata = self.root / 'run/run.json'
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if metadata.exists() and json.loads(metadata.read_text())['phase'] == 'running':
                break
            time.sleep(.02)
        status = subprocess.run([sys.executable, str(RUNNER), 'status', str(metadata.parent)], capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)['phase'], 'running')
        out, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 124, err)
        self.assertEqual(json.loads(out)['stop_reason'], 'timeout')

    def test_sigterm_is_forwarded_and_recorded(self):
        p = subprocess.Popen(self.command('import time; time.sleep(30)'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        metadata = self.root / 'run/run.json'
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if metadata.exists() and json.loads(metadata.read_text())['phase'] == 'running':
                break
            time.sleep(.02)
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 143, err)
        self.assertEqual(json.loads(out)['stop_reason'], 'signal:15')

    def test_missing_binary_is_durable_launch_error(self):
        command = self.command('')
        command[command.index('--') + 1:] = ['/nonexistent/offload-test-binary']
        p = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(p.returncode, 127)
        self.assertIn('launch_error', json.loads(p.stdout))

    def test_existing_run_is_not_overwritten(self):
        directory = self.root / 'run'
        directory.mkdir()
        (directory / 'sentinel').write_text('keep')
        p = subprocess.run(self.command(''), capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual((directory / 'sentinel').read_text(), 'keep')


class InstallTests(unittest.TestCase):
    def test_install_idempotence_and_collision(self):
        spec = importlib.util.spec_from_file_location('offload_install', ROOT / 'install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='offload-install-test-') as tmp, patch.dict(os.environ):
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            home = Path(tmp)
            module.install(home, apply=True)
            module.install(home, apply=True)
            skill = home / '.agents/skills/codex-offload/SKILL.md'
            self.assertEqual((home / '.claude/skills/codex-offload/SKILL.md').read_text(), skill.read_text())
            skill.write_text('existing user work')
            with self.assertRaises(ValueError):
                module.install(home, apply=True)
            self.assertEqual(skill.read_text(), 'existing user work')

    def test_update_preserves_backup_and_claude_link(self):
        spec = importlib.util.spec_from_file_location('offload_install', ROOT / 'install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='offload-update-test-') as tmp, patch.dict(os.environ):
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            home = Path(tmp)
            module.install(home, apply=True)
            target = home / '.agents/skills/codex-offload'
            (target / 'SKILL.md').write_text('local customization')
            (target / 'extra-note.txt').write_text('preserve me')
            module.install(home, apply=False, update=True)
            self.assertEqual((target / 'SKILL.md').read_text(), 'local customization')
            module.install(home, apply=True, update=True)
            backups = list((home / '.agents/offload-skill-backups').glob('codex-offload-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / 'extra-note.txt').read_text(), 'preserve me')
            self.assertEqual((backups[0] / 'SKILL.md').read_text(), 'local customization')
            self.assertEqual(module.digest_tree(target), module.digest_tree(ROOT / 'skills/codex-offload'))
            self.assertEqual((home / '.claude/skills/codex-offload').resolve(), target.resolve())

    def test_lifecycle_and_reference_copies_stay_identical(self):
        for relative in ['scripts/offload.py', 'references/helper.md']:
            self.assertEqual((ROOT / 'skills/codex-offload' / relative).read_bytes(),
                             (ROOT / 'skills/claude-offload' / relative).read_bytes())

    def test_runner_copies_stay_identical(self):
        self.assertEqual(RUNNER.read_bytes(), (ROOT / 'skills/claude-offload/scripts/run.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
