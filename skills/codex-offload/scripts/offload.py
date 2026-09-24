#!/usr/bin/env python3
"""Portable, bounded CLI offloads. POSIX + Python 3 standard library only."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ACTIVE = {'starting', 'running', 'stopping'}
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
# Flagship family used when the caller names no model; resolved to its newest release.
DEFAULT_FAMILY = {'codex': 'astra', 'claude': 'fable'}
# Offloads created before model selection existed pinned these exact IDs.
LEGACY_MODELS = {'codex': 'gpt-6-astra', 'claude': 'claude-fable-5-1'}
CLAUDE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Claude Code's latest-release family aliases. An unlisted lowercase word is treated as a
# newer family; composite aliases select a model per mode, so they cannot be verified.
CLAUDE_FAMILIES = ('fable', 'opus', 'sonnet', 'haiku')
CLAUDE_COMPOSITE_ALIASES = ('default', 'best', 'opusplan')
CODEX_SLUG_RE = re.compile(r'^gpt-(\d+(?:\.\d+)*)-([a-z]+)$')
CLAUDE_ID_RE = re.compile(r'^claude-([a-z]+)-(\d+(?:-\d+)*?)(?:-\d{8})?$')
CLAUDE_ALIAS_RE = re.compile(r'^([a-z]+)(\[[a-z0-9]+\])?$')
FAMILY_RE = re.compile(r'^[a-z]+$')
EFFORT_RE = re.compile(r'^[a-z]+$')
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,255}$')


IDENTITY_CACHE = {}


class Failure(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def identity(pid):
    """PID plus OS start timestamp; zombies count as dead. Never inspect arguments."""
    if not pid or pid <= 1:
        return None
    try:
        p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat='],
                           capture_output=True, text=True, timeout=3,
                           env={**os.environ, 'LC_ALL': 'C'})
        bits = p.stdout.split()
        if p.returncode and p.stderr.strip():
            raise Failure('Process identity inspection unavailable: ' + p.stderr.strip() + '. Run through the host permission mechanism.')
        if p.returncode or len(bits) < 6 or bits[-1].startswith('Z'):
            return None
        return ' '.join(bits[:-1])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failure('Cannot inspect process identity: ' + str(error))


def alive(pid, stamp, verify=False):
    if not stamp:
        return False
    cached = IDENTITY_CACHE.get(pid)
    if verify or cached is None or time.monotonic() - cached[0] >= 1:
        cached = (time.monotonic(), identity(pid))
        IDENTITY_CACHE[pid] = cached
    return cached[1] == stamp


def valid_id(value):
    if not ID_RE.fullmatch(value):
        raise Failure('ID must be 1-80 letters, digits, hyphens or underscores; start with a letter/digit')
    return value


def job_dir(root, job_id):
    path = root / valid_id(job_id)
    if path.is_symlink():
        raise Failure('Refusing a symlinked offload directory')
    return path


def run_dir(job, number):
    return job / 'runs' / f'{number:04d}'


@contextlib.contextmanager
def locked(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def touch(job):
    save(job / 'lease.json', {'monotonic': time.monotonic(), 'at': now()})


def version_key(text):
    return tuple(int(part) for part in text.split('.'))


def codex_catalogue(executable):
    errors = []
    # The default call may refresh the catalogue online; the bundled copy works offline.
    for extra in ([], ['--bundled']):
        try:
            p = subprocess.run([executable, 'debug', 'models', *extra], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            if p.returncode:
                raise ValueError('exit %s: %s' % (p.returncode, p.stderr.strip()[-300:]))
            models = json.loads(p.stdout)['models']
            if not isinstance(models, list):
                raise ValueError('no model list')
            return [m for m in models if isinstance(m, dict) and isinstance(m.get('slug'), str)]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as error:
            errors.append(str(error))
    raise Failure('Cannot read the Codex model catalogue (`codex debug models`): ' + '; '.join(errors))


def listed(entry):
    return entry.get('visibility', 'list') == 'list'


def efforts_of(entry):
    return [level['effort'] for level in entry.get('supported_reasoning_levels') or []
            if isinstance(level, dict) and isinstance(level.get('effort'), str)]


def newest_codex(catalogue):
    """Newest listed release per gpt-<version>-<family> family."""
    newest = {}
    for entry in catalogue:
        match = CODEX_SLUG_RE.fullmatch(entry['slug'])
        if match and listed(entry):
            key = version_key(match.group(1))
            if match.group(2) not in newest or key > newest[match.group(2)][0]:
                newest[match.group(2)] = (key, entry)
    return {family: pair[1] for family, pair in newest.items()}


def codex_models(executable, include_hidden):
    catalogue = codex_catalogue(executable)
    models = []
    for entry in sorted(catalogue, key=lambda e: (not isinstance(e.get('priority'), (int, float)),
                                                  e.get('priority') if isinstance(e.get('priority'), (int, float)) else 0,
                                                  e['slug'])):
        if not listed(entry) and not include_hidden:
            continue
        match = CODEX_SLUG_RE.fullmatch(entry['slug'])
        models.append(dict(id=entry['slug'], display_name=entry.get('display_name'),
                           description=entry.get('description'), family=match.group(2) if match else None,
                           listed=listed(entry), efforts=efforts_of(entry),
                           context_window=entry.get('context_window'), upgrade=entry.get('upgrade')))
    return dict(tool='codex', source='codex debug models', default=DEFAULT_FAMILY['codex'], default_effort='high',
                models=models,
                latest_by_family={family: entry['slug'] for family, entry in newest_codex(catalogue).items()},
                usage='Pass a family (newest release) or an exact id to start --model.')


def claude_models(executable, probe):
    result = dict(tool='claude', default=DEFAULT_FAMILY['claude'], default_effort='high',
                  aliases=list(CLAUDE_FAMILIES), efforts=list(CLAUDE_EFFORTS),
                  usage='Pass a family alias (newest release), optionally with a suffix such as opus[1m], '
                        'or an exact claude-... id to start --model.')
    key = os.environ.get('ANTHROPIC_API_KEY')
    if key:
        base = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com').rstrip('/')
        request = urllib.request.Request(base + '/v1/models?limit=1000',
                                         headers={'x-api-key': key, 'anthropic-version': '2023-06-01'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)['data']
            models, newest = [], {}
            for entry in data:
                if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
                    continue
                match = CLAUDE_ID_RE.fullmatch(entry['id'])
                family = match.group(1) if match else None
                created = str(entry.get('created_at') or '')
                models.append(dict(id=entry['id'], display_name=entry.get('display_name'),
                                   family=family, created_at=created or None))
                if family and (family not in newest or created > newest[family][0]):
                    newest[family] = (created, entry['id'])
            result.update(source='Anthropic Models API (ANTHROPIC_API_KEY account)', models=models,
                          latest_by_family={family: pair[1] for family, pair in newest.items()})
        except (OSError, ValueError, KeyError, TypeError) as error:
            result['api_error'] = str(error)
    if 'models' not in result:
        result.update(source='Claude Code aliases', note=(
            'Claude Code has no offline model list. Each alias selects the newest release of its family that '
            'the installed CLI knows; `models --tool claude --probe ALIAS` makes one tiny paid call to show which. '
            'Current IDs: https://docs.claude.com/en/docs/about-claude/models/overview'))
    if probe:
        if os.environ.get('CLAUDECODE'):
            raise Failure('Nested Claude environment detected (CLAUDECODE); cannot probe from inside Claude Code.')
        result['probe'] = {}
        for alias in probe:
            if not MODEL_RE.fullmatch(alias):
                result['probe'][alias] = {'error': 'not a model alias or ID'}
                continue
            try:
                # Neutral directory: project instructions and hooks are irrelevant to a model lookup.
                p = subprocess.run([executable, '-p', '--model', alias, '--effort', 'low', '--tools', '',
                                    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                                    '--settings', '{"fastMode":false,"ultracode":false}', '--no-session-persistence',
                                    '--output-format', 'stream-json', '--verbose'],
                                   input='Reply with the single word OK.', capture_output=True, text=True, timeout=120,
                                   cwd=tempfile.gettempdir(), env={**os.environ, 'CLAUDE_CODE_DISABLE_FAST_MODE': '1'})
                observed = []
                for line in p.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get('type') == 'system' and event.get('subtype') == 'init':
                        observed.append(event.get('model'))
                result['probe'][alias] = observed[0] if observed and p.returncode == 0 else {
                    'error': (p.stderr.strip() or p.stdout.strip())[-500:] or 'exit %s' % p.returncode}
            except (OSError, subprocess.TimeoutExpired) as error:
                result['probe'][alias] = {'error': str(error)}
    return result


def list_models(args):
    executable = shutil.which(args.tool)
    if not executable:
        raise Failure('Required CLI not found on PATH: ' + args.tool)
    if args.tool == 'codex':
        if args.probe:
            raise Failure('--probe is Claude-only; the Codex catalogue is already exact')
        return codex_models(executable, args.all)
    return claude_models(executable, args.probe)


def normalize_request(tool, spec, effort):
    """Canonical (model, effort) request; stored in the manifest and hashed for idempotency."""
    spec = (spec or DEFAULT_FAMILY[tool]).strip()
    effort = (effort or 'high').strip().lower()
    if not MODEL_RE.fullmatch(spec):
        raise Failure('--model must be a family alias (e.g. opus, sol) or an exact model ID')
    if not EFFORT_RE.fullmatch(effort):
        raise Failure('--effort must be a single word such as low, medium, high, xhigh or max')
    lower = spec.lower()
    if tool == 'codex' or CLAUDE_ALIAS_RE.fullmatch(lower) or lower.startswith('claude-'):
        spec = lower
    return spec, effort


def resolve_model(tool, executable, spec, effort):
    """Turn a normalized request into the model the CLI is asked for.

    Codex families resolve to the newest listed gpt-<version>-<family>. Claude
    families use the CLI's own latest-model alias and are verified by family.
    """
    if tool == 'claude':
        if effort not in CLAUDE_EFFORTS:
            raise Failure('Claude effort must be one of: ' + ', '.join(CLAUDE_EFFORTS))
        alias = CLAUDE_ALIAS_RE.fullmatch(spec)
        if alias and alias.group(1) in CLAUDE_COMPOSITE_ALIASES:
            return dict(model=spec, model_family=None, resolved_model=None, model_verified=False)
        return dict(model=spec, model_family=alias.group(1) if alias else None, resolved_model=None,
                    model_verified=True)
    catalogue = codex_catalogue(executable)
    exact = next((e for e in catalogue if e['slug'].lower() == spec), None)
    family = spec if FAMILY_RE.fullmatch(spec) and exact is None else None
    entry = newest_codex(catalogue).get(family) if family else exact
    if entry is None:
        names = ', '.join(sorted(e['slug'] for e in catalogue if listed(e)))
        raise Failure('No Codex model or family %r. Available: %s. Run `offload.py models --tool codex`.'
                      % (spec, names))
    levels = efforts_of(entry)
    if levels and effort not in levels:
        raise Failure('%s does not support %s effort; supported: %s' % (entry['slug'], effort, ', '.join(levels)))
    return dict(model=entry['slug'], model_family=family, resolved_model=entry['slug'], model_verified=False)


def model_settings(meta):
    """Model selection for a manifest; manifests without one keep their legacy pin."""
    model = meta.get('model') or LEGACY_MODELS[meta['tool']]
    return dict(requested_model=model, model_family=meta.get('model_family'),
                resolved_model=meta.get('resolved_model') or (None if meta.get('model') else model),
                effort=meta.get('effort', 'high'), model_verified=meta.get('model_verified', meta['tool'] == 'claude'))


def canonical_claude_id(value):
    """Comparable Claude ID: drop provider prefixes, context suffixes and snapshot dates."""
    value = value.lower().split('[', 1)[0]
    value = value[value.find('claude-'):] if 'claude-' in value else value
    return re.sub(r'-\d{8}$', '', re.sub(r'-v\d+(:\d+)?$', '', value))


def model_matches(observed, settings):
    if not settings['model_verified']:
        return True
    observed = canonical_claude_id(observed)
    if settings['model_family'] and not settings['resolved_model']:
        return observed.startswith('claude-%s-' % settings['model_family'])
    return observed == canonical_claude_id(settings['resolved_model'] or settings['requested_model'])


def events_summary(path, tool, settings=None):
    result = dict(session_id=None, terminal_event=None, agent_success=False,
                  permission_denials=[], malformed_lines=0, partial_tail=False,
                  last_message='', observed_model=None, init_model=None, fast_mode_state=None,
                  observed_models=[])
    if not path.exists():
        return result
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                result['partial_tail'] = True
                break
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('not an object')
            except (ValueError, UnicodeError):
                result['malformed_lines'] += 1
                continue
            kind = event.get('type')
            if tool == 'codex':
                if kind == 'thread.started':
                    result['session_id'] = event.get('thread_id')
                if kind == 'turn.started':
                    result.update(terminal_event=None, agent_success=False)
                if kind in ('turn.completed', 'turn.failed'):
                    result.update(terminal_event=kind, agent_success=kind == 'turn.completed')
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') == 'agent_message':
                    result['last_message'] = str(item.get('text', ''))
                if kind == 'error':
                    result['last_error'] = event.get('message', event.get('error'))
            else:
                if event.get('parent_tool_use_id'):
                    continue
                if kind == 'system' and event.get('subtype') == 'init':
                    result.update(session_id=event.get('session_id'),
                                  observed_model=event.get('model'), init_model=event.get('model'),
                                  fast_mode_state=event.get('fast_mode_state'))
                    if event.get('model') and event['model'] not in result['observed_models']:
                        result['observed_models'].append(event['model'])
                if kind == 'assistant':
                    message = event.get('message') or {}
                    model = message.get('model')
                    if model:
                        result['observed_model'] = model
                        if model not in result['observed_models']:
                            result['observed_models'].append(model)
                    for block in message.get('content', []):
                        if block.get('type') == 'text':
                            result['last_message'] = block.get('text', '')
                if kind == 'result':
                    result.update(session_id=event.get('session_id', result['session_id']),
                                  terminal_event=event.get('subtype', 'result'),
                                  agent_success=event.get('subtype') == 'success' and event.get('is_error') is False,
                                  permission_denials=event.get('permission_denials', []),
                                  last_message=str(event.get('result', '')))
    result['model_mismatch'] = bool(tool == 'claude' and settings and
                                    any(not model_matches(model, settings) for model in result['observed_models']))
    result['fast_mode_mismatch'] = result.get('fast_mode_state') == 'on'
    return result


def inspect(job, number=None):
    meta = load(job / 'offload.json')
    number = number or meta['run']
    directory = run_dir(job, number)
    record = load(directory / 'run.json')
    settings = model_settings(meta)
    summary = events_summary(directory / 'events.jsonl', meta['tool'], settings)
    result = {**record, **summary, **settings, 'offload_id': job.name, 'run': number,
              'tool': meta['tool'], 'cwd': meta['cwd'], 'run_dir': str(directory), 'speed': 'standard',
              'lease_seconds': meta['lease_seconds'], 'max_runtime': meta['max_runtime']}
    if not summary.get('session_id'):
        result['session_id'] = record.get('resume_session_id')
    state = record['state']
    if state in ACTIVE:
        try:
            worker_alive = alive(record.get('runner_pid'), record.get('runner_identity'))
            if not worker_alive and (record.get('runner_pid') is not None or time.time() - record.get('created_epoch', 0) > 5):
                state = 'lost'
        except Failure as error:
            result['identity_warning'] = str(error)
    result['transport_success'] = bool(record.get('exit_code') == 0 and summary['agent_success']
                                      and not summary['partial_tail'] and not summary['malformed_lines']
                                      and not summary.get('model_mismatch') and not summary.get('fast_mode_mismatch')
                                      and not record.get('stop_reason'))
    if state == 'exited':
        if summary.get('model_mismatch') or summary.get('fast_mode_mismatch'):
            state = 'failed'
        elif summary['last_message'].lstrip().startswith('NEEDS_INPUT:'):
            state = 'needs_input'
        else:
            state = 'done' if result['transport_success'] else 'failed'
    guard_stop = directory / 'watchdog-stop.json'
    if guard_stop.exists():
        result['watchdog_stop'] = load(guard_stop)
        if state == 'lost':
            result['stop_reason'] = result['watchdog_stop']['reason']
    result['state'] = state
    result['attention'] = state in {'needs_input', 'failed', 'lost', 'expired', 'owner_lost', 'timed_out'}
    result['last_message'] = summary['last_message'][-8000:]
    return result


def argv_for(meta, session):
    exe = meta['executable']
    settings = model_settings(meta)
    model = settings['resolved_model'] or settings['requested_model']
    if meta['tool'] == 'codex':
        command = [exe, '-a', 'never', 'exec', '-m', model,
                   '-c', 'model_reasoning_effort="%s"' % settings['effort'], '-c', 'service_tier="default"',
                   '-c', 'approval_policy="never"',
                   '--disable', 'fast_mode', '-s', 'read-only' if meta['mode'] == 'read' else 'workspace-write',
                   '-C', meta['cwd'], '--json']
        if meta['mode'] == 'read' or meta.get('allow_non_git'):
            command += ['--skip-git-repo-check']
        if session:
            command += ['resume', session, '-']
        else:
            command += ['-']
        return command
    command = [exe, '-p', '--model', model, '--effort', settings['effort'],
               '--settings', '{"fastMode":false,"ultracode":false}',
               '--output-format', 'stream-json', '--verbose',
               '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--tools', ','.join(meta['tools']), '--allowedTools', ','.join(meta['allowed_tools']),
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
    for directory in meta['add_dirs']:
        command += ['--add-dir', directory]
    if session:
        command += ['--resume', session]
    return command


def check_writers(root, meta, exclude=None):
    if meta['mode'] != 'edit':
        return
    for path in root.iterdir():
        if path.name == exclude or not (path / 'offload.json').is_file():
            continue
        other = load(path / 'offload.json')
        a, b = Path(other['cwd']), Path(meta['cwd'])
        if other['mode'] == 'edit' and (a == b or a in b.parents or b in a.parents):
            status = inspect(path)
            if status['state'] in ACTIVE or status['state'] == 'lost':
                raise Failure('Another edit offload owns this working directory: ' + path.name, 4)


def launch(job, meta, prompt, session=None):
    directory = run_dir(job, meta['run'])
    directory.mkdir(mode=0o700, parents=True)
    # Child gets an explicit boundary; literal bytes are never shell-expanded.
    prefix = ('Complete the assigned task within the caller\'s authorization. '
              'Do not invoke another agentic CLI or offload skill unless this task explicitly asks for it. '
              'If blocked on missing input or permission, end your final answer with no guesses and begin it with NEEDS_INPUT:.\n\n')
    (directory / 'prompt.txt').write_text(prefix + prompt, encoding='utf-8')
    record = dict(state='starting', created_epoch=time.time(), started_at=now(),
                  runner_pid=None, runner_identity=None, child_pid=None, child_identity=None,
                  exit_code=None, stop_reason=None, resume_session_id=session,
                  command=argv_for(meta, session))
    save(directory / 'run.json', record)
    save(job / 'offload.json', meta)
    touch(job)
    try:
        with (directory / 'worker.log').open('ab') as err:
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(job.parent),
                                       '_worker', job.name, str(meta['run'])],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                      start_new_session=True, close_fds=True)
        # The worker owns run.json from here; don't overwrite its handshake.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = load(directory / 'run.json')
            if current['state'] != 'starting' or worker.poll() is not None:
                break
            time.sleep(.02)
    except OSError as error:
        record.update(state='exited', exit_code=127, launch_error=str(error), ended_at=now())
        save(directory / 'run.json', record)
    return inspect(job)


def start(root, args):
    if not identity(os.getpid()):
        raise Failure('Cannot verify helper process identity; refusing to launch')
    prompt = Path(args.prompt_file).read_text(encoding='utf-8')
    cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
    if not Path(cwd).is_dir():
        raise Failure('--cwd must be a directory')
    executable = shutil.which(args.tool)
    if not executable:
        raise Failure('Required CLI not found on PATH: ' + args.tool)
    for value in (args.lease_seconds, args.max_runtime, args.interrupt_grace):
        if not math.isfinite(value) or value <= 0:
            raise Failure('Lease, runtime and interrupt grace must be finite and greater than zero')
    if args.tool == 'codex' and (args.tools is not None or args.allow_tool or args.add_dir):
        raise Failure('--tools, --allow-tool and --add-dir are Claude-only options')
    tools = args.tools.split(',') if args.tools is not None else ['Read', 'Glob', 'Grep'] + (['Write', 'Edit'] if args.mode == 'edit' else [])
    tools = [t.strip() for t in tools if t.strip()]
    if args.mode == 'read' and set(tools) - {'Read', 'Glob', 'Grep', 'WebSearch', 'WebFetch'}:
        raise Failure('Read mode only allows reading/search tools; use edit mode for other tools')
    # Selected tools other than Bash are allowed; --allow-tool adds rules such as 'Bash(npm test)'.
    allowed = [t for t in tools if t != 'Bash']
    allowed += [t for t in args.allow_tool if t not in allowed]
    if any(t.split('(')[0] not in tools for t in allowed):
        raise Failure('Allowed tools must also be selected in --tools')
    if args.tool == 'claude' and os.environ.get('CLAUDECODE'):
        raise Failure('Nested Claude environment detected (CLAUDECODE). Use Codex for this offload or diagnose the launcher; no automatic environment bypass.')
    owner_stamp = identity(args.owner_pid) if args.owner_pid else None
    if args.owner_pid and not owner_stamp:
        raise Failure('--owner-pid must identify a live, stable caller process')
    model, effort = normalize_request(args.tool, args.model, args.effort)
    meta = dict(version=3, tool=args.tool, executable=str(Path(executable).resolve()), cwd=cwd, mode=args.mode,
                tools=tools, allowed_tools=allowed, add_dirs=[str(Path(d).resolve(strict=True)) for d in args.add_dir],
                lease_seconds=args.lease_seconds, max_runtime=args.max_runtime,
                interrupt_grace=args.interrupt_grace, allow_non_git=args.allow_non_git,
                owner_pid=args.owner_pid, owner_identity=owner_stamp, run=1, requests={},
                model=model, effort=effort)
    # Hash the request, not the catalogue answer, so an identical retry stays idempotent.
    digest = hashlib.sha256(json.dumps([meta, prompt], sort_keys=True).encode()).hexdigest()
    # Manifests from before model selection hashed the same request without these keys.
    legacy = {k: v for k, v in meta.items() if k not in ('model', 'effort')}
    legacy_digest = hashlib.sha256(json.dumps([dict(legacy, version=2), prompt], sort_keys=True).encode()).hexdigest()
    meta['start_hash'] = digest
    job_id = args.id or uuid.uuid4().hex
    job = job_dir(root, job_id)
    # An identical retry returns the existing offload without consulting the catalogue again.
    selection = None if (job / 'offload.json').exists() else resolve_model(args.tool, executable, model, effort)
    with locked(root):
        if job.exists() and not (job / 'offload.json').exists():
            if not any(job.iterdir()):
                job.rmdir()
            else:
                raise Failure('Incomplete offload directory has no manifest; inspect it and use a new ID. Existing contents were preserved.')
        if job.exists():
            old = load(job / 'offload.json')
            if old.get('start_hash') not in (digest, legacy_digest if 'model' not in old else None):
                raise Failure('Offload ID already exists with a different request', 4)
            touch(job)
            return inspect(job)
        meta.update(selection or resolve_model(args.tool, executable, model, effort))
        check_writers(root, meta)
        job.mkdir(mode=0o700)
        return launch(job, meta, prompt)


def send(root, args):
    job = job_dir(root, args.id)
    prompt = Path(args.prompt_file).read_text(encoding='utf-8')
    request = valid_id(args.request_id)
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    with locked(root):
        meta = load(job / 'offload.json')
        previous = meta['requests'].get(request)
        if previous:
            if previous['hash'] != digest:
                raise Failure('Request ID was already used for a different prompt', 4)
            touch(job)
            return inspect(job, previous['run'])
        if meta['tool'] == 'claude' and os.environ.get('CLAUDECODE'):
            raise Failure('Nested Claude environment detected (CLAUDECODE). Continue this offload from a non-Claude caller.')
        status = inspect(job)
        if status['state'] in ACTIVE or status['state'] == 'lost':
            raise Failure('Offload is active or lost; inspect and stop it before resuming', 4)
        if status.get('model_mismatch'):
            raise Failure('The previous run failed the model check (observed %s); start a new offload'
                          % ', '.join(status.get('observed_models') or []))
        if not status['session_id']:
            raise Failure('No saved CLI session ID; start a new offload')
        if meta.get('owner_pid') and not alive(meta['owner_pid'], meta['owner_identity']):
            raise Failure('Original owner exited; start a new offload with a live owner')
        check_writers(root, meta, job.name)
        if meta['tool'] == 'claude' and meta.get('model_family') and not meta.get('resolved_model'):
            # Keep the conversation on the release that answered it, even if the alias moves.
            # The init model keeps variant suffixes such as [1m] that message IDs omit.
            meta['resolved_model'] = status.get('init_model') or status.get('observed_model')
        meta['run'] += 1
        meta['requests'][request] = {'hash': digest, 'run': meta['run']}
        return launch(job, meta, prompt, status['session_id'])


def signal_group(record, sig):
    # Recheck OS identity on every operation; never signal a recycled PID.
    if alive(record.get('child_pid'), record.get('child_identity'), verify=True):
        try:
            os.killpg(record['child_pid'], sig)
        except ProcessLookupError:
            pass


def owned_group_signal(pid, sig):
    # Only for the live worker/watchdog that created this group, never a stale job record.
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def cleanup_group(pid):
    owned_group_signal(pid, signal.SIGTERM)
    time.sleep(.2)
    owned_group_signal(pid, signal.SIGKILL)


def child_gate(root, args):
    job = job_dir(root, args.id)
    directory = run_dir(job, args.number)
    record = load(directory / 'run.json')
    deadline = time.monotonic() + 10
    while not (directory / 'go.json').exists():
        if time.monotonic() >= deadline:
            return 125
        try:
            if not alive(record.get('runner_pid'), record.get('runner_identity')):
                return 125
        except Failure:
            pass  # Unknown is not dead; the startup deadline still bounds this gate.
        time.sleep(.05)
    os.execvpe(record['command'][0], record['command'], os.environ)


def watchdog(root, args):
    job = job_dir(root, args.id)
    directory = run_dir(job, args.number)
    record = load(directory / 'run.json')
    meta = load(job / 'offload.json')
    save(directory / 'watchdog.json', {'pid': os.getpid(), 'identity': identity(os.getpid())})
    reason, stop_at, cleanup_error = None, None, None
    signalled, timeline = set(), []
    bound = record['deadline_monotonic'] + meta.get('interrupt_grace', 15) + 40
    try:
        while time.monotonic() < bound:
            try:
                if not alive(record.get('child_pid'), record.get('child_identity')):
                    break
                if reason is None:
                    if not alive(record.get('runner_pid'), record.get('runner_identity')):
                        reason = 'worker_lost'
                    elif time.monotonic() >= record['deadline_monotonic'] + 7:
                        reason = 'watchdog_deadline'
                    if reason:
                        stop_at = time.monotonic()
                        save(directory / 'watchdog-stop.json', {'reason': reason, 'at': now(), 'signals': timeline})
                if reason:
                    grace = meta.get('interrupt_grace', 15) if reason == 'worker_lost' else 3
                    for after, sig in ((0, signal.SIGINT), (grace, signal.SIGTERM), (grace + 3, signal.SIGKILL)):
                        if sig not in signalled and time.monotonic() - stop_at >= after:
                            signal_group(record, sig)
                            signalled.add(sig)
                            timeline.append({'signal': int(sig), 'at': now()})
                            save(directory / 'watchdog-stop.json', {'reason': reason, 'signals': timeline})
                    if signal.SIGKILL in signalled:
                        break
            except (Failure, OSError, ValueError) as error:
                # A transient inspection failure does not restart the escalation clock.
                cleanup_error = str(error)
                try:
                    save(directory / 'watchdog-warning.json', {'at': now(), 'error': str(error)})
                except OSError:
                    pass
            time.sleep(.3)
        else:
            cleanup_error = 'Watchdog inspection/recovery deadline exceeded'
    finally:
        # The live worker cleans natural exits. Avoid signalling a dead leader twice.
        if reason and signal.SIGKILL not in signalled:
            try:
                cleanup_group(record['child_pid'])
            except OSError as error:
                cleanup_error = str(error)
        save(directory / 'watchdog-done.json', {'at': now(), 'reason': reason, 'cleanup_error': cleanup_error})
    return None


def worker(root, args):
    job = job_dir(root, args.id)
    meta = load(job / 'offload.json')
    directory = run_dir(job, args.number)
    record = load(directory / 'run.json')
    record.update(runner_pid=os.getpid(), runner_identity=identity(os.getpid()), deadline_monotonic=time.monotonic() + meta['max_runtime'])
    save(directory / 'run.json', record)
    child = None
    reason = None
    stop_at = None
    signalled = set()

    def request_stop(signum, _frame):
        nonlocal reason, stop_at
        if reason is None:
            reason = 'interrupted'
            stop_at = time.monotonic()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)
    began = time.monotonic()
    last_model_check = 0
    try:
        env = dict(os.environ)
        if meta['tool'] == 'claude':
            env.update(CLAUDE_CODE_DISABLE_FAST_MODE='1', CLAUDE_CODE_EFFORT_LEVEL=model_settings(meta)['effort'])
        with (directory / 'prompt.txt').open('rb') as source, (directory / 'events.jsonl').open('wb') as out, (directory / 'stderr.log').open('wb') as err:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(root),
                                      '_child', job.name, str(args.number)], cwd=meta['cwd'], stdin=source,
                                     stdout=out, stderr=err, env=env, start_new_session=True, close_fds=True)
            record.update(child_pid=child.pid, child_identity=identity(child.pid))
            if not record['child_identity']:
                raise Failure('Cannot capture child identity; refusing to launch CLI')
            save(directory / 'run.json', record)
            with (directory / 'watchdog.log').open('ab') as guard_log:
                guard = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--root', str(root),
                                          '_watchdog', job.name, str(args.number)],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=guard_log,
                                         start_new_session=True, close_fds=True)
            ready_deadline = time.monotonic() + 4
            while not (directory / 'watchdog.json').exists():
                if guard.poll() is not None or time.monotonic() >= ready_deadline:
                    raise Failure('Watchdog failed to arm; refusing to launch the CLI')
                time.sleep(.02)
            record.update(state='running')
            save(directory / 'run.json', record)
            save(directory / 'go.json', {'at': now()})
            while child.poll() is None:
                elapsed = time.monotonic() - began
                if reason is None:
                    if (directory / 'stop.json').exists():
                        reason = 'interrupted'
                    elif elapsed >= meta['max_runtime']:
                        reason = 'timed_out'
                    elif time.monotonic() - load(job / 'lease.json')['monotonic'] >= meta['lease_seconds']:
                        reason = 'expired'
                    elif meta.get('owner_pid'):
                        try:
                            if not alive(meta['owner_pid'], meta['owner_identity']):
                                reason = 'owner_lost'
                        except Failure as error:
                            record['identity_warning'] = str(error)
                    if not reason and meta['tool'] == 'claude' and elapsed - last_model_check >= 2:
                        last_model_check = elapsed
                        observed = events_summary(directory / 'events.jsonl', 'claude', model_settings(meta))
                        if observed.get('model_mismatch') or observed.get('fast_mode_mismatch'):
                            reason = 'model_mismatch'
                    if reason:
                        stop_at = time.monotonic()
                if reason:
                    record.update(state='stopping', stop_reason=reason)
                    if not signalled:
                        save(directory / 'run.json', record)
                    grace = meta.get('interrupt_grace', 15) if reason == 'interrupted' else 3
                    for after, sig in ((0, signal.SIGINT), (grace, signal.SIGTERM), (grace + 3, signal.SIGKILL)):
                        if time.monotonic() - stop_at >= after and sig not in signalled:
                            owned_group_signal(child.pid, sig)
                            signalled.add(sig)
                time.sleep(.15)
            raw = child.wait()
            record['exit_code'] = raw if raw >= 0 else 128 - raw
            cleanup_group(child.pid)
        record.update(state=('failed' if reason == 'model_mismatch' else reason) or 'exited', stop_reason=reason, ended_at=now())
    except Exception as error:
        if child is not None and child.poll() is None:
            for sig, grace in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 1)):
                owned_group_signal(child.pid, sig)
                try:
                    child.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    pass
            cleanup_group(child.pid)
        record.update(state='exited', exit_code=127, launch_error=str(error), ended_at=now())
    save(directory / 'run.json', record)
    return None


def read_events(job, args):
    status = inspect(job)
    number = status['run']
    try:
        run_number, offset = map(int, args.cursor.split(':')) if args.cursor else (number, 0)
    except ValueError:
        raise Failure('Cursor must be RUN:BYTE_OFFSET')
    if run_number != number or offset < 0:
        raise Failure('Stale or invalid cursor; omit it to read the current run')
    if args.max_bytes < 1 or args.max_bytes > 8 * 1024 * 1024:
        raise Failure('--max-bytes must be between 1 and 8388608')
    path = Path(status['run_dir']) / 'events.jsonl'
    output = []
    consumed = offset
    partial = False
    if path.exists():
        with path.open('rb') as stream:
            if offset > path.stat().st_size:
                raise Failure('Cursor beyond end of log')
            if offset:
                stream.seek(offset - 1)
                if stream.read(1) != b'\n':
                    raise Failure('Cursor must point to a line boundary')
            stream.seek(offset)
            while stream.tell() - offset < args.max_bytes:
                # A single large event is returned whole up to 8MiB, or explicitly rejected.
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise Failure('Event exceeds 8MiB; inspect events.jsonl directly')
                if not line.endswith(b'\n'):
                    partial = True
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    event = {'type': 'malformed_line', 'text': line.decode(errors='replace')}
                output.append(event)
                consumed = stream.tell()
    return dict(offload_id=job.name, run=number, state=status['state'], events=output,
                cursor=f'{number}:{consumed}', partial_tail=partial)


def stop(root, job):
    with locked(root):
        status = inspect(job)
        directory = Path(status['run_dir'])
        if status['state'] == 'lost':
            # Lost supervisor: recover only the verified child, never an arbitrary PID.
            signal_group(status, signal.SIGINT)
            deadline = time.monotonic() + 3
            while alive(status.get('child_pid'), status.get('child_identity')) and time.monotonic() < deadline:
                time.sleep(.1)
            signal_group(status, signal.SIGKILL)
            record = load(directory / 'run.json')
            record.update(state='interrupted', stop_reason='lost_worker_cleanup', ended_at=now(), exit_code=None)
            save(directory / 'run.json', record)
            return inspect(job)
        if status['state'] in ACTIVE:
            save(directory / 'stop.json', {'at': now()})
            status['stop_requested'] = True
        return status


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=os.environ.get('AGENT_OFFLOAD_ROOT', str(Path.home() / '.local/state/agent-offload')))
    sub = p.add_subparsers(dest='action', required=True, metavar='ACTION')
    s = sub.add_parser('start', help='Start a bounded offload and return immediately')
    s.add_argument('--tool', choices=['codex', 'claude'], required=True)
    s.add_argument('--cwd', required=True)
    s.add_argument('--prompt-file', required=True)
    s.add_argument('--id')
    s.add_argument('--mode', choices=['read', 'edit'], default='read')
    s.add_argument('--lease-seconds', type=float, default=300)
    s.add_argument('--max-runtime', type=float, default=1800)
    s.add_argument('--owner-pid', type=int)
    s.add_argument('--model', help='Family alias for its newest release (see `models`; e.g. claude: fable, opus, '
                   'sonnet, haiku; codex: astra, sol, luna) or an exact model ID. Default: fable / astra')
    s.add_argument('--effort', default='high', help='Reasoning effort (default high). Claude: low, medium, high, xhigh, '
                   'max; Codex: the levels the chosen model lists in `models`')
    s.add_argument('--interrupt-grace', type=float, default=15, help='Seconds before escalating an explicit stop to SIGTERM')
    s.add_argument('--allow-non-git', action='store_true', help='Allow Codex edit mode outside a Git repository')
    s.add_argument('--tools', help='Claude built-in tool set; comma separated')
    s.add_argument('--allow-tool', action='append', default=[], help='Claude permission pattern; repeatable')
    s.add_argument('--add-dir', action='append', default=[], help='Claude additional context directory')
    helps = {'status': 'Show state and result; renews the lease unless --no-touch',
             'read': 'Read new JSONL events from a cursor; renews the lease',
             'wait': 'Wait up to 60s for completion or attention; renews the lease once',
             'touch': 'Renew the lease and show status',
             'stop': 'Request cancellation; follow with wait'}
    for name in ['status', 'read', 'wait', 'touch', 'stop']:
        s = sub.add_parser(name, help=helps[name])
        s.add_argument('id')
        if name == 'status':
            s.add_argument('--no-touch', action='store_true')
        if name == 'read':
            s.add_argument('--cursor')
            s.add_argument('--max-bytes', type=int, default=65536)
        if name == 'wait':
            s.add_argument('--timeout', type=float, default=30)
    s = sub.add_parser('send', help='Continue the exact session after its current run exits')
    s.add_argument('id')
    s.add_argument('--prompt-file', required=True)
    s.add_argument('--request-id', required=True)
    sub.add_parser('list', help='Summarize all offloads in the registry without renewing leases')
    s = sub.add_parser('models', help='List models the destination CLI can use and the newest per family')
    s.add_argument('--tool', choices=['codex', 'claude'], required=True)
    s.add_argument('--all', action='store_true', help='Codex: include hidden catalogue entries')
    s.add_argument('--probe', action='append', default=[], metavar='ALIAS',
                   help='Claude: one tiny paid call per alias to report the exact release it selects')
    for name in ('_worker', '_child', '_watchdog'):
        s = sub.add_parser(name)  # internal; no help keeps it out of the command list
        s.add_argument('id')
        s.add_argument('number', type=int)
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    try:
        code = 0
        if args.action in ('_worker', '_child', '_watchdog'):
            return {'_worker': worker, '_child': child_gate, '_watchdog': watchdog}[args.action](root, args) or 0
        if args.action == 'start':
            result = start(root, args)
        elif args.action == 'send':
            result = send(root, args)
        elif args.action == 'models':
            result = list_models(args)
        elif args.action == 'list':
            result = []
            if root.exists():
                for job in sorted(root.iterdir()):
                    if job.is_symlink() or not (job / 'offload.json').exists():
                        continue
                    try:
                        status = inspect(job)
                        result.append({k: status.get(k) for k in ['offload_id', 'run', 'tool', 'cwd', 'state', 'attention', 'session_id']})
                    except (OSError, ValueError, KeyError, Failure) as error:
                        result.append({'offload_id': job.name, 'state': 'unreadable', 'error': str(error)})
        else:
            job = job_dir(root, args.id)
            if args.action == 'stop':
                result = stop(root, job)
            else:
                if args.action == 'wait' and (not math.isfinite(args.timeout) or not 0 <= args.timeout <= 60):
                    raise Failure('--timeout must be between 0 and 60 seconds')
                if not getattr(args, 'no_touch', False):
                    # No background heartbeat: orphaned wait commands cannot renew forever.
                    touch(job)
                if args.action == 'read':
                    result = read_events(job, args)
                else:
                    result = inspect(job)
                    if args.action == 'wait':
                        deadline = time.monotonic() + args.timeout
                        while result['state'] in ACTIVE and time.monotonic() < deadline:
                            time.sleep(.2)
                            result = inspect(job)
                        if result['state'] in ACTIVE:
                            result['wait_reason'] = 'timeout'
                            code = 124
                        else:
                            result['wait_reason'] = 'attention' if result['attention'] else 'done'
                            code = 3 if result['attention'] else 0
        print(json.dumps(result, ensure_ascii=False))
        return code
    except (Failure, OSError, ValueError, KeyError) as error:
        print(json.dumps({'error': str(error)}))
        return getattr(error, 'code', 2)


if __name__ == '__main__':
    sys.exit(main())
