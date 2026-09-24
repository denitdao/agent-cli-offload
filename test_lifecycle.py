"""Offline public-interface integration tests for the portable offload helper.

Every child CLI is a local fake; these tests must never invoke a paid model.
"""
import concurrent.futures
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
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / 'skills/codex-offload/scripts/offload.py'
ACTIVE = {'starting', 'running', 'stopping'}

# A fake CLI driven only by the literal JSON prompt passed on stdin. Records the
# exact argument vector/environment, produces actual streaming protocol events,
# and can leave a stubborn grandchild to exercise process-group cleanup.
FAKE = r'''import json, os, signal, subprocess, sys, time
from pathlib import Path
if sys.argv[1:3] == ['debug', 'models']:
    levels = [{'effort': e} for e in ('low', 'medium', 'high', 'xhigh')]
    print(json.dumps({'models': [
        {'slug': 'gpt-6-astra', 'visibility': 'list', 'supported_reasoning_levels': levels + [{'effort': 'ultra'}]},
        {'slug': 'gpt-5.6-sol', 'visibility': 'list', 'supported_reasoning_levels': levels},
        {'slug': 'gpt-6-sol', 'visibility': 'list', 'supported_reasoning_levels': levels},
        {'slug': 'gpt-5.10-luna', 'visibility': 'list', 'supported_reasoning_levels': levels},
        {'slug': 'gpt-5.9-luna', 'visibility': 'list', 'supported_reasoning_levels': levels},
        {'slug': 'gpt-7-sol', 'visibility': 'hide', 'supported_reasoning_levels': levels},
        {'slug': 'nova-preview', 'visibility': 'list', 'priority': 9, 'supported_reasoning_levels': levels},
        {'slug': 'davinci', 'visibility': 'list', 'supported_reasoning_levels': [{'description': 'no effort key'}]},
        {'slug': 42}]}))
    sys.exit(0)
raw = sys.stdin.read()
try:
    cfg = json.loads(raw.split('\n\n',1)[-1])
except ValueError:
    cfg = {'answer': raw}
tool = Path(sys.argv[0]).name
capture = {'argv':sys.argv[1:], 'stdin':raw, 'pid':os.getpid(),
           'effort':os.environ.get('CLAUDE_CODE_EFFORT_LEVEL'),
           'fast_disabled':os.environ.get('CLAUDE_CODE_DISABLE_FAST_MODE')}
with open(os.environ.get('FAKE_CALLS', 'fake-calls.jsonl'), 'a') as f:
    f.write(json.dumps(capture) + '\n')
if cfg.get('ignore_signals'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if cfg.get('grandchild'):
    code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); signal.signal(signal.SIGINT,signal.SIG_IGN); Path('grandchild.pid').write_text(str(os.getpid())); time.sleep(180)"
    subprocess.Popen([sys.executable, '-c', code])
def emit(x):
    print(json.dumps(x), flush=True)
session = cfg.get('session', '11111111-2222-4333-8444-555555555555')
if tool == 'claude':
    emit({'type':'system','subtype':'init','session_id':session,
          'model':cfg.get('model','claude-fable-5-1'), 'fast_mode_state':'off'})
else:
    emit({'type':'thread.started','thread_id':session})
    emit({'type':'turn.started'})
if cfg.get('slow_interrupt'):
    def finish_interrupted(signum, frame):
        time.sleep(cfg['slow_interrupt'])
        if tool == 'claude':
            emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'Saved after graceful interrupt'})
        else:
            emit({'type':'item.completed','item':{'type':'agent_message','text':'Saved after graceful interrupt'}})
            emit({'type':'turn.completed'})
        sys.exit(0)
    signal.signal(signal.SIGINT,finish_interrupted)
    Path('interrupt-ready').write_text('ready')
if cfg.get('malformed'):
    print('this is not json', flush=True)
if cfg.get('progress'):
    if tool == 'claude':
        emit({'type':'assistant','message':{'model':cfg.get('model','claude-fable-5-1'),'content':[{'type':'text','text':cfg['progress']}]}})
    else:
        emit({'type':'item.completed','item':{'type':'agent_message','text':cfg['progress']}})
time.sleep(cfg.get('sleep', 0))
if cfg.get('partial'):
    sys.stdout.write('{"type":"turn.completed"}')
    sys.stdout.flush()
    sys.exit(cfg.get('exit',0))
if not cfg.get('no_final'):
    answer = cfg.get('answer','All done.')
    if tool == 'claude':
        if cfg.get('child_result'):
            emit({'type':'result','subtype':'success','is_error':False,'session_id':'wrong-child-session','parent_tool_use_id':'child','result':'Child done'})
        emit({'type':'result','subtype':cfg.get('subtype','success'), 'is_error':cfg.get('is_error',False), 'session_id':session, 'result':answer, 'permission_denials':cfg.get('denials',[])})
    else:
        emit({'type':'item.completed','item':{'type':'agent_message','text':answer}})
        emit({'type':cfg.get('final_type','turn.completed')})
sys.exit(cfg.get('exit',0))
'''


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HELPER.exists():
            raise unittest.SkipTest('Lifecycle helper is not present yet')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='offload-lifecycle-')
        self.base = Path(self.temp.name)
        self.registry = self.base / 'registry'
        self.cwd = self.base / 'work'
        self.cwd.mkdir()
        self.bindir = self.base / 'bin'
        self.bindir.mkdir()
        for tool in ('codex', 'claude'):
            executable = self.bindir / tool
            executable.write_text('#!' + sys.executable + '\n' + FAKE)
            executable.chmod(0o700)
        self.env = os.environ.copy()
        self.env['PATH'] = str(self.bindir) + os.pathsep + self.env.get('PATH','')
        self.env['FAKE_CALLS'] = str(self.cwd / 'fake-calls.jsonl')
        self.ids = set()
        self.seq = 0

    def tearDown(self):
        # Cleanup still runs on assertion failure; never leave deliberately
        # stubborn fake children behind to confuse subsequent cases.
        for offload_id in self.ids:
            self.call('stop', offload_id, check=False)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status=self.status(offload_id)
                if status['state'] not in ACTIVE:
                    break
                time.sleep(.1)
        for pid_file in self.base.rglob('grandchild.pid'):
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.temp.cleanup()

    def prompt(self, cfg):
        self.seq += 1
        path = self.base / ('input-%s.json' % self.seq)
        path.write_text(json.dumps(cfg))
        return path

    def call(self, *args, check=True, timeout=15, env=None):
        p = subprocess.run([sys.executable, str(HELPER), '--root', str(self.registry),
                            *map(str,args)], env=env or self.env, text=True, cwd=self.cwd,
                           capture_output=True, timeout=timeout)
        try:
            value = json.loads(p.stdout)
        except ValueError:
            self.fail('Not JSON: argv=%r exit=%s stdout=%r stderr=%r' % (args,p.returncode,p.stdout,p.stderr))
        if check:
            self.assertEqual(p.returncode, 0, (args,value,p.stderr))
        return value

    def start(self, cfg=None, tool='codex', ident=None, **options):
        prompt = self.prompt(cfg or {})
        options.setdefault('interrupt_grace', .3)
        args = ['start','--tool',tool,'--cwd',str(self.cwd),'--prompt-file',str(prompt)]
        if ident:
            args += ['--id',ident]
        for key,value in options.items():
            args += ['--'+key.replace('_','-'),str(value)]
        result = self.call(*args)
        self.ids.add(result['offload_id'])
        return result

    def status(self, ident, touch=False):
        return self.call('status',ident,*([] if touch else ['--no-touch']))

    def terminal(self, ident, timeout=12):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            s = self.status(ident)
            if s['state'] not in ACTIVE:
                return s
            time.sleep(.04)
        self.fail('Offload did not terminate: %r' % s)

    def started(self, ident, timeout=4):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            s=self.status(ident)
            if s.get('child_pid'):
                return s
            time.sleep(.03)
        self.fail('Child never started: %r' % s)

    def captures(self):
        path=self.cwd/'fake-calls.jsonl'
        deadline=time.monotonic()+2
        while not path.exists() and time.monotonic()<deadline:
            time.sleep(.02)
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_start_returns_before_work_finishes_and_saved_success(self):
        before=time.monotonic()
        job=self.start({'sleep':1.2,'answer':'Result with unicode: café 日本語'})
        self.assertLess(time.monotonic()-before,1.0)
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'done')
        self.assertTrue(result['transport_success'])
        self.assertEqual(result['session_id'],'11111111-2222-4333-8444-555555555555')
        folder=self.registry/job['offload_id']/'runs'/'0001'
        for name in ('prompt.txt','events.jsonl','stderr.log','run.json'):
            self.assertTrue((folder/name).exists(),name)
        self.assertTrue((self.registry/job['offload_id']/'offload.json').exists())

    def test_claude_main_session_not_child_and_denial_not_automatic_block(self):
        job=self.start({'child_result':True,'denials':[{'tool_name':'Write'}]},tool='claude')
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'done')
        self.assertTrue(result['transport_success'])
        self.assertEqual(result['session_id'],'11111111-2222-4333-8444-555555555555')
        self.assertIn('Write',json.dumps(result))

    def test_nonzero_exit_cannot_be_success(self):
        job=self.start({'exit':7})
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['transport_success'])

    def test_missing_terminal_is_failure(self):
        result=self.terminal(self.start({'no_final':True})['offload_id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['transport_success'])

    def test_partial_terminal_is_not_success(self):
        result=self.terminal(self.start({'partial':True})['offload_id'])
        self.assertFalse(result['transport_success'])
        self.assertEqual(result['state'],'failed')

    def test_malformed_line_is_reported_and_never_claimed_success(self):
        result=self.terminal(self.start({'malformed':True})['offload_id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['transport_success'])
        self.assertEqual(result['malformed_lines'],1)
        self.assertEqual(result['terminal_event'],'turn.completed')

    def test_wrong_claude_model_rejected(self):
        result=self.terminal(self.start({'model':'claude-haiku-4-5'},tool='claude')['offload_id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['transport_success'])

    def test_hard_deadline_cannot_be_extended_by_polling(self):
        job=self.start({'sleep':60},max_runtime=.6,lease_seconds=30)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=self.status(job['offload_id'],touch=True)
            if result['state'] not in ACTIVE:
                break
            time.sleep(.07)
        self.assertEqual(result['state'],'timed_out')
        self.assertFalse(result['transport_success'])

    def test_lease_expires_without_touch(self):
        job=self.start({'sleep':60},lease_seconds=.5,max_runtime=30)
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'expired')
        self.assertFalse(result['transport_success'])

    def test_touch_keeps_alive_then_lease_expires(self):
        job=self.start({'sleep':60},lease_seconds=.55,max_runtime=30)
        for _ in range(6):
            time.sleep(.15)
            result=self.call('touch',job['offload_id'])
            self.assertIn(result['state'],ACTIVE)
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'expired')

    def test_list_does_not_renew_lease(self):
        job=self.start({'sleep':60},lease_seconds=.5,max_runtime=30)
        for _ in range(7):
            self.call('list')
            time.sleep(.10)
        self.assertEqual(self.terminal(job['offload_id'])['state'],'expired')

    def test_owner_death_stops_child(self):
        owner=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        try:
            job=self.start({'sleep':60},owner_pid=owner.pid,lease_seconds=30,max_runtime=30)
            self.started(job['offload_id'])
            owner.kill()
            owner.wait(timeout=3)
            result=self.terminal(job['offload_id'])
            self.assertEqual(result['state'],'owner_lost')
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=3)

    def test_explicit_stop_interrupts_stubborn_process_group(self):
        job=self.start({'sleep':60,'ignore_signals':True,'grandchild':True},lease_seconds=30)
        state=self.started(job['offload_id'])
        deadline=time.monotonic()+4
        while not (self.cwd/'grandchild.pid').exists() and time.monotonic()<deadline:
            time.sleep(.03)
        self.assertTrue((self.cwd/'grandchild.pid').exists())
        grandchild=int((self.cwd/'grandchild.pid').read_text())
        self.call('stop',job['offload_id'])
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'interrupted')
        self.assertFalse(result['transport_success'])
        for pid in (state['child_pid'],grandchild):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    os.kill(pid,0)
                except ProcessLookupError:
                    break
                # A zombie has exited and cannot do work; the platform's init
                # may not have reaped a detached grandchild yet.
                ps=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
                if not ps.stdout.strip() or ps.stdout.lstrip().startswith('Z'):
                    break
                time.sleep(.04)
            else:
                self.fail('Owned process survived stop: %s'%pid)

    def test_send_resumes_exact_session_and_uses_flagship_defaults(self):
        for tool in ('codex','claude'):
            with self.subTest(tool=tool):
                job=self.start(tool=tool)
                previous=self.terminal(job['offload_id'])
                follow=self.prompt({'answer':'Second answer'})
                sent=self.call('send',job['offload_id'],'--prompt-file',follow,'--request-id','followup-one')
                self.assertEqual(sent['run'],2)
                result=self.terminal(job['offload_id'])
                self.assertEqual(result['state'],'done')
                captured=self.captures()[-1]
                self.assertIn(previous['session_id'],captured['argv'])
                joined=' '.join(captured['argv'])
                self.assertIn('high',joined)
                if tool=='codex':
                    self.assertIn('gpt-6-astra',joined)
                    self.assertIn('service_tier',joined)
                    self.assertIn('default',joined)
                else:
                    self.assertIn('claude-fable-5-1',joined)
                    self.assertEqual(captured['fast_disabled'],'1')

    def last_argv(self):
        return self.captures()[-1]['argv']

    def test_codex_family_resolves_to_newest_listed_release(self):
        for family,expected in (('sol','gpt-6-sol'),('luna','gpt-5.10-luna'),('astra','gpt-6-astra')):
            with self.subTest(family=family):
                job=self.start(model=family,effort='xhigh')
                result=self.terminal(job['offload_id'])
                self.assertEqual(result['state'],'done')
                self.assertEqual(result['resolved_model'],expected)
                self.assertEqual(result['effort'],'xhigh')
                argv=self.last_argv()
                self.assertEqual(argv[argv.index('-m')+1],expected)
                self.assertIn('model_reasoning_effort="xhigh"',argv)

    def test_codex_exact_model_unknown_family_and_bad_effort(self):
        job=self.start(model='gpt-5.6-sol')
        self.assertEqual(self.terminal(job['offload_id'])['resolved_model'],'gpt-5.6-sol')
        prompt=self.prompt({})
        for options in (['--model','terra'],['--model','gpt-9-nope'],['--model','astra','--effort','max']):
            with self.subTest(options=options):
                reply=self.call('start','--tool','codex','--cwd',self.cwd,'--prompt-file',prompt,*options,check=False)
                self.assertIn('error',reply)

    def test_claude_family_alias_verified_by_family_and_pinned_on_send(self):
        job=self.start({'model':'claude-opus-5-5[1m]'},tool='claude',model='Opus',effort='max')
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'done',result)
        self.assertEqual(result['model_family'],'opus')
        captured=self.captures()[-1]
        self.assertEqual(captured['argv'][captured['argv'].index('--model')+1],'opus')
        self.assertEqual(captured['effort'],'max')
        self.call('send',job['offload_id'],'--prompt-file',self.prompt({'model':'claude-opus-5-5[1m]'}),'--request-id','two')
        self.assertEqual(self.terminal(job['offload_id'])['state'],'done')
        argv=self.last_argv()
        self.assertEqual(argv[argv.index('--model')+1],'claude-opus-5-5[1m]')

    def test_claude_other_family_or_other_exact_release_is_failure(self):
        for model,observed in (('opus','claude-fable-5-1'),('claude-opus-5-5','claude-opus-5-1'),('sonnet','claude-sonnet-5')):
            with self.subTest(model=model,observed=observed):
                result=self.terminal(self.start({'model':observed},tool='claude',model=model)['offload_id'])
                self.assertEqual(result['state'],'failed' if not observed.startswith('claude-'+model) else 'done')

    def test_codex_models_lists_catalogue_and_newest_per_family(self):
        result=self.call('models','--tool','codex')
        ids=[m['id'] for m in result['models']]
        self.assertEqual(result['latest_by_family'],{'astra':'gpt-6-astra','sol':'gpt-6-sol','luna':'gpt-5.10-luna'})
        self.assertNotIn('gpt-7-sol',ids)
        preview=next(m for m in result['models'] if m['id']=='nova-preview')
        self.assertIsNone(preview['family'])
        self.assertIn('xhigh',preview['efforts'])
        self.assertIn('gpt-7-sol',[m['id'] for m in self.call('models','--tool','codex','--all')['models']])
        # A slug outside the naming convention is still usable as an exact model.
        self.assertEqual(self.terminal(self.start(model='nova-preview')['offload_id'])['resolved_model'],'nova-preview')

    def test_claude_models_aliases_probe_and_api(self):
        env={k:v for k,v in self.env.items() if k not in ('ANTHROPIC_API_KEY','CLAUDECODE')}
        result=self.call('models','--tool','claude','--probe','opus',env=env)
        self.assertEqual(result['source'],'Claude Code aliases')
        self.assertIn('opus',result['aliases'])
        self.assertEqual(result['probe'],{'opus':'claude-fable-5-1'})  # the fake always reports Fable
        self.assertIn('opus',self.captures()[-1]['argv'])

        import http.server, threading
        body=json.dumps({'data':[{'id':'claude-opus-5-1','created_at':'2026-03-01T00:00:00Z'},
                                 {'id':'claude-opus-5-5','created_at':'2026-08-01T00:00:00Z'},
                                 {'id':'claude-haiku-4-5-20251001','created_at':'2025-10-01T00:00:00Z'}]}).encode()
        seen={}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update(path=self.path,key=self.headers.get('x-api-key'))
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self,*a): pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            result=self.call('models','--tool','claude',env={**env,'ANTHROPIC_API_KEY':'test-key',
                             'ANTHROPIC_BASE_URL':'http://127.0.0.1:%d'%server.server_port})
        finally:
            server.shutdown()
        self.assertEqual(seen['key'],'test-key')
        self.assertTrue(seen['path'].startswith('/v1/models'))
        self.assertEqual(result['latest_by_family'],{'opus':'claude-opus-5-5','haiku':'claude-haiku-4-5-20251001'})

    def test_claude_alias_forms_and_provider_ids(self):
        # (requested --model, model Claude reports, expected state)
        cases=(('opus[1m]','claude-opus-5-5[1m]','done'),
               ('opus[1m]','claude-sonnet-5','failed'),
               ('claude-haiku-4-5','claude-haiku-4-5-20251001','done'),
               ('CLAUDE-OPUS-5-5','claude-opus-5-5','done'),
               ('opus','us.anthropic.claude-opus-5-5-20260801-v1:0','done'),
               ('default','claude-sonnet-5','done'),
               ('lyra','claude-opus-5-5','failed'))
        for model,observed,state in cases:
            with self.subTest(model=model,observed=observed):
                result=self.terminal(self.start({'model':observed},tool='claude',model=model)['offload_id'])
                self.assertEqual(result['state'],state,result)
        self.assertFalse(result['model_verified'] if model=='default' else False)

    def test_send_refused_after_model_mismatch(self):
        job=self.start({'model':'claude-haiku-4-5'},tool='claude',model='opus')
        self.assertEqual(self.terminal(job['offload_id'])['state'],'failed')
        reply=self.call('send',job['offload_id'],'--prompt-file',self.prompt({}),'--request-id','again',check=False)
        self.assertIn('model check',reply['error'])

    def test_effort_is_validated_and_normalized(self):
        prompt=self.prompt({})
        for tool,effort in (('codex','high"\nmodel="x'),('claude','ultra'),('codex','HIGH ')):
            with self.subTest(tool=tool,effort=effort):
                reply=self.call('start','--tool',tool,'--cwd',self.cwd,'--prompt-file',prompt,'--effort',effort,check=False)
                if effort=='HIGH ':
                    self.ids.add(reply['offload_id'])
                    self.assertEqual(reply['effort'],'high')
                else:
                    self.assertIn('error',reply)

    def test_codex_odd_catalogue_entries(self):
        # A letters-only slug is an exact model, not an unknown family; no effort list means no check.
        result=self.terminal(self.start(model='davinci',effort='max')['offload_id'])
        self.assertEqual((result['state'],result['resolved_model']),('done','davinci'))

    def test_allow_tool_adds_to_default_grants(self):
        job=self.start(tool='claude',mode='edit',tools='Read,Grep,Bash',allow_tool='Bash(git diff:*)')
        self.terminal(job['offload_id'])
        argv=self.last_argv()
        self.assertEqual(argv[argv.index('--allowedTools')+1].split(','),['Read','Grep','Bash(git diff:*)'])

    def test_retry_of_pre_selection_manifest_stays_idempotent(self):
        job=self.start(tool='codex',ident='legacy-job')
        self.terminal(job['offload_id'])
        manifest=self.registry/'legacy-job'/'offload.json'
        meta=json.loads(manifest.read_text())
        import hashlib
        prompt=Path(self.captures()[-1]['stdin'].split('\n\n',1)[-1])
        old={k:v for k,v in meta.items() if k not in ('model','effort','model_family','resolved_model','model_verified','start_hash')}
        old['version']=2
        text=(self.base/'input-1.json').read_text()
        old['start_hash']=hashlib.sha256(json.dumps([{k:v for k,v in old.items() if k!='start_hash'},text],sort_keys=True).encode()).hexdigest()
        manifest.write_text(json.dumps(old))
        again=self.call('start','--tool','codex','--cwd',self.cwd,'--prompt-file',self.base/'input-1.json','--id','legacy-job','--interrupt-grace','0.3')
        self.assertEqual(again['run'],1)

    def test_legacy_manifest_keeps_its_pinned_model(self):
        job=self.start(tool='claude')
        self.terminal(job['offload_id'])
        manifest=self.registry/job['offload_id']/'offload.json'
        meta=json.loads(manifest.read_text())
        for key in ('model','model_family','resolved_model','effort'):
            meta.pop(key)
        manifest.write_text(json.dumps(meta))
        result=self.status(job['offload_id'])
        self.assertEqual((result['state'],result['requested_model'],result['effort']),('done','claude-fable-5-1','high'))

    def test_active_send_is_rejected_without_second_child(self):
        job=self.start({'sleep':.7})
        self.started(job['offload_id'])
        before=len(self.captures())
        reply=self.call('send',job['offload_id'],'--prompt-file',self.prompt({}),'--request-id','not-yet',check=False)
        self.assertIn('error',reply)
        self.terminal(job['offload_id'])
        self.assertEqual(len(self.captures()),before)

    def test_start_id_collision_does_not_overwrite(self):
        job=self.start(ident='stable-id')
        self.terminal(job['offload_id'])
        manifest=(self.registry/'stable-id'/'offload.json').read_bytes()
        reply=self.call('start','--tool','codex','--cwd',self.cwd,'--prompt-file',self.prompt({'answer':'wrong'}),'--id','stable-id',check=False)
        self.assertIn('error',reply)
        self.assertEqual((self.registry/'stable-id'/'offload.json').read_bytes(),manifest)

    def test_send_idempotent_concurrent_retries_launch_once(self):
        job=self.start()
        self.terminal(job['offload_id'])
        follow=self.prompt({'sleep':.3,'answer':'Exactly once'})
        argv=('send',job['offload_id'],'--prompt-file',follow,'--request-id','shared-token')
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            replies=list(pool.map(lambda _:self.call(*argv),range(5)))
        self.assertEqual({x['run'] for x in replies},{2})
        self.terminal(job['offload_id'])
        self.assertEqual(len(self.captures()),2)
        again=self.call(*argv)
        self.assertEqual(again['run'],2)
        self.assertEqual(len(self.captures()),2)
        conflict=self.call('send',job['offload_id'],'--prompt-file',self.prompt({'answer':'different'}),'--request-id','shared-token',check=False)
        self.assertIn('error',conflict)
        self.assertEqual(len(self.captures()),2)

    def test_edit_writers_same_cwd_cannot_overlap(self):
        job=self.start({'sleep':.8},mode='edit')
        self.started(job['offload_id'])
        denied=self.call('start','--tool','claude','--cwd',self.cwd,'--prompt-file',self.prompt({}),'--mode','edit',check=False)
        self.assertIn('error',denied)
        self.terminal(job['offload_id'])
        second=self.start(mode='edit')
        self.assertEqual(self.terminal(second['offload_id'])['state'],'done')


    def test_attention_requires_explicit_marker(self):
        for tool in ('codex','claude'):
            with self.subTest(tool=tool):
                job=self.start({'answer':'NEEDS_INPUT: Missing required account access.'},tool=tool)
                result=self.terminal(job['offload_id'])
                self.assertEqual(result['state'],'needs_input')

    def test_literal_prompt_preserves_shell_metacharacters(self):
        text='Literal $HOME `touch forbidden` $(touch forbidden)\nline two café'
        job=self.start({'answer':text})
        self.terminal(job['offload_id'])
        self.assertEqual(json.loads(self.captures()[-1]['stdin'].split('\n\n',1)[-1])['answer'],text)
        self.assertFalse((self.cwd/'forbidden').exists())

    def test_completed_state_stable_after_repeated_stop(self):
        job=self.start()
        before=self.terminal(job['offload_id'])
        for _ in range(2):
            self.call('stop',job['offload_id'])
        after=self.status(job['offload_id'])
        self.assertEqual(after['state'],before['state'])
        self.assertTrue(after['transport_success'])

    def test_same_cwd_readers_can_run_in_parallel(self):
        first=self.start({'sleep':.5},mode='read')
        second=self.start({'sleep':.5},mode='read')
        self.assertNotEqual(first['offload_id'],second['offload_id'])
        for job in (first,second):
            self.assertEqual(self.terminal(job['offload_id'])['state'],'done')

    def test_resume_checks_writer_lock_again(self):
        first=self.start(mode='edit')
        self.terminal(first['offload_id'])
        other=self.start({'sleep':.7},mode='edit')
        denied=self.call('send',first['offload_id'],'--prompt-file',self.prompt({}),
                         '--request-id','blocked-by-writer',check=False)
        self.assertIn('error',denied)
        self.terminal(other['offload_id'])
        sent=self.call('send',first['offload_id'],'--prompt-file',self.prompt({}),
                       '--request-id','writer-finished')
        self.assertEqual(sent['run'],2)
        self.assertEqual(self.terminal(first['offload_id'])['state'],'done')

    def test_same_id_concurrent_starts_launch_once(self):
        prompt=self.prompt({'sleep':.2})
        args=('start','--tool','codex','--cwd',self.cwd,'--prompt-file',prompt,'--id','race-id')
        self.ids.add('race-id')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.call(*args,check=False),range(4)))
        self.assertTrue(all('error' not in result for result in results))
        self.assertEqual({result['run'] for result in results},{1})
        self.assertEqual(self.terminal('race-id')['state'],'done')
        self.assertEqual(len(self.captures()),1)


    def test_successful_parent_exit_does_not_leave_ordinary_grandchild(self):
        job=self.start({'sleep':.25,'grandchild':True})
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'done')
        path=self.cwd/'grandchild.pid'
        self.assertTrue(path.exists())
        pid=int(path.read_text())
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            try:
                os.kill(pid,0)
            except ProcessLookupError:
                break
            ps=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
            if not ps.stdout.strip() or ps.stdout.lstrip().startswith('Z'):
                break
            time.sleep(.05)
        else:
            self.fail('Successful CLI left ordinary grandchild alive: %s'%pid)


    def test_incremental_read_cursor_disjoint_and_utf8_safe(self):
        job=self.start({'progress':'First café 日本語','sleep':.5,'answer':'Final café 日本語'})
        self.started(job['offload_id'])
        first=self.call('read',job['offload_id'],'--max-bytes',1)
        self.assertTrue(first['events'])
        self.assertTrue(first['cursor'].startswith('1:'))
        self.terminal(job['offload_id'])
        all_events=list(first['events'])
        cursor=first['cursor']
        for _ in range(20):
            chunk=self.call('read',job['offload_id'],'--cursor',cursor,'--max-bytes',1)
            if not chunk['events']:
                self.assertEqual(chunk['cursor'],cursor)
                break
            self.assertNotEqual(chunk['cursor'],cursor)
            all_events.extend(chunk['events'])
            cursor=chunk['cursor']
        else:
            self.fail('Cursor did not reach end of finite output')
        entire=self.call('read',job['offload_id'])
        self.assertEqual(all_events,entire['events'])
        self.assertEqual(cursor,entire['cursor'])
        self.assertIn('café 日本語',json.dumps(all_events,ensure_ascii=False))

    def test_read_excludes_partial_tail_and_retains_cursor(self):
        job=self.start({'partial':True})
        self.terminal(job['offload_id'])
        first=self.call('read',job['offload_id'])
        self.assertTrue(first['partial_tail'])
        self.assertNotIn('turn.completed',[e.get('type') for e in first['events']])
        again=self.call('read',job['offload_id'],'--cursor',first['cursor'])
        self.assertEqual(again['events'],[])
        self.assertTrue(again['partial_tail'])
        self.assertEqual(again['cursor'],first['cursor'])

    def test_read_rejects_stale_and_nonboundary_cursors(self):
        job=self.start()
        self.terminal(job['offload_id'])
        first=self.call('read',job['offload_id'])
        for cursor in ('1:1','1:-1','1:999999999','garbage'):
            self.assertIn('error',self.call('read',job['offload_id'],'--cursor',cursor,check=False))
        self.call('send',job['offload_id'],'--prompt-file',self.prompt({}),'--request-id','second')
        self.terminal(job['offload_id'])
        self.assertIn('error',self.call('read',job['offload_id'],'--cursor',first['cursor'],check=False))
        self.assertTrue(self.call('read',job['offload_id'])['cursor'].startswith('2:'))

    def test_wait_timeout_is_bounded_and_does_not_cancel_work(self):
        job=self.start({'sleep':.6})
        before=time.monotonic()
        result=self.call('wait',job['offload_id'],'--timeout',0,check=False)
        self.assertLess(time.monotonic()-before,.7)
        self.assertIn(result['state'],ACTIVE)
        self.assertEqual(result['wait_reason'],'timeout')
        self.assertEqual(self.terminal(job['offload_id'])['state'],'done')

    def test_wait_does_not_renew_lease_continuously(self):
        job=self.start({'sleep':60},lease_seconds=.5,max_runtime=30)
        result=self.call('wait',job['offload_id'],'--timeout',3,check=False)
        self.assertEqual(result['state'],'expired')
        self.assertEqual(result['wait_reason'],'attention')

    def test_status_and_read_renew_lease(self):
        for operation in ('status','read'):
            with self.subTest(operation=operation):
                job=self.start({'sleep':60},lease_seconds=.6,max_runtime=30)
                for _ in range(5):
                    time.sleep(.16)
                    result=self.call(operation,job['offload_id'])
                    self.assertIn(result['state'],ACTIVE)
                self.assertEqual(self.terminal(job['offload_id'])['state'],'expired')

    def test_lost_runner_reported_and_send_refused_then_stop_cleans_child(self):
        job=self.start({'sleep':60,'ignore_signals':True},lease_seconds=30)
        state=self.started(job['offload_id'])
        os.kill(state['runner_pid'],signal.SIGKILL)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=self.status(job['offload_id'])
            if result['state']=='lost':
                break
            time.sleep(.08)
        self.assertEqual(result['state'],'lost')
        denied=self.call('send',job['offload_id'],'--prompt-file',self.prompt({}),'--request-id','unsafe-resume',check=False)
        self.assertIn('error',denied)
        cleaned=self.call('stop',job['offload_id'])
        self.assertEqual(cleaned['state'],'interrupted')
        self.assertFalse(cleaned['transport_success'])

    def test_pid_identity_mismatch_never_signals_unrelated_process(self):
        job=self.start()
        self.terminal(job['offload_id'])
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            path=self.registry/job['offload_id']/'runs'/'0001'/'run.json'
            record=json.loads(path.read_text())
            record.update(state='running',runner_pid=unrelated.pid,runner_identity='not-this-start-time',
                          child_pid=unrelated.pid,child_identity='not-this-start-time',created_epoch=time.time())
            path.write_text(json.dumps(record))
            self.assertEqual(self.status(job['offload_id'])['state'],'lost')
            self.call('stop',job['offload_id'])
            self.assertIsNone(unrelated.poll(),'Stop signaled a reused/unrelated PID')
        finally:
            unrelated.kill()
            unrelated.wait(timeout=3)

    def test_private_run_artifacts(self):
        job=self.start()
        self.terminal(job['offload_id'])
        directory=self.registry/job['offload_id']
        self.assertEqual(directory.stat().st_mode & 0o077,0)
        for name in ('prompt.txt','events.jsonl','stderr.log','run.json'):
            self.assertEqual((directory/'runs'/'0001'/name).stat().st_mode & 0o077,0)


    def test_any_observed_claude_model_downgrade_is_failure(self):
        job=self.start({'model':'claude-haiku-4-5','progress':'Now uses Fable'},tool='claude')
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['transport_success'])

    def test_watchdog_cleans_after_worker_sigkill_without_caller_intervention(self):
        job=self.start({'sleep':60,'ignore_signals':True,'grandchild':True},lease_seconds=30,max_runtime=30)
        state=self.started(job['offload_id'])
        folder=self.registry/job['offload_id']/'runs'/'0001'
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if (folder/'watchdog.json').exists() and (self.cwd/'grandchild.pid').exists():
                break
            time.sleep(.03)
        self.assertTrue((folder/'watchdog.json').exists())
        grandchild=int((self.cwd/'grandchild.pid').read_text())
        os.kill(state['runner_pid'],signal.SIGKILL)
        deadline=time.monotonic()+12
        while not (folder/'watchdog-done.json').exists() and time.monotonic()<deadline:
            time.sleep(.08)
        self.assertTrue((folder/'watchdog-done.json').exists(), 'Watchdog failed to finish orphan cleanup: '+repr({name:(folder/name).read_text()[-3000:] for name in ('run.json','watchdog.log','watchdog-warning.json','watchdog-stop.json') if (folder/name).exists()}))
        self.assertEqual(self.status(job['offload_id'])['state'],'lost')
        for pid in (state['child_pid'],grandchild):
            ps=subprocess.run(['/bin/ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
            self.assertTrue(not ps.stdout.strip() or ps.stdout.lstrip().startswith('Z'),
                            'Orphan remained active after watchdog cleanup: %s'%pid)


    def test_slow_sigint_retains_final_result_then_resumes_same_session(self):
        job=self.start({'sleep':60,'slow_interrupt':5},tool='claude',interrupt_grace=7)
        deadline=time.monotonic()+5
        while not (self.cwd/'interrupt-ready').exists() and time.monotonic()<deadline:
            time.sleep(.03)
        self.assertTrue((self.cwd/'interrupt-ready').exists())
        before=self.status(job['offload_id'])
        self.call('stop',job['offload_id'])
        ended=self.terminal(job['offload_id'],timeout=12)
        self.assertEqual(ended['state'],'interrupted')
        self.assertEqual(ended['exit_code'],0)
        self.assertEqual(ended['terminal_event'],'success')
        self.assertIn('Saved after graceful interrupt',ended['last_message'])
        self.assertFalse(ended['transport_success'])
        resumed=self.call('send',job['offload_id'],'--prompt-file',self.prompt({}),
                          '--request-id','after-graceful-stop')
        self.assertEqual(resumed['run'],2)
        result=self.terminal(job['offload_id'])
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['session_id'],before['session_id'])
        self.assertIn(before['session_id'],self.captures()[-1]['argv'])

    def test_claude_bash_tool_exposure_does_not_implicitly_grant_shell(self):
        job=self.start(tool='claude',mode='edit',tools='Read,Bash')
        self.terminal(job['offload_id'])
        argv=self.captures()[-1]['argv']
        self.assertIn('Bash',argv[argv.index('--tools')+1].split(','))
        allowed=argv[argv.index('--allowedTools')+1].split(',')
        self.assertNotIn('Bash',allowed)
        self.assertFalse(any(x.startswith('Bash(') for x in allowed))
        explicit=self.start(tool='claude',mode='edit',tools='Read,Bash',allow_tool='Bash(python3:*)')
        self.terminal(explicit['offload_id'])
        argv=self.captures()[-1]['argv']
        self.assertIn('Bash(python3:*)',argv[argv.index('--allowedTools')+1].split(','))
        self.assertNotIn('Bash',argv[argv.index('--allowedTools')+1].split(','))

    def test_nested_cwd_edit_writers_are_excluded_both_directions(self):
        nested=self.cwd/'sub'
        nested.mkdir()
        parent=self.cwd
        for first,second in ((parent,nested),(nested,parent)):
            with self.subTest(first=first):
                self.cwd=first
                job=self.start({'sleep':.8},mode='edit')
                denied=self.call('start','--tool','codex','--cwd',second,
                                 '--prompt-file',self.prompt({}),'--mode','edit',check=False)
                self.assertIn('error',denied)
                self.terminal(job['offload_id'])
        self.cwd=parent

    def test_codex_rejects_unsupported_claude_permission_options(self):
        for option,value in (('--tools','Read'),('--allow-tool','Read'),('--add-dir',self.cwd)):
            with self.subTest(option=option):
                result=self.call('start','--tool','codex','--cwd',self.cwd,
                                 '--prompt-file',self.prompt({}),option,value,check=False)
                self.assertIn('error',result)
        self.assertFalse((self.cwd/'fake-calls.jsonl').exists())

    def test_list_reports_corrupt_manifest_without_hiding_good_jobs(self):
        good=self.start()
        self.terminal(good['offload_id'])
        bad=self.registry/'broken'
        bad.mkdir()
        (bad/'offload.json').write_text('{}')
        result=self.call('list')
        states={item['offload_id']:item['state'] for item in result}
        self.assertEqual(states[good['offload_id']],'done')
        self.assertEqual(states['broken'],'unreadable')

    def test_empty_launch_remnant_can_be_recovered(self):
        empty=self.registry/'empty-remnant'
        empty.mkdir(parents=True)
        job=self.start(ident='empty-remnant')
        self.assertEqual(self.terminal(job['offload_id'])['state'],'done')

    def test_nonempty_launch_remnant_is_preserved_and_recovery_explained(self):
        damaged=self.registry/'nonempty-remnant'
        damaged.mkdir(parents=True)
        (damaged/'sentinel.txt').write_text('Unrelated content must remain')
        result=self.call('start','--tool','codex','--cwd',self.cwd,
                         '--prompt-file',self.prompt({}),'--id','nonempty-remnant',check=False)
        self.assertIn('error',result)
        self.assertEqual((damaged/'sentinel.txt').read_text(),'Unrelated content must remain')
        self.assertFalse((self.cwd/'fake-calls.jsonl').exists())
        self.assertRegex(result['error'].lower(),r'recover|inspect|manual|remnant')

    def test_failed_child_identity_probe_never_executes_target_cli(self):
        spec=importlib.util.spec_from_file_location('lifecycle_helper_under_test',HELPER)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job=self.registry/'identity-failure'
        folder=job/'runs'/'0001'
        folder.mkdir(parents=True)
        module.save(job/'offload.json',dict(tool='codex',cwd=str(self.cwd),run=1,
                    max_runtime=20,lease_seconds=10,interrupt_grace=.3))
        module.save(folder/'run.json',dict(state='starting',created_epoch=time.time(),
                    runner_pid=None,runner_identity=None,child_pid=None,child_identity=None,
                    command=[str(self.bindir/'codex')]))
        (folder/'prompt.txt').write_text('{}')
        module.touch(job)
        actual_identity=module.identity
        real_popen=subprocess.Popen
        children=[]
        def fail_child_identity(pid):
            if pid != os.getpid():
                raise module.Failure('Simulated child identity lookup failure')
            return actual_identity(pid)
        def capture_popen(*args,**kwargs):
            child=real_popen(*args,**kwargs)
            # identity() itself launches ps; track only the gate child.
            if '_child' in args[0]:
                children.append(child)
            return child
        original_handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
        try:
            with patch.object(module,'identity',side_effect=fail_child_identity), patch.object(module.subprocess,'Popen',side_effect=capture_popen):
                module.worker(self.registry,SimpleNamespace(id=job.name,number=1))
            self.assertFalse((self.cwd/'fake-calls.jsonl').exists())
            self.assertFalse((folder/'go.json').exists())
            self.assertTrue(children)
            self.assertTrue(all(child.poll() is not None for child in children),'Unidentified gate child left alive')
            record=json.loads((folder/'run.json').read_text())
            self.assertEqual(record['exit_code'],127)
            self.assertIn('identity',record['launch_error'].lower())
        finally:
            for sig,handler in original_handlers.items():
                signal.signal(sig,handler)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


    def test_watchdog_transient_identity_error_preserves_escalation_clock(self):
        spec=importlib.util.spec_from_file_location('watchdog_fault_injection',HELPER)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job=self.registry/'transient-watchdog'
        folder=job/'runs'/'0001'
        folder.mkdir(parents=True)
        runner_pid,child_pid=1234001,1234002
        module.save(job/'offload.json',{'interrupt_grace':.6})
        module.save(folder/'run.json',dict(child_pid=child_pid,child_identity='child-start',
                    runner_pid=runner_pid,runner_identity='worker-start',deadline_monotonic=100))
        elapsed=[0.0]
        attempts=[]
        term_failed=[False]
        def sleep(seconds):
            elapsed[0]+=seconds
        def alive(pid,stamp,verify=False):
            return pid==child_pid
        def send_signal(record,sig):
            attempts.append((sig,elapsed[0]))
            if sig==signal.SIGTERM and not term_failed[0]:
                term_failed[0]=True
                raise module.Failure('Transient identity probe failed during SIGTERM')
        with patch.object(module,'identity',return_value='guard-start'), \
             patch.object(module,'alive',side_effect=alive), \
             patch.object(module,'signal_group',side_effect=send_signal), \
             patch.object(module,'cleanup_group') as cleanup, \
             patch.object(module.time,'monotonic',side_effect=lambda:elapsed[0]), \
             patch.object(module.time,'sleep',side_effect=sleep):
            module.watchdog(self.registry,SimpleNamespace(id=job.name,number=1))
        signals=[sig for sig,_ in attempts]
        self.assertEqual(signals.count(signal.SIGINT),1,'Transient lookup resent SIGINT')
        self.assertEqual(signals.count(signal.SIGTERM),2,'Failed SIGTERM was not retried')
        self.assertEqual(signals.count(signal.SIGKILL),1)
        kill_time=next(at for sig,at in attempts if sig==signal.SIGKILL)
        self.assertLessEqual(kill_time,3.9,'Transient lookup restarted escalation deadline')
        cleanup.assert_not_called()  # A successful group SIGKILL already covers descendants.
        timeline=json.loads((folder/'watchdog-stop.json').read_text())['signals']
        self.assertEqual([x['signal'] for x in timeline],[signal.SIGINT,signal.SIGTERM,signal.SIGKILL])
        done=json.loads((folder/'watchdog-done.json').read_text())
        self.assertEqual(done['reason'],'worker_lost')
        self.assertTrue((folder/'watchdog-warning.json').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
